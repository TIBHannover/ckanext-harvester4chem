import json
from dateutil.parser import parse

import logging
import os.path
import random
from urllib.error import HTTPError
import traceback
from datetime import date
import requests

from ckan.model import Session
from ckan.logic import get_action
from ckan import model

from ckanext.harvester4chem.molecule_sync import synchronize_harvested_package

from ckanext.harvest.harvesters.base import HarvesterBase
from ckan.lib.munge import munge_tag
from ckan.lib.munge import munge_title_to_name
from ckan.lib.search import rebuild
from ckanext.harvest.model import HarvestObject

from rdkit.Chem import inchi
from rdkit.Chem import rdmolfiles
from rdkit.Chem import Draw
from rdkit.Chem import Descriptors

import requests

log = logging.getLogger(__name__)


class NMRxIVBioSchema(HarvesterBase):
    """
        To get the datasets from NMRXiv in the BioSchemas via Swagger API, we use this Harvester class.
        This Harvester provides IDs and dataset's metadata are attained by two different APIs.
     """

    def info(self):
        """
        Return information about this harvester.
        """
        return {
            "name": "nmrXiv Swagger Harvester",
            "title": "nmrXiv Swagger Harvester",
            "description": "Harvester for scrapping and harvesting metadata from nmrXiv Swagger API",
        }

    def gather_stage(self, harvest_job):
        """
        Gathers identifiers of each source, gathering them to the next stage for fetching metadata.
        This stage uses Swagger API provided by nmrXiv, to extract all the identifiers available in the nmrXiv database.

        ckan-ily they are converted harvest_object_ids, within the database of the CKAN.

        :param harvest_job: HarvestJob object
        :return: A list of HarvestObject ids for Fetch stage
        """

        log.debug("in gather stage: %s" % harvest_job.source.url)
        something_else = harvest_job.source.config
        harvest_obj_ids = []

        base_swagger_api = harvest_job.source.url

        try:
            log.debug(f" Config Content:  {json.loads(something_else)}")
            config_dict= json.loads(something_else)
            last_page_boolean = config_dict.get("get_last_page")
            get_last_page_boolean = last_page_boolean if last_page_boolean is not None else False
        except ValueError:
            get_last_page_boolean = False
            pass


        log.debug(f"{base_swagger_api} & Config value {get_last_page_boolean}")

        for identi in self._get_dataseturl(base_url=base_swagger_api, get_last_page_only=get_last_page_boolean):
            harvest_obj = HarvestObject(guid=identi, job=harvest_job)
            harvest_obj.save()
            harvest_obj_ids.append(harvest_obj.id)

            log.debug("Harvest obj %s created" %harvest_obj.id)

        log.debug("Gather stage successfully finished with %s harvest objects" % len(harvest_obj_ids))

        return harvest_obj_ids

    def fetch_stage(self, harvest_object):
        """
        TODO: Extend this information
        Fetch scrape-able text from the URL/ harvest job

        :return:
        """

        base_swagger_api = harvest_object.source.url

        try:

            log.debug("in fetch stage: %s" % harvest_object.guid)

            base_url = base_swagger_api + 'schemas/bioschemas'

            response = requests.get(base_url)
            response.raise_for_status()  # Raises an error for 4XX/5XX responses

            metadata_response = requests.get(f'{base_url}/{harvest_object.guid}')
            metadata_response.raise_for_status()
            metadata_data = metadata_response.json()

            content = json.dumps(metadata_data)

            harvest_object.content = content
            harvest_object.save()

            return True

        except requests.RequestException as e:
            print(f"Request failed: {e}")
            return False

    def import_stage(self, harvest_object):
        """
        To harvest data from api metadata to ckan database
        :param harvest_object: HarvestObject object
        :return: True if everything went well, False if errors were found
        """

        global created, modified, technique_measure
        if not harvest_object:
            log.error("No harvest object received")
            self._save_object_error("No harvest object received")
            return False

        try:
            # self._set_config(harvest_object.job.source.config)
            context = {
                "model": model,
                "session": Session,
                "user": 'harvest',
                "ignore_auth": True,
            }

            package_dict = {}

            content = json.loads(harvest_object.content)
            log.debug("in import stage %s" % harvest_object.guid)
            # log.debug(content) Occupying to much, space and time

            # get id
            package_dict["id"] = munge_title_to_name(harvest_object.guid)
            log.debug(f"Here is the package id saved {package_dict['id']}")

            package_dict['name'] = content['name']

            package_dict["title"] = content['name']
            package_dict['url'] = content['url']

            # add owner org
            source_dataset = get_action("package_show")(
                context.copy(), {"id": harvest_object.source.id}
            )
            owner_org = source_dataset.get("owner_org")
            package_dict["owner_org"] = owner_org

            # add resources
            package_dict["resources"] = self._extract_resources(content)
            package_dict["language"] = 'english'

            # add notes, license_id
            try:
                package_dict['notes'] = content['isPartOf']['description']
            except KeyError as e:
                log.exception(f'description not available {e}')
                package_dict['notes'] = ''
                pass
            try:
                package_dict["license_id"] = self._extract_license_id(context=context, content=content)
                log.debug(f'This is the license {package_dict["license_id"]}')
            except Exception as e:
                log.exception(f'License Error: {e}')
                pass

            self._extract_extras_image(package=package_dict, content_hasBioPart=content)

            # Chemical information by extracting BioChemEntity
            content_about = content['isPartOf']['about']
            content_hasBioPart = content_about['hasBioChemEntityPart'][0]
            try:
                package_dict['inchi'] = content_hasBioPart['inChI']
                package_dict['inchi_key'] = content_hasBioPart['inChIKey']
                smiles_a = content_hasBioPart['smiles']
                package_dict['smiles'] = next((item for item in smiles_a if item is not None), 'n/a')
                # package_dict['smiles'] = content_hasBioPart['smiles']
                package_dict['exactmass'] = content_hasBioPart['molecularWeight']
                package_dict['mol_formula'] = content_hasBioPart['molecularFormula']

            except KeyError as e:
                log.exception(f"Chemical Information Error: {e}")
                pass

            try:
                # measurement Technical Information
                technique_measure = content['measurementTechnique']

                if isinstance(technique_measure, dict):

                    technique = technique_measure['name']
                    package_dict['measurement_technique'] = technique
                    package_dict['measurement_technique_iri'] = technique_measure['@id']

                elif isinstance(technique_measure, str):
                    package_dict['measurement_technique'] = technique_measure

                else:
                    log.error("Measurement not available for package %s", package_dict['id'])

            except (KeyError, TypeError) as e:
                log.exception(f'TypeError or KeyError for MeasurementTechnique for ID{package_dict["id"]}: {str(e)}')
            pass

            try:
                # Obtaining DOI from @id of Content
                package_dict['doi'] = content['@id']
            except KeyError as e:
                log.exception(f'DOI not found {e}')
                pass

            try:
                # Date of metadata publication
                package_dict['metadata_published'] = content['datePublished']
            except KeyError as e:
                log.exception(f'Metadata date Published Error: {e}')

            # Author information
            try:
                double_dict_ispartof = content['isPartOf']['isPartOf']
                package_dict['metadata_published'] = double_dict_ispartof['datePublished']
                citation_author = double_dict_ispartof['citation']

                package_dict['author'] = citation_author[0]['author']

            except Exception as e:
                log.exception(f'Author/date_published Error {e}')
                pass

            package_dict['variableMeasured'] = self._extract_variable_measured(content=content)

            tags = self._extract_tags(content)
            package_dict['tags'] = tags

            # creating package
            log.debug("Create/update package using dict: %s" % package_dict)
            transaction = Session.begin_nested()
            package_result = self._create_or_update_package(
                package_dict, harvest_object, "package_show"
            )
            if not package_result:
                raise RuntimeError("CKAN package create/update failed")
            synchronize_harvested_package(
                package_dict, harvest_object,
                names=[content.get('name')],
                name_source="nmrXiv",
            )
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

    def _get_dataseturl(self, base_url, get_last_page_only=False):
        """
        :param base_url: receives url, which is a Swagger-API url of nmrXiv ONLY
        :param get_last_page_only: if True, fetches only the last page's data
        :return:
        """

        if base_url == 'https://nmrxiv.org/api/v1/':
            try:
                base_url_gather = base_url + 'list/datasets'

                # Initial request to get pagination details
                log.debug(f'Requesting for {base_url_gather}')

                response = requests.get(base_url_gather)
                response.raise_for_status()  # Raises an error for 4XX/5XX responses
                data = response.json()
                last_page = data['meta']['last_page']

                if get_last_page_only:
                    # Fetch only the last page
                    page_response = requests.get(f'{base_url_gather}?page={last_page}')
                    page_response.raise_for_status()  # Raises an error for bad responses
                    page_data = page_response.json()['data']

                    # Using list comprehension for cleaner code
                    last_page_identifiers = [dataset['identifier'] for dataset in page_data]
                    return last_page_identifiers

                else:
                    all_identifiers = []
                    for page_number in range(1, last_page + 1):
                        page_response = requests.get(f'{base_url_gather}?page={page_number}')
                        page_response.raise_for_status()  # Raises an error for bad responses
                        page_data = page_response.json()['data']

                        # Using list comprehension for cleaner code
                        all_identifiers.extend([dataset['identifier'] for dataset in page_data])

                    return all_identifiers

            except requests.RequestException as e:
                print(f"Request failed: {e}")
                return []

        else:
            return 'Error fetching API'

    def _get_mapping(self):
        return {
            "title": "name",
            "notes": "description",
            "url": "url",
            "metadata_modified": "dateModified",
            "metadata_created": "dateCreated",
        }

    def _extract_resources(self, content):
        resources = []
        url = content['url']
        log.debug("URL of resource: %s" % url)
        if url:
            try:
                resource_format = content["format"][0]
            except (IndexError, KeyError):
                resource_format = "HTML"
            resources.append(
                {
                    "name": content["name"],
                    "resource_type": resource_format,
                    "format": resource_format,
                    "url": url,
                }
            )
        return resources

    def _extract_tags(self, content):
        try:
            # Safely retrieve 'measurementTechnique' and ensure it's a dictionary
            technique_measure_tag = content.get('measurementTechnique', {})
            if not isinstance(technique_measure_tag, dict):
                raise TypeError("Expected 'measurementTechnique' to be a dictionary.")

            # Extract 'name' from 'technique_measure_tag', ensuring it exists and is a string
            technique = technique_measure_tag.get('name', '')
            if not isinstance(technique, str):
                raise TypeError("Expected 'name' to be a string.")

            # Prepare and return tags
            tags = [{"name": munge_tag(technique[:100])}]
            return tags

        except KeyError:
            log.exception('KeyError: Measurement Technique field is missing')
            return []

        except TypeError as e:
            log.exception(f'TypeError: {str(e)}')
            return []

    def _extract_variable_measured(self, content):

        variable_measured_package_list = []

        from curies import Converter as Converter

        converter_instance = Converter.from_prefix_map(
            {
                "CHEBI": "http://purl.obolibrary.org/obo/CHEBI_",
                "MONDO": "http://purl.obolibrary.org/obo/MONDO_",
                "GO": "http://purl.obolibrary.org/obo/GO_",
                "FIX": "http://purl.obolibrary.org/obo/FIX_",
                "OBI": "http://purl.obolibrary.org/obo/OBI_",
                "NCIT": "http://purl.obolibrary.org/obo/NCIT_",
                "CHMO": "http://purl.obolibrary.org/obo/CHMO_",
            }
        )

        for values in content.get('variableMeasured', []):  # Safe access to 'variableMeasured'
            variable_measured_dict = {
                'variableMeasured_name': str(values.get('name', '')),
                'variableMeasured_propertyID': str(values.get('propertyID', '')),
                'variableMeasured_value': str(values.get('value', '')),
                'variableMeasured_tsurl': converter_instance.expand(values.get('propertyID','')),
            }

            log.debug(f'Variable Measured: {variable_measured_dict}')

            variable_measured_package_list.append(variable_measured_dict)

        log.debug(f"All Variable Measured: {variable_measured_package_list}")

        return variable_measured_package_list

    def _extract_extras_image(self, package, content_hasBioPart):
        extras = []
        package_id = package['id']
        #

        try:
            content_about = content_hasBioPart['isPartOf']['about']
            content = content_about['hasBioChemEntityPart'][0]
            #
            standard_inchi = content['inChI']
            #
            inchi_key = content['inChIKey']


            if standard_inchi.startswith('InChI'):
                molecu = inchi.MolFromInchi(standard_inchi)
                log.debug("Molecule generated")
                try:
                    filepath = '/var/lib/ckan/default/storage/images/' + str(inchi_key) + '.png'
                    if os.path.isfile(filepath):
                        log.debug("Image Already exists")
                    else:
                        Draw.MolToFile(molecu, filepath)
                        log.debug("Molecule Image generated for %s", package_id)

                except Exception as e:
                    log.error(e)

            # extracting date metadata as extra data.
            try:
                if content['datePublished']:
                    published = content['datePublished']
                    date_value = parse(published)
                    date_without_tz = date_value.replace(tzinfo=None)
                    value = date_without_tz.isoformat()
                    extras.append({"key": "datePublished", "value": value})
                if content['dateCreated']:
                    created = content['dateCreated']
                    date_value = parse(created)
                    date_without_tz = date_value.replace(tzinfo=None)
                    value = date_without_tz.isoformat()
                    extras.append({"key": "dateCreated", "value": value})
                if content['dateModified']:
                    modified = content['dateModified']
                    date_value = parse(modified)
                    date_without_tz = date_value.replace(tzinfo=None)
                    value = date_without_tz.isoformat()
                    extras.append({"key": "dateModified", "value": value})
            except Exception:
                pass

            log.debug(f"Data saved to extras {extras}")
            # return extras
        except Exception as e:
            log.error(f"Error extracting extras: {e}")
            pass

        return None

    def _extract_license_id(self, context, content):
        package_license = None
        content_license = content['license']
        license_list = get_action('license_list')(context.copy(), {})
        for license_name in license_list:

            if content_license == license_name['id'] or content_license == license_name['url'] or content_license == \
                    license_name['title']:
                package_license = license_name['id']

        return package_license
