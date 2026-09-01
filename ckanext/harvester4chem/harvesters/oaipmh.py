import logging
import json
import re
from urllib.error import HTTPError
import traceback
import os
from datetime import datetime
from datetime import timedelta

from ckan.model import Session
from ckan.logic import get_action
from ckan import model

from ckanext.harvest.harvesters.base import HarvesterBase
from ckan.lib.munge import munge_tag
from ckan.lib.munge import munge_title_to_name
from ckan.lib.search import rebuild
from ckanext.harvest.model import HarvestObject, UPDATE_FREQUENCIES

import oaipmh.client
from oaipmh.client import Client
from oaipmh.metadata import MetadataRegistry

from ckanext.harvester4chem.harvesters.metadata import oai_ddi_reader
from ckanext.harvester4chem.harvesters.metadata import oai_dc_reader
from ckanext.harvester4chem.harvesters.metadata import oai_datacite_reader

from rdkit.Chem import inchi
from rdkit.Chem import rdmolfiles
from rdkit.Chem import Draw
from rdkit.Chem import Descriptors
from rdkit.Chem import rdMolDescriptors

from ckanext.harvester4chem.molecule_sync import synchronize_harvested_package
from ckanext.related_resources.models.related_resources import RelatedResources as related_resources

log = logging.getLogger(__name__)

