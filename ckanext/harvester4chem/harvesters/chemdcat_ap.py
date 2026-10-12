"""CKAN harvester for Chemotion's ChemDCAT-AP API (v5)."""
import json
import logging
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

log = logging.getLogger(__name__)


class ChemDCATAPHarvester(HarvesterBase):
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
            return "inchikey", query["inchikey"][0]
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
        values = cls._all_values(content, "license", "license_id", "rights",
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
            image_url = next((resource["url"] for resource in resources
                              if "chemical image" in resource.get("name", "").lower()), None)
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
                "image_url": image_url,
            }
            measurement_data = self._measurement_data(content)
            if measurement_data:
                extra_values["chemdcat_measurement_data"] = json.dumps(
                    measurement_data, ensure_ascii=False)
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
                    extras.append({"key": key, "value": str(value)})

            # CKAN harvest's base helper requires a stable remote dataset id.
            # Use the source + gathered GUID so every rerun updates this record.
            remote_key = "%s:%s" % (harvest_object.source.id, harvest_object.guid)
            package_id = str(uuid.uuid5(uuid.NAMESPACE_URL, remote_key))
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
                        "molecular_formula", "iupac_name", "doi", "image_url"):
                value = extra_values.get(key)
                if value not in (None, "", [], {}):
                    package[key] = value
            context = {"model": model, "session": Session, "user": "harvest",
                       "ignore_auth": True}
            source = get_action("package_show")(
                context.copy(), {"id": harvest_object.source.id})
            package["owner_org"] = source.get("owner_org")

            if license_candidates:
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

            result = self._create_or_update_package(
                package, harvest_object, "package_show")
            if result is None or result is False:
                return False
            # _create_or_update_package returns True for a change and
            # 'unchanged' when the existing record already matches.
            return result
        except Exception as exc:
            Session.rollback()
            log.exception("ChemDCAT-AP import failed for %s", harvest_object.guid)
            self._save_object_error(
                "Import failed for %s: %s / %s" %
                (harvest_object.guid, exc, traceback.format_exc()), harvest_object)
            return False

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

        def add_resource(item, default_name, default_format):
            if isinstance(item, str):
                url = item
                name = default_name
                fmt = default_format
            elif isinstance(item, dict):
                url = self._value(item, "download_url", "downloadURL", "access_url",
                                  "accessURL", "url", "@id", "id", "value")
                if isinstance(url, dict):
                    url = self._value(url, "@id", "id", "url", "value")
                name = self._text(self._value(item, "title", "name")) or default_name
                fmt = self._text(self._value(item, "encodingFormat", "mediaType",
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
        for distribution in self._all_values(content, "distribution", "distributions"):
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
