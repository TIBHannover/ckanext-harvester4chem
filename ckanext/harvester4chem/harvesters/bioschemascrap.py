import ast
import datetime
import json
from dateutil.parser import parse

import logging
import os.path
import random
from urllib.error import HTTPError
import traceback
from datetime import date

from ckan.model import Session
from ckan.logic import get_action
from ckan import model

from ckanext.related_resources.models.related_resources import RelatedResources as related_resources
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

import urllib.request
from bs4 import BeautifulSoup
import requests
import re

log = logging.getLogger(__name__)


class BioSchemaMUHarvester(HarvesterBase):
    """ Trying use this extension to scarp data from available sitemap URLs.
    In this extension we map scrapped data with CKAN and Bioschema.org from Massbank (Only through scrapping) """

    def info(self):
        """
        Return information about this harvester.
        """
        return {
            "name": "Bioschema Sitemap ",
            "title": "Bioschema Scraper/Harvester",
            "description": "Harvester for scrapping and harvesting metadata from BioSchema.org. "
                           "(This harvester will be deprecated)",
        }

    def gather_stage(self, harvest_job):
        """
        Gather is to gather
        :param harvest_job: HarvestJob object
        :return: A list of HarvestObject ids
        """

        log.debug("in gather stage: %s" % harvest_job.source.url)
        harvest_obj_ids = []

        sitemaps = harvest_job.source.url
        log.debug("%s" % sitemaps)
        for identi in self._get_dataseturl(url=sitemaps):
            harvest_obj = HarvestObject(guid=identi, job=harvest_job)
            harvest_obj.save()
            harvest_obj_ids.append(harvest_obj.id)
            log.debug("Harvest obj %s created" % harvest_obj.id)

        log.debug("Gather stage successfully finished with %s harvest objects" % len(harvest_obj_ids))
        return harvest_obj_ids



    def fetch_stage(self, harvest_object):
        """
        Fetch scrapable text from the URL/ harvest job

        :return:
        """

        global datasetDict, molecularDict

        try:
            log.debug("in fetch stage: %s" % harvest_object.guid)

            the_id = harvest_object.guid

            referenceurl = "https://massbank.eu/MassBank/RecordDisplay?id="
            dataset_url = referenceurl + the_id

            r = requests.get(dataset_url)

            soup = BeautifulSoup(r.content, 'html.parser')

            for node in soup.findAll("script", {"type": "application/ld+json"}):
                script = ''.join(node.findAll(text=True))
                # print(script)
                data = re.sub(r'[\n ]+', ' ', script).strip()
                # new_string = re.sub(r"['[]']", "", data)
                finalvalue = ast.literal_eval(data)
                if finalvalue is not None:
                    datasetDict = finalvalue[1]
                    molecularDict = finalvalue[0]

                merge_dict = {**datasetDict,**molecularDict}
                log.debug(merge_dict)
                content = json.dumps(merge_dict)

                harvest_object.content = content
                harvest_object.save()

        except (Exception) as e:
            log.exception(e)
            self._save_object_error(
                "Exception in fetch stage for %s: %r / %s"
                % (harvest_object.guid, e, traceback.format_exc()),
                harvest_object,
            )
            return False

        return True

    def import_stage(self, harvest_object):
        """
        To harvest data from scraped values to ckan database
        :param harvest_object: HarvestObject object
        :return: True if everything went well, False if errors were found
        """
        global created,modified
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
            log.debug(content)

            # get id
            package_dict["id"] = munge_title_to_name(harvest_object.guid)
            log.debug(f"Here isthe package id saved {package_dict['id']}")

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
            package_dict['notes'] = content['description']
            package_dict["license_id"] = self._extract_license_id(context=context, content=content)
            log.debug(f'This is the license {package_dict["license_id"]}')

            self._extract_extras_image(package= package_dict,content_hasBioPart= content)

            #package_dict['author'] = content['citation']

            #package_dict['extras'] = extras

            # Chemical information by extracting BioChemEntity
            content_hasBioPart = content['hasBioChemEntityPart'][0]
            package_dict['inchi'] = content_hasBioPart['inChI']
            package_dict['inchi_key'] = content_hasBioPart['inChIKey']
            package_dict['smiles'] = content_hasBioPart['smiles']
            package_dict['exactmass'] = content_hasBioPart['monoisotopicMolecularWeight']
            package_dict['mol_formula'] = content_hasBioPart['molecularFormula']


            ## measurement Technical Information
            technique_measure = [content['measurementTechnique']]
            technique_null = technique_measure[0][0]
            technique = technique_null['name']
            package_dict['measurement_technique'] = technique

            package_dict['measurement_technique_iri'] = technique_null['url']

            #package_dict['doi'] = content_hasBioPart['doi']
            #package_dict['measurement_technique'] = content['']

            #package_dict['metadata_created']    = content['']

            package_dict['metadata_published'] = content['datePublished']

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
                names=content.get('alternateName'),
                name_source="BioSchemas",
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


    # This function to get dataset urls of given sitemap
    def _get_dataseturl(self, url):

        try:
            response = urllib.request.urlopen(url)
            xml = BeautifulSoup(response,
                                'lxml-xml',
                                from_encoding=response.info().get_param('charset'))
            AllSites = []
            done = []

            # There would be two cases where index of sitemaps would be given or only one sitemap with datasets would
            # be given. Below two conditions check the xml whether it is a direct sitemap or set of sitemaps. We can
            # scrap application/ld+json schema for both the conditions. And this whole function returns the URL of
            # whole dataset in the HTML format.

            if xml.find("sitemap"):
                sitemaps = xml.find_all("sitemap")
                log.debug("This source is a sitemap with more than one sitemaps urls")

                for sitemap in sitemaps:
                    AllSites.append(sitemap.findNext("loc").text)

                for site in AllSites:
                    EachSiteresponse = urllib.request.urlopen(site)

                    EachSitexml = BeautifulSoup(EachSiteresponse,
                                                'lxml-xml',
                                                from_encoding=response.info().get_param('charset'))
                    datasetURL = EachSitexml.find_all("url")
                    datasetURL10 = datasetURL[:61]

                    for durl in datasetURL10:
                        ndrul = durl.findNext("loc").text
                        # log.debug("doing stuff")
                        done.append(self.scrape_new(ndrul))
                        log.debug("Gathered URL & identifier")

            elif xml.find_all("url"):
                sitemaps = xml.find_all("url")
                datasetURL10 = sitemaps[:61]
                log.debug("This source contains sitemap as root")
                for durl in datasetURL10:
                    ndrul = durl.findNext("loc").text
                    done.append(self.scrape_new(ndrul))
                    log.debug("Gathered URL & identifier")

            else:
                log.debug("This source contains only one URL")
                done.append(self.scrape_new(url))
                log.debug("Gathered URL & identifier")

        except:
            return log.debug('missing data')

        return done

    def scrape_new(self, dataseturls):

        global data_dict
        identifier = None

        r = requests.get(dataseturls)
        soup = BeautifulSoup(r.content, 'html.parser')

        for node in soup.findAll("script", {"type": "application/ld+json"}):
            script = ''.join(node.findAll(text=True))
            metadata = re.sub(r'[\n ]+', ' ', script).strip()
            try:
                conv_dict = ast.literal_eval(metadata)
                # log.debug("generating Dict")
                if conv_dict is not None:
                    dict = conv_dict[1]
                    identifier = dict['identifier']
                    log.debug("generated identi %s" % identifier)
            except SyntaxError:
                pass

        return identifier
        # molecularDict = conv_dict[0]

    def _get_mapping(self):
        return {
            "title": "name",
            "notes": "description",
            "url": "url",
            "metadata_modified": "dateModified",
            "metadata_created": "dateCreated",
        }

    def _extract_resources(self,content):
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

    def _extract_tags(self,content):

        technique_measure = [content['measurementTechnique']]
        technique0 = technique_measure[0][0]
        technique = technique0['name']

        #log.debug(f'this is technia {technique}')
        
        #if technique:
        #    tags.extend(technique)

        tags = [{"name": munge_tag(technique[:100])}]# for tag in tags]
        return tags

    def _extract_extras_image(self,package,content_hasBioPart):
        extras = []
        package_id = package['id']
        #
        content = content_hasBioPart['hasBioChemEntityPart'][0]
        #
        standard_inchi = content['inChI']
        #
        inchi_key = content['inChIKey']
        #smiles = content['smiles']
        #exact_mass = content['monoisotopicMolecularWeight']
        #
        #
        #extras.append({"key": "inchi", 'value' : standard_inchi})
        #extras.append({"key": "inchi_key", 'value' : inchi_key})
        #extras.append({"key": "smiles", 'value' : smiles})
        #extras.append({'key': "exactmass", "value": exact_mass})



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
        #return extras

        return None

    def _extract_license_id(self, context, content):
        package_license = None
        content_license = content['license']
        license_list = get_action('license_list')(context.copy(), {})
        for license_name in license_list:

            if content_license == license_name['id'] or content_license == license_name['url'] or content_license == license_name['title']:
                package_license = license_name['id']

        return package_license
