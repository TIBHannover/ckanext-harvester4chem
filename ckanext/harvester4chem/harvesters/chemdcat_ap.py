"""CKAN harvester for Chemotion's ChemDCAT-AP API.

Source config:
{
  "publications_endpoint": "https://www.chemotion-repository.net/api/v1/public/publications",
  "metadata_endpoint": "https://www.chemotion-repository.net/api/v1/public/publications/{publication_id}/chemdcat_ap",
  "offset": 0,
  "limit": 100
}

Set the two endpoint URLs to the paths shown in the Chemotion Swagger UI.
The record endpoint must contain {publication_id}. The original profile JSON
is preserved in the CKAN package extra named chemdcat_ap.
"""
import json
import logging
import re
import traceback
from urllib.parse import parse_qs, quote, urlparse

import requests
from ckan import model
from ckan.lib.munge import munge_tag, munge_title_to_name
from ckan.lib.search import rebuild
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
        listing = config.get("publications_endpoint") or source.url
        template = config.get("metadata_endpoint")
        if not listing:
            raise ValueError("Set publications_endpoint or the harvest source URL")
        if not template or "{publication_id}" not in template:
            raise ValueError("metadata_endpoint must contain {publication_id}")
        return config, listing, template

    @staticmethod
    def _publication_id(item):
        if isinstance(item, dict):
            for key in ("publication_id", "publication-id", "publicationId",
                        "id", "identifier", "url", "@id"):
                if item.get(key) is not None:
                    result = ChemDCATAPHarvester._publication_id(item[key])
                    if result:
                        return result
            return None
        if isinstance(item, (int, float)):
            return str(int(item))
        if not isinstance(item, str) or not item.strip():
            return None
        value = item.strip()
        query = parse_qs(urlparse(value).query)
        for key in ("publication-id", "publication_id", "publicationId", "id"):
            if query.get(key):
                return query[key][0]
        match = re.search(r"/pid/([^/?#]+)", value)
        if match:
            return match.group(1)
        if value.startswith(("http://", "https://")):
            return urlparse(value).path.rstrip("/").rsplit("/", 1)[-1] or None
        return value

    @staticmethod
    def _records(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("publications", "data", "results", "items"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        raise ValueError("Listing response must contain a publication list")

    def gather_stage(self, harvest_job):
        try:
            config, endpoint, _ = self._config(harvest_job.source)
            offset = int(config.get("offset", 0))
            limit = int(config.get("limit", 100))
            timeout = float(config.get("timeout", 30))
            if offset < 0 or limit < 1:
                raise ValueError("offset must be >= 0 and limit must be > 0")
            list_params = config.get("list_params") or {}
            ids, seen = [], set()
            while True:
                params = dict(list_params)
                params.update({"offset": offset, "limit": limit})
                response = requests.get(endpoint, params=params, timeout=timeout,
                                        headers={"Accept": "application/json"})
                response.raise_for_status()
                records = self._records(response.json())
                if not records:
                    break
                for item in records:
                    publication_id = self._publication_id(item)
                    if publication_id and publication_id not in seen:
                        ids.append(publication_id)
                        seen.add(publication_id)
                if len(records) < limit:
                    break
                offset += limit
            result = []
            for publication_id in ids:
                obj = HarvestObject(guid=publication_id, job=harvest_job)
                obj.save()
                result.append(obj.id)
            log.info("Gathered %d Chemotion publication IDs", len(result))
            return result
        except Exception as exc:
            log.exception("ChemDCAT-AP gather failed")
            self._save_gather_error(
                "Could not gather publication IDs: %s / %s" %
                (exc, traceback.format_exc()), harvest_job)
            return None

    def fetch_stage(self, harvest_object):
        try:
            config, _, template = self._config(harvest_object.source)
            url = template.format(
                publication_id=quote(str(harvest_object.guid), safe=""))
            response = requests.get(
                url, timeout=float(config.get("timeout", 30)),
                headers={"Accept": "application/ld+json, application/json"})
            response.raise_for_status()
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
    def _value(content, *keys):
        for key in keys:
            value = content.get(key)
            if value not in (None, "", [], {}):
                return value
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

            title = self._text(self._value(content, "title", "name", "label"))
            title = title or "Chemotion publication %s" % harvest_object.guid
            identifier = self._text(self._value(content, "id", "@id", "identifier"))
            name = munge_title_to_name(identifier or str(harvest_object.guid))
            if not name:
                raise ValueError("Could not derive a CKAN package name")

            landing = self._value(content, "landing_page", "landingPage", "url")
            if isinstance(landing, dict):
                landing = landing.get("id") or landing.get("@id")
            package = {
                "name": name,
                "title": title,
                "notes": self._text(self._value(content, "description", "abstract")) or "",
                "url": self._text(landing) or "",
                "extras": [{"key": "chemdcat_ap",
                            "value": json.dumps(content, ensure_ascii=False)}],
                "tags": self._tags(content),
                "resources": self._resources(content, title),
            }
            context = {"model": model, "session": Session, "user": "harvest",
                       "ignore_auth": True}
            source = get_action("package_show")(
                context.copy(), {"id": harvest_object.source.id})
            package["owner_org"] = source.get("owner_org")

            license_value = self._text(self._value(content, "license", "license_id"))
            if license_value:
                for lic in get_action("license_list")(context.copy(), {}):
                    if license_value in (lic.get("id"), lic.get("url"), lic.get("title")):
                        package["license_id"] = lic["id"]
                        break
            publisher = self._value(content, "publisher", "maintainer")
            author = self._value(content, "creator", "author", "authors")
            modified = self._value(content, "modified", "metadata_modified",
                                   "date_modified", "dateModified")
            if publisher:
                package["maintainer"] = self._text(publisher)
            if author:
                package["author"] = self._text(author)
            if modified:
                package["metadata_modified"] = self._text(modified)

            self._create_or_update_package(package, harvest_object, "package_show")
            Session.commit()
            rebuild(package["name"])
            return True
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
            value = content.get(key)
            values.extend(value if isinstance(value, list) else ([value] if value else []))
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
        values = self._value(content, "distribution", "distributions")
        values = values if isinstance(values, list) else ([values] if values else [])
        resources = []
        for item in values:
            if isinstance(item, str):
                url, name, fmt = item, title, "HTML"
            elif isinstance(item, dict):
                url = self._value(item, "download_url", "downloadURL",
                                  "access_url", "accessURL", "url")
                if isinstance(url, dict):
                    url = url.get("id") or url.get("@id")
                name = self._text(self._value(item, "title", "name")) or title
                fmt = self._text(item.get("format")) or "Unknown"
            else:
                continue
            if url and str(url).startswith(("http://", "https://")):
                resources.append({"url": url, "name": name, "format": fmt})
        if not resources:
            landing = self._value(content, "landing_page", "landingPage", "url")
            if isinstance(landing, dict):
                landing = landing.get("id") or landing.get("@id")
            if landing and str(landing).startswith(("http://", "https://")):
                resources.append({"url": landing, "name": title, "format": "HTML"})
        return resources