class OaipmhHarvester(HarvesterBase):
    """
    OAI-PMH Harvester for DataCite Metadata and repositories!

    """

    # TODO: Check weather vaild or not!

    def info(self):
        """
        Return information about this harvester.
        """
        return {
            "name": "oai_pmh",
            "title": "DataCite OAI Harvester",
            "description": "Harvester for OAI-PMH DataCite metadata ",
        }

    def gather_stage(self, harvest_job):
        """
        The gather stage will recieve a HarvestJob object and will be
        responsible for:
            - gathering all the necessary objects to fetch on a later.
              stage (e.g. for a CSW server, perform a GetRecords request)
            - creating the necessary HarvestObjects in the database, specifying
              the guid and a reference to its source and job.
            - creating and storing any suitable HarvestGatherErrors that may
              occur.
            - returning a list with all the ids of the created HarvestObjects.

        :param harvest_job: HarvestJob object
        :returns: A list of HarvestObject ids
        """
        log.debug("in gather stage: %s" % harvest_job.source.url)
        log.debug("with updating frequency: %s" % harvest_job.source.frequency)
        log.debug("This is strictly OAI-PMH ")

        try:
            harvest_obj_ids = []
            registry = self._create_metadata_registry()
            self._set_config(harvest_job.source.config, harvest_job.source.frequency)
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
                harvest_obj.save()
                harvest_obj_ids.append(harvest_obj.id)
                log.debug("Harvest obj %s created" % harvest_obj.id)

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
            pass

        except Exception as e:
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
            pass

        log.debug(
            "Gather stage successfully finished with %s harvest objects"
            % len(harvest_obj_ids)
        )
        return harvest_obj_ids

    def _identifier_generator(self, client):
        """
        pyoai generates the URL based on the given method parameters
        Therefore one may not use the set parameter if it is not there
        """

        if self.set_from or self.set_until:
            for header in client.listIdentifiers(metadataPrefix=self.md_format, set=self.set_spec,
                                                 from_=datetime.strptime(self.set_from, "%Y-%m-%dT%H:%M:%SZ"),
                                                 until=datetime.strptime(self.set_until, "%Y-%m-%dT%H:%M:%SZ")):
                yield header

        elif self.set_spec:
            for header in client.listIdentifiers(metadataPrefix=self.md_format, set=self.set_spec,
                                                 from_=datetime.strptime(self.set_from, "%Y-%m-%dT%H:%M:%SZ"),
                                                 until=datetime.strptime(self.set_until, "%Y-%m-%dT%H:%M:%SZ")):
                yield header

        else:
            for header in client.listIdentifiers(
                    metadataPrefix=self.md_format
            ):
                yield header

    def _create_metadata_registry(self, ):
        registry = MetadataRegistry()
        registry.registerReader("oai_dc", oai_dc_reader)
        registry.registerReader("oai_ddi", oai_ddi_reader)
        registry.registerReader("oai_datacite", oai_datacite_reader)
        return registry

    def _set_config(self, source_config, frequency):

        """
        Configuration from GUI is being added here.
        This function also checks which frequency with which harvest process would be proceeded.

        It also checks weather the date and time mentioned and acts accordingly
        """
        now = datetime.now()
        default = now - timedelta(days=180)
        daily = now - timedelta(days=1)
        weekly = now - timedelta(days=5)
        monthly = now - timedelta(days=30)
        biweekly = now - timedelta(days=14)

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

            if frequency == 'DAILY':
                self.set_from = config_json.get("from", str(daily.strftime("%Y-%m-%dT%H:%M:%SZ")))

            elif frequency == 'WEEKLY':
                self.set_from = config_json.get("from", str(weekly.strftime("%Y-%m-%dT%H:%M:%SZ")))

            elif frequency == 'MONTHLY':
                self.set_from = config_json.get("from", str(monthly.strftime("%Y-%m-%dT%H:%M:%SZ")))

            elif frequency == 'BIWEEKLY':
                self.set_from = config_json.get("from", str(biweekly.strftime("%Y-%m-%dT%H:%M:%SZ")))

            else:
                self.set_from = config_json.get("from", str(default.strftime("%Y-%m-%dT%H:%M:%SZ")))

            self.set_until = config_json.get("until", str(now.strftime("%Y-%m-%dT%H:%M:%SZ")))
            log.debug(f"passed from {self.set_from}")
            log.debug(f"passed  until {self.set_until}")

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
        log.debug("in fetch stage: %s" % harvest_object.guid)
        try:
            self._set_config(harvest_object.job.source.config, harvest_object.job.source.frequency)
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

            try:
                content_dict = metadata.getMap()
                content_dict["set_spec"] = header.setSpec()
                if metadata_modified:
                    content_dict["metadata_modified"] = metadata_modified
                log.debug(content_dict)
                content = json.dumps(content_dict)
            except:
                log.exception("Dumping the metadata failed!")
                self._save_object_error(
                    "Dumping the metadata failed!", harvest_object
                )
                return False

            harvest_object.content = content
            harvest_object.save()
        except Exception as e:
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
            self._set_config(harvest_object.job.source.config, harvest_object.job.source.frequency)
            context = {
                "model": model,
                "session": Session,
                "user": self.user,
                "ignore_auth": True,
            }

            package_dict = {}
            content = json.loads(harvest_object.content)
            log.debug(content)

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
            package_dict["license_id"] = self._extract_license_id(context=context, content=content)

            # add resources
            url = self._get_possible_resource(harvest_object, content)
            package_dict["resources"] = self._extract_resources(url, content)

            # extract tags from 'type' and 'subject' field
            # everything else is added as extra field
            tags, extras, related_resources = self._extract_tags_and_extras(content)
            package_dict["tags"] = tags
            package_dict["extras"] = extras

            # create smiles code form inchi & add to extras table
            try:
                package_dict["inchi"] = content['inchi'][0]
                smiles, inchi_key, exact_mass, mol_formula = self._get_chemical_info(package_dict, content)
                package_dict["inchi_key"] = inchi_key
                package_dict["smiles"] = smiles
                package_dict["exact_mass"] = exact_mass
                package_dict["mol_formula"] = mol_formula

            except Exception as e:
                log.error(f"Failed to {e}")
                pass

            # groups aka projects
            groups = []

            # create group based on set
            if content["set_spec"]:
                log.debug("set_spec: %s" % content["set_spec"])
                groups.extend(
                    {"id": group_id}
                    for group_id in self._find_or_create_groups(
                        content["set_spec"], context.copy()
                    )
                )

            # add groups from content
            groups.extend(
                {"id": group_id}
                for group_id in self._extract_groups(content, context.copy())
            )

            package_dict["groups"] = groups

            # allow sub-classes to add additional fields
            package_dict = self._extract_additional_fields(
                content, package_dict
            )

            log.debug("Create/update package using dict: %s" % package_dict)
            transaction = Session.begin_nested()
            package_result = self._create_or_update_package(
                package_dict, harvest_object, "package_show"
            )
            if not package_result:
                raise RuntimeError("CKAN package create/update failed")
            synchronize_harvested_package(
                package_dict, harvest_object,
                names=content.get('title'),
                name_source="OAI-PMH",
            )
            self._save_relationships_to_db(package_dict, content)
            rebuild(package_dict["name"])
            if transaction.is_active:
                transaction.commit()
            Session.commit()

            log.debug("Finished record")

        except Exception as e:
            Session.rollback()
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
            "url": "source",
            "language": "language",
            "metadata_modified": "metadata_modified",
            "inchi": "inchi"

        }

    def _extract_author(self, content):
        return ", ".join(content["creator"])

    def _extract_license_id(self, context, content):
        package_license = None
        content_license = ", ".join(content["rights"])
        license_list = get_action('license_list')(context.copy(), {})
        for license_name in license_list:
            if content_license == license_name['id'] or content_license == license_name['url'] or content_license == \
                    license_name['title']:
                package_license = license_name['id']

        return package_license

    def _extract_tags_and_extras(self, content):
        extras = []
        tags = []
        related_resources = []

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
                # To harvest related and relationType without raising any exceptions
                if key == 'relation' or key == 'relationType':
                    try:
                        value = value
                    except Exception as e:
                        log.debug(f'Exception in _extract_tags_and_extras {e}')
                        pass
                else:
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

        tag_tech = self._extract_measuring_tech(content)
        if tag_tech:
            tags.extend(tag_tech)
        tags = [{"name": munge_tag(tag[:100])} for tag in tags]

        return (tags, extras, related_resources)

    def _get_possible_resource(self, harvest_obj, content):
        url = None
        candidates = content["identifier"]
        candidates.append(harvest_obj.guid)
        for ident in candidates:
            if ident.startswith("http://") or ident.startswith("https://"):
                url = ident
            elif ident.startswith("10."):
                url = "https://doi.org/" + ident
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

    def _extract_groups(self, content, context):
        if "series" in content and len(content["series"]) > 0:
            return self._find_or_create_groups(content["series"], context)
        return []

    def _extract_additional_fields(self, content, package_dict):
        # This method is the ideal place for sub-classes to
        # change whatever they want in the package_dict
        return package_dict

    def _find_or_create_groups(self, groups, context):
        log.debug("Group names: %s" % groups)
        group_ids = []
        try:
            for group_name in groups:
                data_dict = {
                    "id": group_name,
                    "name": munge_title_to_name(group_name),
                    "title": group_name,
                }
                try:
                    group = get_action("group_show")(context.copy(), data_dict)
                    log.info("found the group " + group["id"])
                except:
                    group = get_action("group_create")(context.copy(), data_dict)
                    log.info("created the group " + group["id"])
                group_ids.append(group["id"])

        except Exception as e:
            log.error(f"Failed here {e}. Need to be addressed ")

            log.debug("Group ids: %s" % group_ids)
        return group_ids

    # NFDI4Chem extensions for storing chemical data in respective tables

    def _get_chemical_info(self, package, content):

        """ function to convert InChI code to smiles code.
        This uses rdkit library to  convert available InChI to SMILES. (Chemo-informatics library)

        Database Table has been generated for molecule data.
        We use psycopg2 to connect to database and INSERT data using SQL query

        """
        # global values
        smiles = None
        inchi_key = None
        exact_mass = None
        mol_formula = None
        standard_inchi = content["inchi"]
        package_id = package['id']

        for inchi_code in standard_inchi:
            if inchi_code.startswith('InChI'):
                molecule = inchi.MolFromInchi(inchi_code)
                smiles = rdmolfiles.MolToSmiles(molecule)
                inchi_key = inchi.InchiToInchiKey(inchi_code)
                exact_mass = Descriptors.MolWt(molecule)
                mol_formula = rdMolDescriptors.CalcMolFormula(molecule)

                # upload images to folder
                try:
                    filepath = '/var/lib/ckan/default/storage/images/' + str(inchi_key) + '.png'
                    if os.path.isfile(filepath):
                        log.debug("Image Already exists")
                    else:
                        Draw.MolToFile(molecule, filepath)
                    log.debug("Molecule Image generated for %s", package_id)
                except Exception as e:
                    log.error(e)

        log.debug("Moleculer Data loaded for %s", package['id'])
        log.debug(f"Molecular Formula {mol_formula}")

        return smiles, inchi_key, exact_mass, mol_formula

    def _save_relationships_to_db(self, package, content):

        """ Database Table have been generated for storing related resources
        We connect to database and send those values directly  from harvested metadata
        Molecule data is also sent from this function, storing into molecule_data table
        Sending into DB pythonically. """

        package_id = package['id']
        relation_id = content['relation']
        relationType = content['relationType']
        relationIdType = content['relationIdType']
        values = list(self.yield_func(package_id, relation_id, relationType, relationIdType))
        for value in values:
            related_resources.create(*value, None)
        return "Related resources sent to database"

    def _extract_measuring_tech(self, content):

        tag_names = None
        package_title = str(content['title'])

        # mass spectrometry
        mass_Exp = re.compile(r'Mass')
        mass_exp = re.compile(r'mass')
        hnmr_exp = re.compile(r'1H NMR')
        cnmr_exp = re.compile(r'13C NMR')
        ir_exp = re.compile(r'IR')
        uv_exp = re.compile(r'UV')

        if mass_exp.search(package_title) or mass_Exp.search(package_title):
            tag_names = ['mass-spectrometry']
            return tag_names

        if hnmr_exp.search(package_title):
            tag_names = ['1H-NMR']
            return tag_names

        if cnmr_exp.search(package_title):
            tag_names = ['13C-NMR']
            return tag_names

        if ir_exp.search(package_title):
            tag_names = ['IR']
            return tag_names

        if uv_exp.search(package_title):
            tag_names = ['UV']
            return tag_names

        else:
            return None
        # tag_name = [{"name": munge_tag(tag[:100])} for tag in tag_names]

    def yield_func(self, package_id, relation_id, relationType, relationIdType):
        # An yield function to return generator list values to make a single list of values

        for p, q, r in zip(relation_id, relationType, relationIdType):
            value = (package_id, p, q, r)
            yield value
