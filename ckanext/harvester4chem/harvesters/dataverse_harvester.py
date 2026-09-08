import hashlib
import logging
import uuid
from urllib.request import urlopen
import traceback
from collections import Counter

from ckan import logic
from ckan import model
from ckan import plugins as p
from ckan.common import config
from ckan.model import Session
from ckan.logic import get_action
from ckanext.harvester4chem.license_utils import apply_license


from ckan.plugins.core import SingletonPlugin, implements

from ckanext.harvest.interfaces import IHarvester
from ckanext.harvest.harvesters.base import HarvesterBase
from ckanext.harvest.model import HarvestObject
from ckanext.harvest.model import HarvestObjectExtra as HOExtra

from ckan.lib.munge import munge_tag
from ckan.lib.munge import munge_title_to_name
from ckan.lib.search import rebuild


from ckan.lib.search.index import PackageSearchIndex
from ckan.lib.helpers import json
from ckan.lib.navl.validators import not_empty


import oaipmh.client
from oaipmh.metadata import MetadataRegistry

from ckanext.oaipmh.metadata import oai_ddi_reader
from ckanext.oaipmh.metadata import oai_dc_reader

log = logging.getLogger(__name__)


class DataVerseHarvester(HarvesterBase, SingletonPlugin):
    '''
    Harvester per Dataverse
    GATHER: makes a request to the index service and saves each entry in a HarvestObject
    FETCH: read the HarvestObject, retrieve the metadata, update the content of the HarvestObject by adding the newly uploaded metadata
    IMPORT: parses the HarvestObject and creates / updates the corresponding dataset
    '''

    def info(self):
        """
         Return information about this harvester.
         """
        return {
         "name": "Dataverse Harvester",
         "title": "Dataverse Harvester",
         "description": "Harvester for Dataverse",
        }

    def harvester_name(self):
        return "Dataverse Harvester"

    def gather_stage(self, harvest_job):
        """
        The gather stage will receive a HarvestJob object and will be
        responsible for:
            - gathering all the necessary objects to fetch on a later.
              Stage (e.g. for a CSW server, perform a GetRecords request)
            - creating the necessary HarvestObjects in the database, specifying
              the guid and a reference to its source and job.
            - creating and storing any suitable HarvestGatherErrors that may
              occur.
            - returning a list with all the ids of the created HarvestObjects.
        :param harvest_job: HarvestJob object
        :returns: A list of HarvestObject ids
        """
        log.debug("in gather stage: %s" % harvest_job.source.url)
        try:
            harvest_obj_ids = []
            registry = self._create_metadata_registry()
            self._set_config(harvest_job.source.config)
            client = oaipmh.client.Client(
                harvest_job.source.url,
                registry,
                self.credentials,
                force_http_get=self.force_http_get,
            )

            client.identify()  # check if identify works
            for header in self._identifier_generator(client):
                harvest_obj = HarvestObject(
                    guid=header.identifier(), job=harvest_job
                )
                # TODO: drop
                # if harvest_obj.guid != '10.14272/VIZKKYMOUGQEKZ-UHFFFAOYSA-L.1':
                # continue
                harvest_obj.save()
                harvest_obj_ids.append(harvest_obj.id)
                log.debug("Harvest obj %s created" % harvest_obj.id)
                # TODO: drop
                # return harvest_obj_ids
        except (HTTPError) as e:
            log.exception(
                "Gather stage failed on %s (%s): %s, %s"
                % (harvest_job.source.url, e.fp.read(), e.reason, e.hdrs)
            )
            self._save_gather_error(
                "Could not gather anything from %s" % harvest_job.source.url,
                harvest_job,
            )
            return None
        except (Exception) as e:
            log.exception(
                "Gather stage failed on %s: %s"
                % (
                    harvest_job.source.url,
                    str(e),
                )
            )
            self._save_gather_error(
                "Could not gather anything from %s: %s / %s"
                % (harvest_job.source.url, str(e), traceback.format_exc()),
                harvest_job,
            )
            return None
        log.debug(
            "Gather stage successfully finished with %s harvest objects"
            % len(harvest_obj_ids)
        )
        return harvest_obj_ids

    def _identifier_generator(self, client):
        """
        pyoai generates the URL based on the given method parameters;
        Therefore, one may not use the set parameter if it is not there
        """
        if self.set_spec:
            for header in client.listIdentifiers(
                    metadataPrefix=self.md_format, set=self.set_spec
            ):
                yield header
        else:
            for header in client.listIdentifiers(
                    metadataPrefix=self.md_format
            ):
                yield header

    def _create_metadata_registry(self):
        registry = MetadataRegistry()
        registry.registerReader("oai_dc", oai_dc_reader)
        registry.registerReader("oai_ddi", oai_ddi_reader)
        return registry

    def _set_config(self, source_config):
        try:
            config_json = json.loads(source_config)
            log.debug("config_json: %s" % config_json)
            try:
                username = config_json["username"]
                password = config_json["password"]
                self.credentials = (username, password)
            except (IndexError, KeyError):
                self.credentials = None

            self.user = "harvest"
            self.set_spec = config_json.get("set", None)
            self.md_format = config_json.get("metadata_prefix", "oai_dc")
            self.force_http_get = config_json.get("force_http_get", False)

        except ValueError:
            pass

    def fetch_stage(self, harvest_object):
        """
        The fetch stage will receive a HarvestObject object and will be
        responsible for:
            - getting the contents of the remote object (e.g. for a CSW server,
              perform a GetRecordById request).
            - saving the content in the provided HarvestObject.
            - creating and storing any suitable HarvestObjectErrors that may
              occur.
            - returning True if everything went as expected, False otherwise.
        :param harvest_object: HarvestObject object
        :returns: True if everything went right, False if errors were found
        """
        content = None
        log.debug("in fetch stage: %s" % harvest_object.guid)
        try:
            self._set_config(harvest_object.job.source.config)
            registry = self._create_metadata_registry()
            client = oaipmh.client.Client(
                harvest_object.job.source.url,
                registry,
                self.credentials,
                force_http_get=self.force_http_get,
            )
            record = None
            try:
                log.debug(
                    "Load %s with metadata prefix '%s'"
                    % (harvest_object.guid, self.md_format)
                )

                self._before_record_fetch(harvest_object)

                record = client.getRecord(
                    identifier=harvest_object.guid,
                    metadataPrefix=self.md_format,
                )
                self._after_record_fetch(record)
                log.debug("record found!")
            except:
                log.exception("getRecord failed for %s" % harvest_object.guid)
                self._save_object_error(
                    "Get record failed for %s!" % harvest_object.guid,
                    harvest_object,
                )
                return False

            header, metadata, _ = record

            try:
                metadata_modified = header.datestamp().isoformat()
            except:
                metadata_modified = None
                
            #log.debug("Header data elemt  %s", header.element() )
            #log.debug("metadata  subject %s" ,metadata.getMap())

            """ To Fetch only chemistry metadata from Dublin Core Subject. We fetch everything from using OAI-DC, then search for the subject.
            If _Chemistry_ inside the array has an hit, then harvest only that DOI's metadata """

            # TODO: What the hell is this? Why NONEType?

            if metadata:
                content_dict = metadata.getMap()
                log.debug("Subject are %s ", content_dict['subject'])
            else:
                return False

            for subject in content_dict['subject']:
                try:
                    if subject == 'Chemistry':
                        content_dict["set_spec"] = header.setSpec()
                        if metadata_modified:
                            content_dict["metadata_modified"] = metadata_modified
                        log.debug(content_dict)
                        content = json.dumps(content_dict)
                        harvest_object.content = content
                        harvest_object.save()
                        log.debug("Only Chemistry metadata is dumped")
                        break

                    else:
                        log.info("Not chemistry metadata for %s", harvest_object.guid)
                        return False

                except:
                        log.exception("Dumping the metadata failed!")
                        self._save_object_error(
                            "Dumping the metadata failed!", harvest_object
                        )
                        return False


        except (Exception) as e:
            log.exception(e)
            self._save_object_error(
                "Exception in fetch stage for %s: %r / %s"
                % (harvest_object.guid, e, traceback.format_exc()),
                harvest_object,
            )
            return False

        return True

    def _before_record_fetch(self, harvest_object):
        pass

    def _after_record_fetch(self, record):
        pass

    def import_stage(self, harvest_object):
        """
        The import stage will receive a HarvestObject object and will be
        responsible for:
            - performing any necessary action with the fetched object (e.g
              create a CKAN package).
              Note: if this stage creates or updates a package, a reference
              to the package must be added to the HarvestObject.
              Additionally, the HarvestObject must be flagged as current.
            - creating the HarvestObject - Package relation (if necessary)
            - creating and storing any suitable HarvestObjectErrors that may
              occur.
            - returning True if everything went as expected, False otherwise.
        :param harvest_object: HarvestObject object
        :returns: True if everything went right, False if errors were found
        """

        log.debug("in import stage: %s" % harvest_object.guid)
        if not harvest_object:
            log.error("No harvest object received")
            self._save_object_error("No harvest object received")
            return False

        try:
            self._set_config(harvest_object.job.source.config)
            context = {
                "model": model,
                "session": Session,
                "user": self.user,
                "ignore_auth": True,
            }

            package_dict = {}
            content = json.loads(harvest_object.content)

            package_dict["id"] = munge_title_to_name(harvest_object.guid)
            package_dict["name"] = package_dict["id"]

            mapping = self._get_mapping()
            for ckan_field, oai_field in mapping.items():
                try:
                    package_dict[ckan_field] = content[oai_field][0]
                except (IndexError, KeyError):
                    continue

            # add author
            package_dict["author"] = self._extract_author(content)

            # add owner_org
            source_dataset = get_action("package_show")(
                context.copy(), {"id": harvest_object.source.id}
            )
            owner_org = source_dataset.get("owner_org")
            package_dict["owner_org"] = owner_org

            # add license
            apply_license(package_dict, content.get("rights"), context)

            # add resources
            url = self._get_possible_resource(harvest_object, content)
            package_dict["url"] = url
            package_dict["identifier"] = content['identifier']
            package_dict["resources"] = self._extract_resources(url, content)

            # extract tags from 'type' and 'subject' field
            # everything else is added as extra field
            tags, extras = self._extract_tags_and_extras(content)
            package_dict["tags"] = tags
            package_dict["extras"] = extras

            # groups aka projects are empty, as we are not dealing with them. as only Chemistry data is being harvested
            groups = []

            # allow subclasses to add additional fields
            package_dict = self._extract_additional_fields(
                content, package_dict
            )

            log.debug("Create/update package using dict: %s" % package_dict)

            # Force update Package
            try:
                existing = get_action("package_show")(context, {'id': package_dict['id']})
                if existing.get('doi'):
                    get_action('package_update')(context, package_dict)
                    log.info(f"{package_dict['name']} is Force Updated")

                else:
                    self._create_or_update_package(
                        package_dict, harvest_object, "package_show"
                    )
                    rebuild(package_dict["name"])
                    Session.commit()
            except Exception as e:
                log.error(f"an error for using package_show {e}")
                self._create_or_update_package(
                    package_dict, harvest_object, "package_show"
                )
                rebuild(package_dict["name"])
                Session.commit()

            log.debug("Finished record")
        except (Exception) as e:
            log.exception(e)
            self._save_object_error(
                "Exception in fetch stage for %s: %r / %s"
                % (harvest_object.guid, e, traceback.format_exc()),
                harvest_object,
            )
            return False
        return True

    def _get_mapping(self):
        return {
            "title": "title",
            "notes": "description",
            "maintainer": "publisher",
            "maintainer_email": "maintainer_email",
            "url": "url",
            "language": "language",
            "metadata_modified" : "metadata_modified",
            "author":"creator",
            "doi" : "identifier"
        }

    def _extract_author(self, content):
        return ", ".join(content["creator"])

    def _extract_tags_and_extras(self, content):
        extras = []
        tags = []
        for key, value in content.items():
            if key in self._get_mapping().values():
                continue
            if key in ["type", "subject"]:
                if type(value) is list:
                    tags.extend(value)
                else:
                    tags.extend(value.split(";"))
                continue
            if value and type(value) is list:
                value = value[0]
            if not value:
                value = None
            if key.endswith("date") and value:
                # the ckan indexer can't handle timezone-aware datetime objects
                try:
                    from dateutil.parser import parse

                    date_value = parse(value)
                    date_without_tz = date_value.replace(tzinfo=None)
                    value = date_without_tz.isoformat()
                except (ValueError, TypeError):
                    continue
            extras.append({"key": key, "value": value})

        tags = [{"name": munge_tag(tag[:100])} for tag in tags]

        return (tags, extras)

    def _get_possible_resource(self, harvest_obj, content):
        url = None
        candidates = content["identifier"]
        candidates.append(harvest_obj.guid)
        for ident in candidates:
            if ident.startswith("http://") or ident.startswith("https://"):
                url = ident
                break
        return url

    def _extract_resources(self, url, content):
        resources = []
        log.debug("URL of resource: %s" % url)
        if url:
            try:
                resource_format = content["format"][0]
            except (IndexError, KeyError):
                resource_format = "HTML"
            resources.append(
                {
                    "name": content["title"][0],
                    "resource_type": resource_format,
                    "format": resource_format,
                    "url": url,
                }
            )
        return resources

    # def _extract_groups(self, content, context):
    #     if "series" in content and len(content["series"]) > 0:
    #         return self._find_or_create_groups(content["series"], context)
    #     return []

    def _extract_additional_fields(self, content, package_dict):
        # This method is the ideal place for sub-classes to
        # change whatever they want in the package_dict
        return package_dict

    # def _find_or_create_groups(self, groups, context):
    #     log.debug("Group names: %s" % groups)
    #     group_ids = []
    #     for group_name in groups:
    #         data_dict = {
    #             "id": group_name,
    #             "name": munge_title_to_name(group_name),
    #             "title": group_name,
    #         }
    #         try:
    #             group = get_action("group_show")(context.copy(), data_dict)
    #             log.info("found the group " + group["id"])
    #         except:
    #             group = get_action("group_create")(context.copy(), data_dict)
    #             log.info("created the group " + group["id"])
    #         group_ids.append(group["id"])
    #
    #     log.debug("Group ids: %s" % group_ids)
    #     return group_ids

    #def _get_json_content(self, identifiers):
