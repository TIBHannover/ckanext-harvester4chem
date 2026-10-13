"""CKAN harvester for Chemotion's ChemDCAT-AP API (v9)."""
import json
import logging
import os
import re
import time
import traceback
import uuid
from urllib.parse import parse_qs, urlparse

import requests
from ckan import model
from ckan.lib.munge import munge_tag, munge_title_to_name
from ckan.logic import get_action
from ckan.model import Session
from ckanext.harvest.harvesters.base import HarvesterBase
from ckanext.harvest.model import HarvestObject
from ckanext.harvester4chem.license_utils import apply_license
from ckanext.harvester4chem.molecule_sync import (
    normalize_structure,
    synchronize_harvested_package,
)

log = logging.getLogger(__name__)


class ChemDCATAPHarvester(HarvesterBase):
    # Expectation: only syntactically valid Chemotion publication IDs and
    # InChIKeys enter the queue; malformed listing values are skipped early.
    _INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")

    def info(self):
        return {
            "name": "chemdcat_ap",
            "title": "ChemDCAT-AP Harvester",
            "description": "Harvest ChemDCAT-AP metadata from Chemotion Repository",
        }

    def _config(self, source):
        try:
            config = json.loads(source.config or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError("Harvester configuration must be valid JSON") from exc
        if not isinstance(config, dict):
            raise ValueError("Harvester configuration must be a JSON object")
        supplied_url = (config.get("api_base_url") or source.url or "").rstrip("/")
        if not supplied_url:
            raise ValueError("Set the Chemotion Swagger URL or API base URL as the harvest source URL")
        parsed_url = urlparse(supplied_url)
        if parsed_url.path.rstrip("/").endswith("/swagger_doc"):
            base = parsed_url.scheme + "://" + parsed_url.netloc + "/api/v1/public"
        else:
            base = supplied_url
        if base.endswith("/metadata/publications"):
            public_base = base.rsplit("/metadata/publications", 1)[0]
            default_listing = base
        elif base.endswith("/metadata"):
            public_base = base.rsplit("/metadata", 1)[0]
            default_listing = base + "/publications"
        elif base.endswith("/api/v1/public"):
            public_base = base
            default_listing = base + "/metadata/publications"
        elif not parsed_url.path or parsed_url.path == "/":
            public_base = base + "/api/v1/public"
            default_listing = public_base + "/metadata/publications"
        else:
            public_base = base + "/api/v1/public"
            default_listing = public_base + "/metadata/publications"
        listing = config.get("publications_endpoint") or default_listing
        return config, listing, public_base + "/metadata/chemdcat"

    @staticmethod
    def _record_identifier(item):
        if isinstance(item, dict):
            for key in ("publication_id", "publication-id", "publicationId"):
                if item.get(key) is not None:
                    return "publication_id", str(item[key])
            for key in ("url", "@id", "identifier"):
                if item.get(key) is not None:
                    result = ChemDCATAPHarvester._record_identifier(item[key])
                    if result[1]:
                        return result
            return None, None
        if isinstance(item, (int, float)):
            return "publication_id", str(int(item))
        if not isinstance(item, str) or not item.strip():
            return None, None
        value = item.strip()
        query = parse_qs(urlparse(value).query)
        for key in ("publication_id", "publication-id", "publicationId"):
            if query.get(key):
                return "publication_id", query[key][0]
        if query.get("inchikey"):
            candidate = query["inchikey"][0]
            key_part, separator, suffix = candidate.partition("/")
            if (ChemDCATAPHarvester._INCHIKEY_RE.fullmatch(key_part.upper()) and
                    (not separator or re.fullmatch(r"[A-Za-z0-9._-]+", suffix))):
                return "inchikey", candidate
            log.warning("Skipping invalid InChIKey in Chemotion listing: %r", candidate)
            return None, None
        if value.lower().startswith("inchikey:"):
            candidate = value.split(":", 1)[1]
            key_part, separator, suffix = candidate.partition("/")
            if (ChemDCATAPHarvester._INCHIKEY_RE.fullmatch(key_part.upper()) and
                    (not separator or re.fullmatch(r"[A-Za-z0-9._-]+", suffix))):
                return "inchikey", candidate
            log.warning("Skipping invalid InChIKey in Chemotion listing: %r", candidate)
            return None, None
        match = re.search(r"/pid/([^/?#]+)", value)
        if match:
            return "publication_id", match.group(1)
        if value.startswith(("http://", "https://")):
            return None, None
        return "publication_id", value

    @staticmethod
    def _records(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("publications", "data", "results", "items"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        raise ValueError("Listing response must contain a publication list")

    @staticmethod
    def _get_response(url, params, timeout, retries=3, backoff=2):
        for attempt in range(retries + 1):
            response = requests.get(
                url, params=params, timeout=timeout,
                headers={"Accept": "application/ld+json, application/json"})
            if response.status_code not in (429, 500, 502, 503, 504):
                if response.status_code == 422:
                    # Chemotion's validation body usually identifies the
                    # rejected query parameter; retain it for diagnosis.
                    log.error("Chemotion rejected request %s with HTTP 422: %s",
                              response.url, response.text[:2000])
                response.raise_for_status()
                return response
            if attempt >= retries:
                response.raise_for_status()
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else backoff * (2 ** attempt)
            except (TypeError, ValueError):
                delay = backoff * (2 ** attempt)
            log.warning("Chemotion returned HTTP %s; retrying in %.1f seconds",
                        response.status_code, delay)
            time.sleep(delay)

    def gather_stage(self, harvest_job):
        try:
            config, endpoint, _ = self._config(harvest_job.source)
            initial_offset = int(config.get("offset", 0))
            limit = int(config.get("limit", 1000))
            timeout = float(config.get("timeout", 30))
            retries = int(config.get("retries", 3))
            backoff = float(config.get("retry_backoff", 2))
            if initial_offset < 0 or limit < 1 or limit > 1000:
                raise ValueError("offset must be >= 0 and limit must be between 1 and 1000")
            list_params = dict(config.get("list_params") or {})
            if "date_from" in config:
                list_params["date_from"] = config["date_from"]
            if config.get("date_to"):
                list_params["date_to"] = config["date_to"]
            else:
                list_params.pop("date_to", None)
            if "types" in config:
                types = config["types"]
            elif config.get("type_chem"):
                types = [config["type_chem"]]
            else:
                types = ["Container"]

            ids, seen = [], set()
            for publication_type in types:
                offset = initial_offset
                while True:
                    params = dict(list_params)
                    params.update({"type": publication_type,
                                   "offset": offset, "limit": limit})
                    response = self._get_response(
                        endpoint, params, timeout, retries=retries, backoff=backoff)
                    records = self._records(response.json())
                    if not records:
                        break
                    for item in records:
                        query_parameter, identifier = self._record_identifier(item)
                        guid = ("%s:%s" % (query_parameter, identifier)
                                if identifier else None)
                        if guid and guid not in seen:
                            ids.append(guid)
                            seen.add(guid)
                    if len(records) < limit:
                        break
                    offset += limit

            object_ids = []
            for guid in ids:
                harvest_object = HarvestObject(guid=guid, job=harvest_job)
                harvest_object.save()
                object_ids.append(harvest_object.id)
            log.info("Gathered %d Chemotion publication identifiers", len(object_ids))
            return object_ids
        except Exception as exc:
            log.exception("ChemDCAT-AP gather failed")
            self._save_gather_error(
                "Could not gather publication identifiers: %s / %s" %
                (exc, traceback.format_exc()), harvest_job)
            return None

    def fetch_stage(self, harvest_object):
        try:
            config, _, endpoint = self._config(harvest_object.source)
            guid = str(harvest_object.guid)
            if ":" in guid:
                query_parameter, identifier = guid.split(":", 1)
            else:
                query_parameter, identifier = "publication_id", guid
            if query_parameter not in ("publication_id", "inchikey"):
                raise ValueError("Unsupported Chemotion record identifier")
            if query_parameter == "inchikey":
                key_part, separator, suffix = identifier.partition("/")
                if (not self._INCHIKEY_RE.fullmatch(key_part.upper()) or
                        (separator and not re.fullmatch(r"[A-Za-z0-9._-]+", suffix))):
                    raise ValueError(
                        "Invalid InChIKey GUID; expected a standard InChIKey "
                        "optionally followed by /<publication-id>")
            response = self._get_response(
                endpoint, {query_parameter: identifier},
                float(config.get("timeout", 30)),
                retries=int(config.get("retries", 3)),
                backoff=float(config.get("retry_backoff", 2)))
            content = response.json()
            if not isinstance(content, dict):
                raise ValueError("Record response must be a JSON object")
            harvest_object.content = json.dumps(content, ensure_ascii=False)
            harvest_object.save()
            return True
        except Exception as exc:
            log.exception("ChemDCAT-AP fetch failed for %s", harvest_object.guid)
            self._save_object_error(
                "Fetch failed for %s: %s / %s" %
                (harvest_object.guid, exc, traceback.format_exc()), harvest_object)
            return False

    @staticmethod
    def _key_token(key):
        """Normalize JSON-LD compact terms and schema slot names."""
        local = str(key).rsplit("#", 1)[-1].rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        return re.sub(r"[^a-z0-9]", "", local.lower())

    @staticmethod
    def _walk_nodes(value):
        if isinstance(value, dict):
            yield value
            for key, child in value.items():
                if key != "@context" and isinstance(child, (dict, list)):
                    for node in ChemDCATAPHarvester._walk_nodes(child):
                        yield node
        elif isinstance(value, list):
            for child in value:
                for node in ChemDCATAPHarvester._walk_nodes(child):
                    yield node

    @classmethod
    def _all_values(cls, content, *keys):
        wanted = set(cls._key_token(key) for key in keys)
        values = []
        for node in cls._walk_nodes(content):
            for key, value in node.items():
                if (key != "@context" and cls._key_token(key) in wanted and
                        value not in (None, "", [], {})):
                    values.append(value)
        return values

    @classmethod
    def _value(cls, content, *keys):
        if isinstance(content, dict):
            for key in keys:
                value = content.get(key)
                if value not in (None, "", [], {}):
                    return value
        values = cls._all_values(content, *keys)
        return values[0] if values else None

    @classmethod
    def _dataset_node(cls, content):
        for node in cls._walk_nodes(content):
            types = cls._strings(node.get("@type") or node.get("type"))
            if any(cls._key_token(value).endswith("dataset") for value in types):
                return node
        for node in cls._walk_nodes(content):
            if node.get("distribution") or node.get("distributions"):
                return node
        return content

    @classmethod
    def _doi(cls, content):
        # Prefer explicit DOI fields, then inspect the dataset identifiers.
        values = cls._all_values(content, "doi", "publication_doi", "identifier",
                                 "other_identifier", "id", "@id")
        pattern = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
        for value in values:
            for text in cls._strings(value):
                match = pattern.search(text)
                if match:
                    return match.group(0).rstrip(".,;:)]")
        return None

    @classmethod
    def _strings(cls, value):
        if isinstance(value, dict):
            preferred = ("@id", "id", "url", "value", "name", "title",
                         "label", "identifier", "@value")
            ordered = []
            for wanted in preferred:
                ordered.extend(v for k, v in value.items()
                               if cls._key_token(k) == cls._key_token(wanted))
            ordered.extend(v for k, v in value.items()
                           if k != "@context" and v not in ordered)
            result = []
            for child in ordered:
                result.extend(cls._strings(child))
            return result
        if isinstance(value, (list, tuple)):
            result = []
            for child in value:
                result.extend(cls._strings(child))
            return result
        return [str(value)] if value is not None else []

    @classmethod
    def _license_candidates(cls, content):
        values = cls._all_values(content, "license", "licence", "license_id", "rights",
                                 "accessRights", "access_rights")
        candidates = []
        for value in values:
            for candidate in cls._strings(value):
                candidate = candidate.strip()
                if candidate and candidate not in candidates:
                    candidates.append(candidate)
        return candidates

    @staticmethod
    def _license_token(value):
        value = str(value or "").strip().lower()
        value = re.sub(r"^https?://", "", value)
        value = value.removeprefix("www.") if hasattr(value, "removeprefix") else re.sub(r"^www\.", "", value)
        return re.sub(r"[^a-z0-9]", "", value)

    @classmethod
    def _license_id_for(cls, candidates, license_items):
        aliases = {
            "creativecommonsattribution40": "cc-by",
            "creativecommonsattribution30": "cc-by",
            "ccby40": "cc-by", "ccby30": "cc-by",
            "creativecommonsattributionsharealike40": "cc-by-sa",
            "ccbysa40": "cc-by-sa",
            "creativecommonsattributionnoncommercial40": "cc-by-nc",
            "ccbync40": "cc-by-nc",
            "creativecommonsattributionnoncommercialsharealike40": "cc-by-nc-sa",
            "ccbyncsa40": "cc-by-nc-sa",
            "creativecommonsattributionnoderivatives40": "cc-by-nd",
            "ccbynd40": "cc-by-nd",
            "creativecommonsattributionnoncommercialnoderivatives40": "cc-by-nc-nd",
            "ccbyncnd40": "cc-by-nc-nd",
            "creativecommonspublicdomainzero10": "cc-zero",
            "creativecommonsorgpublicdomainzero10": "cc-zero",
            "cc0": "cc-zero",
            "cc010": "cc-zero",
        }
        for candidate in candidates:
            token = cls._license_token(candidate)
            normalized_candidate = candidate.lower().rstrip("/")
            mapped = aliases.get(token)
            if "creativecommons.org/licenses/" in normalized_candidate:
                slug = normalized_candidate.split("creativecommons.org/licenses/", 1)[1].strip("/")
                if slug.startswith("publicdomain/zero/"):
                    mapped = "cc-zero"
                elif slug.startswith("by/"):
                    mapped = "cc-by"
                elif slug.startswith("by-"):
                    variant = slug.split("/", 1)[0][3:]
                    mapped = "cc-by-" + variant
            if "creativecommons.org/publicdomain/zero/" in normalized_candidate:
                mapped = "cc-zero"
            for item in license_items:
                fields = (item.get("id"), item.get("url"), item.get("title"))
                if any(cls._license_token(field) == token for field in fields if field):
                    return item.get("id")
                if mapped and item.get("id") == mapped:
                    return mapped
        return None

    @classmethod
    def _measurement_data(cls, content):
        relation_fields = set((
            "wasgeneratedby", "evaluatedentity", "evaluatedactivity", "realizedplan",
            "carriedoutby", "hadinputentity", "hadoutputentity",
            "hasquantitativeattribute", "hasqualitativeattribute",
        ))
        result = []
        for node in cls._walk_nodes(content):
            selected = {}
            type_values = cls._strings(node.get("@type") or node.get("type"))
            is_measurement = any(
                "quantitativeattribute" in cls._key_token(value) or
                "qualitativeattribute" in cls._key_token(value)
                for value in type_values)
            for key, value in node.items():
                token = cls._key_token(key)
                if (key != "@context" and value not in (None, "", [], {}) and
                        (token in relation_fields or token.startswith("has") or
                         (is_measurement and token in
                          ("value", "unit", "title", "description", "hasquantitytype", "id", "@id")))):
                    selected[key] = value
            if selected:
                subject = node.get("@id") or node.get("id")
                record = {"properties": selected}
                if subject:
                    record["id"] = subject
                if node.get("@type") or node.get("type"):
                    record["type"] = node.get("@type") or node.get("type")
                result.append(record)
        return result

    @classmethod
    def _expand_curie(cls, value):
        """Expand ontology CURIEs used by ChemDCAT-AP, leaving URLs intact."""
        if not value:
            return None
        value = str(value).strip()
        if value.startswith(("http://", "https://")):
            return value
        prefixes = {
            "CHMO": "http://purl.obolibrary.org/obo/CHMO_",
            "CHEBI": "http://purl.obolibrary.org/obo/CHEBI_",
            "OBI": "http://purl.obolibrary.org/obo/OBI_",
            "QUDT": "http://qudt.org/vocab/quantitykind/",
        }
        if ":" not in value:
            return None
        prefix, local = value.split(":", 1)
        base = prefixes.get(prefix.upper())
        return base + local if base and local else None

    @classmethod
    def _measurement_info(cls, content):
        """Read technique labels/ontology IDs and preserve related activity data."""
        measurements = []
        activities = cls._all_values(content, "was_generated_by")
        flattened = []
        for activity in activities:
            if isinstance(activity, list):
                flattened.extend(activity)
            else:
                flattened.append(activity)
        for activity in flattened:
            if not isinstance(activity, dict):
                continue
            concept = activity.get("rdf_type") or activity.get("rdf:type") or {}
            if not isinstance(concept, dict):
                concept = {"id": concept}
            concept_id = cls._text(concept.get("id") or concept.get("@id"))
            label = cls._text(concept.get("title") or activity.get("title"))
            activity_description = cls._text(activity.get("description"))
            measurements.append({
                "technique": label,
                "technique_id": concept_id,
                "technique_iri": cls._expand_curie(concept_id),
                "activity_id": cls._text(activity.get("id") or activity.get("@id")),
                "description": activity_description,
                "evaluated_entity": activity.get("evaluated_entity", []),
            })
        return measurements

    @classmethod
    def _molar_mass(cls, content):
        """Extract a value from the ChemDCAT-AP has_molar_mass node."""
        for node in cls._walk_nodes(content):
            for key, value in node.items():
                if cls._key_token(key) != "hasmolarmass":
                    continue
                for child in cls._walk_nodes(value):
                    if child.get("value") is not None:
                        return child.get("value")
        return None

    @staticmethod
    def _generate_chemical_image(inchi_value, inchikey):
        """Create the same local RDKit depiction used by the legacy harvester."""
        if not inchi_value or not inchikey or not str(inchi_value).startswith("InChI"):
            return None
        try:
            from ckan.common import config as ckan_config
            from rdkit.Chem import Draw, inchi as rdkit_inchi

            storage_path = ckan_config.get(
                "ckan.storage_path", "/var/lib/ckan/default/storage")
            image_dir = os.path.join(storage_path, "images")
            os.makedirs(image_dir, exist_ok=True)
            image_path = os.path.join(image_dir, str(inchikey) + ".png")
            molecule = rdkit_inchi.MolFromInchi(str(inchi_value))
            if molecule is None:
                log.warning("RDKit could not create a molecule from InChI %s", inchikey)
                return None
            if not os.path.isfile(image_path):
                Draw.MolToFile(molecule, image_path, size=(400, 400))
            if not os.path.isfile(image_path) or os.path.getsize(image_path) == 0:
                raise IOError("RDKit did not create a readable image file")
            return image_path
        except Exception:
            log.exception("Could not generate chemical depiction for %s", inchikey)
            return None

    @staticmethod
    def _text(value):
        if isinstance(value, dict):
            for key in ("name", "title", "id", "@id", "value"):
                if value.get(key) is not None:
                    return ChemDCATAPHarvester._text(value[key])
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, list):
            return "; ".join(filter(None, (ChemDCATAPHarvester._text(v) for v in value)))
        return None if value is None else str(value)

    def import_stage(self, harvest_object):
        if not harvest_object:
            self._save_object_error("No harvest object received")
            return False
        try:
            content = json.loads(harvest_object.content or "{}")
            if not isinstance(content, dict):
                raise ValueError("Record content must be a JSON object")
            dataset = self._dataset_node(content)

            title = self._text(self._value(dataset, "title", "name", "label"))
            title = title or "Chemotion publication %s" % harvest_object.guid
            identifier = self._text(self._value(dataset, "id", "@id", "identifier"))
            name = munge_title_to_name(identifier or str(harvest_object.guid))
            if not name:
                raise ValueError("Could not derive a CKAN package name")

            landing = self._value(dataset, "landing_page", "landingPage", "homepage")
            if isinstance(landing, dict):
                landing = landing.get("id") or landing.get("@id")
            doi = self._doi(dataset) or self._doi(content)
            if not landing and doi:
                landing = "https://doi.org/" + doi

            resources = self._resources(content, title)
            extras = [{"key": "chemdcat_ap",
                       "value": json.dumps(content, ensure_ascii=False)}]
            extra_values = {
                "doi": doi,
                "inchi": self._text(self._value(content, "inchi")),
                "inchikey": self._text(self._value(content, "inchikey", "inchi_key")),
                "inchi_key": self._text(self._value(content, "inchikey", "inchi_key")),
                "smiles": self._text(self._value(content, "smiles", "canonical_smiles")),
                "molecular_formula": self._text(self._value(content, "molecular_formula")),
                "iupac_name": self._text(self._value(content, "iupac_name")),
                "image_url": None,
                "exactmass": self._molar_mass(content),
            }
            extra_values["mol_formula"] = extra_values["molecular_formula"]
            extra_values["iupacName"] = extra_values["iupac_name"]
            image_path = self._generate_chemical_image(
                extra_values["inchi"], extra_values["inchi_key"])
            if image_path:
                extra_values["chemical_image_path"] = image_path
                # The existing NFDI4Chem UI resolves depictions from this
                # legacy CKAN storage URL convention.
                extra_values["image_url"] = "/storage/images/%s.png" % (
                    extra_values["inchi_key"])

            measurement_info = self._measurement_info(content)
            if measurement_info:
                first_measurement = measurement_info[0]
                extra_values["measurement_technique"] = first_measurement.get("technique")
                extra_values["measurement_technique_iri"] = first_measurement.get("technique_iri")
            measurement_data = self._measurement_data(content)
            measurement_payload = {
                "techniques": measurement_info,
                "attributes_and_relations": measurement_data,
                "dataset_description": self._text(
                    self._value(dataset, "description", "abstract")),
            }
            if measurement_info or measurement_data or measurement_payload["dataset_description"]:
                extra_values["chemdcat_measurement_data"] = json.dumps(
                    measurement_payload, ensure_ascii=False)
            license_candidates = (self._license_candidates(dataset) or
                                  self._license_candidates(content))
            if license_candidates:
                extra_values["chemdcat_license"] = json.dumps(
                    license_candidates, ensure_ascii=False)
                license_uri = next((item for item in license_candidates
                                    if item.startswith(("http://", "https://"))), None)
                if license_uri:
                    extra_values["license_uri"] = license_uri
            for key, value in extra_values.items():
                if value not in (None, "", [], {}):
                    # Chemical fields such as doi/inchi/smiles are custom
                    # schema fields in this CKAN instance. Repeating them as
                    # extras makes package_create reject the dataset. Keep
                    # their top-level values below, and namespace supplemental
                    # copies so they cannot collide with CKAN schema fields.
                    extra_key = "harvester4chem_" + key
                    extras.append({"key": extra_key, "value": str(value)})

            # CKAN harvest's base helper requires a stable remote dataset id.
            # Use the source + gathered GUID so every rerun updates this record.
            remote_key = "%s:%s" % (harvest_object.source.id, harvest_object.guid)
            package_id = str(uuid.uuid5(uuid.NAMESPACE_URL, remote_key))
            # Add a stable remote-ID suffix to new package names. CKAN's
            # name-availability check can race when import workers create
            # records concurrently; the suffix also separates distinct
            # Chemotion records that normalize to the same identifier slug.
            name = "%s-%s" % (name[:90].rstrip("-"),
                               package_id.replace("-", "")[:8])

            # Older harvester versions may have created this same remote
            # record with a CKAN-generated package ID. Harvest's base helper
            # searches by package ID, so the deterministic ID above would not
            # find that package and CKAN could then reject its already-used
            # name. Reuse the linked ID only when both source and GUID match.
            previous_package = self._existing_package_for_guid(harvest_object)
            if previous_package:
                package_id = previous_package["id"]
                name = previous_package.get("name") or name

            package = {
                "id": package_id,
                "name": name,
                "title": title,
                "notes": self._text(self._value(dataset, "description", "abstract")) or "",
                "url": self._text(landing) or "",
                "extras": extras,
                "tags": self._tags(content),
                "resources": resources,
            }
            # These mirrors support installed CKAN chemical-structure plugins
            # that look for package fields in addition to package extras.
            for key in ("inchi", "inchikey", "inchi_key", "smiles",
                        "molecular_formula", "mol_formula", "exactmass",
                        "iupac_name", "iupacName", "doi", "image_url",
                        "measurement_technique", "measurement_technique_iri"):
                value = extra_values.get(key)
                if value not in (None, "", [], {}):
                    package[key] = value
            context = {"model": model, "session": Session, "user": "harvest",
                       "ignore_auth": True}
            source = get_action("package_show")(
                context.copy(), {"id": harvest_object.source.id})
            package["owner_org"] = source.get("owner_org")

            license_value = self._value(content, "licence", "license")
            if license_value:
                try:
                    # Use the repository's shared license mapper, as the
                    # legacy Chemotion harvester does, then use the local
                    # catalog matcher as a fallback.
                    apply_license(package, license_value, context)
                except Exception:
                    log.exception("Shared Chemotion license mapping failed")
            if license_candidates and not package.get("license_id"):
                try:
                    license_items = get_action("license_list")(context.copy(), {})
                    license_id = self._license_id_for(license_candidates, license_items)
                    if license_id:
                        package["license_id"] = license_id
                    else:
                        log.warning("No CKAN license matched Chemotion license %s",
                                    license_candidates[0])
                except Exception:
                    # Keep the source license in extras even if the CKAN
                    # license-list action is unavailable.
                    log.exception("Could not map Chemotion license to CKAN license_id")
            publisher = self._value(content, "publisher", "maintainer")
            author = self._value(content, "creator", "author", "authors")
            modified = self._value(dataset, "modified", "metadata_modified",
                                   "date_modified", "dateModified")
            if publisher:
                package["maintainer"] = self._text(publisher)
            if author:
                package["author"] = self._text(author)
            if modified:
                package["metadata_modified"] = self._text(modified)

            normalize_structure(
                package.get("inchi"), package.get("inchi_key"),
                package.get("smiles"), package.get("mol_formula"),
                package.get("exactmass"))
            transaction = Session.begin_nested()
            result = self._create_or_update_package(
                package, harvest_object, "package_show")
            if result is None or result is False:
                if transaction.is_active:
                    transaction.rollback()
                return False
            if image_path and extra_values.get("image_url"):
                package["image_url"] = extra_values["image_url"]
            synchronize_harvested_package(
                package, harvest_object,
                names=self._molecule_names(content),
                name_source="Chemotion",
            )
            if transaction.is_active:
                transaction.commit()
            Session.commit()
            # _create_or_update_package returns True for a change and
            # 'unchanged' when the existing record already matches.
            return result
        except Exception as exc:
            Session.rollback()
            log.exception("ChemDCAT-AP import failed for %s", harvest_object.guid)
            error_text = str(exc)
            traceback_text = traceback.format_exc()
            if ("relationship_relation_delete" in traceback_text or
                    ("name" in error_text and "relationship" in traceback_text.lower())):
                # Expectation: ckanext-relationship's after_update hook passes
                # the required relation name to relationship_relation_delete.
                # That hook is outside this harvester and must be fixed or
                # upgraded in the CKAN environment before package updates pass.
                error_text += (
                    " | ckanext-relationship after_update failed: its "
                    "relationship_relation_delete call must include the "
                    "required name; fix/upgrade that extension and retry")
            self._save_object_error(
                "Import failed for %s: %s / %s" %
                (harvest_object.guid, error_text, traceback_text), harvest_object)
            return False

    @staticmethod
    def _existing_package_for_guid(harvest_object):
        """Find a prior package linked to this exact source and remote GUID."""
        source_id = harvest_object.source.id
        objects = (Session.query(HarvestObject)
                   .filter(HarvestObject.guid == harvest_object.guid)
                   .all())
        for previous_object in objects:
            if previous_object.id == harvest_object.id:
                continue
            try:
                same_source = previous_object.source.id == source_id
            except AttributeError:
                same_source = False
            if not same_source or not previous_object.package_id:
                continue
            context = {"model": model, "session": Session,
                       "ignore_auth": True}
            try:
                return get_action("package_show")(
                    context, {"id": previous_object.package_id})
            except Exception:
                # A stale harvest-object link should not block package import.
                log.debug("Prior package link %s for GUID %s is unavailable",
                          previous_object.package_id, harvest_object.guid,
                          exc_info=True)
        return None

    @classmethod
    def _molecule_names(cls, content):
        """Return the chemical entity's IUPAC/alternate names, never dataset titles."""
        def chemical_values(value):
            if isinstance(value, dict):
                # ChemDCAT-AP typed literals may carry a label/type beside
                # their actual value; only the literal's value is a name.
                if value.get("value") is not None:
                    return cls._strings(value["value"])
                return cls._strings(value.get("name") or value.get("title") or
                                    value.get("@value"))
            if isinstance(value, list):
                result = []
                for item in value:
                    result.extend(chemical_values(item))
                return result
            return cls._strings(value)

        preferred, other = [], []
        for node in cls._walk_nodes(content):
            node_type = cls._strings(node.get("@type") or node.get("type"))
            if any(cls._key_token(value).lower() == "chemicalentity"
                   for value in node_type):
                for key in ("iupac_name", "iupacName", "alternate_name",
                            "alternateName", "alternate_names", "alternateNames",
                            "name", "preferred_label", "preferredLabel", "title"):
                    value = node.get(key)
                    if value:
                        preferred.extend(chemical_values(value))
            elif any(cls._key_token(value).lower() == "substancesample"
                     for value in node_type):
                # The sample label (e.g. CRS-7615) is only a fallback after
                # chemical entity names, never the measurement dataset title.
                for key in ("alternate_name", "alternateName", "name", "title"):
                    value = node.get(key)
                    if value:
                        other.extend(chemical_values(value))
        # Some API revisions omit @type on nested chemical nodes.
        if not preferred:
            for value in cls._all_values(content, "iupac_name", "iupacName"):
                preferred.extend(chemical_values(value))
        result, seen = [], set()
        for value in preferred + other:
            value = value.strip()
            token = value.casefold()
            if value and token not in seen:
                result.append(value)
                seen.add(token)
        return result

    def _tags(self, content):
        values = []
        for key in ("keyword", "keywords", "theme", "topic", "measurement_technique"):
            values.extend(self._all_values(content, key))
        tags, seen = [], set()
        for value in values:
            label = self._text(value)
            if label:
                label = munge_tag(label[:100])
                if label and label not in seen:
                    tags.append({"name": label})
                    seen.add(label)
        return tags

    def _resources(self, content, title):
        resources, seen_urls = [], set()

        def direct_value(item, *keys):
            if not isinstance(item, dict):
                return None
            wanted = set(self._key_token(key) for key in keys)
            for key, value in item.items():
                if self._key_token(key) in wanted and value not in (None, "", [], {}):
                    return value
            return None

        def add_resource(item, default_name, default_format):
            if isinstance(item, str):
                url = item
                name = default_name
                fmt = default_format
            elif isinstance(item, dict):
                url = direct_value(item, "download_url", "downloadURL", "access_url",
                                   "accessURL", "url", "@id", "id", "value")
                if isinstance(url, dict):
                    url = direct_value(url, "@id", "id", "url", "value")
                name = self._text(direct_value(item, "title", "name")) or default_name
                fmt = self._text(direct_value(item, "encodingFormat", "mediaType",
                                              "mimeType", "format")) or default_format
            else:
                return
            if not url or not str(url).startswith(("http://", "https://")):
                return
            url = str(url)
            if fmt.startswith("image/"):
                fmt = fmt.split("/", 1)[1].upper()
                if fmt == "JPG":
                    fmt = "JPEG"
            elif fmt == "image":
                suffix = urlparse(url).path.rsplit(".", 1)
                if len(suffix) == 2 and suffix[1]:
                    fmt = suffix[1].upper()
                    if fmt == "JPG":
                        fmt = "JPEG"
            if url in seen_urls:
                return
            seen_urls.add(url)
            resources.append({"url": url, "name": name, "format": fmt})

        # DCAT distributions can contain links to the actual measurement files.
        for distribution in self._all_values(
                content, "distribution", "distributions", "dataset_distribution"):
            values = distribution if isinstance(distribution, list) else [distribution]
            for item in values:
                add_resource(item, title, "Unknown")

        # Preserve externally hosted depiction/thumbnail links as image
        # resources so CKAN image viewers can render them where supported.
        image_keys = ("image", "image_url", "imageUrl", "thumbnail",
                      "thumbnailUrl", "molecular_image", "chemical_structure_image",
                      "depiction", "foaf:img")
        for image_value in self._all_values(content, *image_keys):
            values = image_value if isinstance(image_value, list) else [image_value]
            for image_item in values:
                add_resource(image_item, title + " - chemical image", "image")

        if not resources:
            landing = self._value(content, "landing_page", "landingPage", "homepage")
            if isinstance(landing, dict):
                landing = landing.get("id") or landing.get("@id")
            if landing and str(landing).startswith(("http://", "https://")):
                resources.append({"url": landing, "name": title, "format": "HTML"})
        return resources
