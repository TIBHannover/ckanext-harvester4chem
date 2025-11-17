

[![CKAN](https://img.shields.io/badge/ckan-2.9-orange.svg?style=flat-square)](https://github.com/ckan/ckan/tree/2.9)

# ckanext-harvester4chem

A unified CKAN extension that brings together multiple specialised harvesters tailored for chemistry-related research data.
This project consolidates several separate harvester implementations—each designed for a different metadata source, schema, or API—into a single, organised extension.

The goal of ckanext-harvester4chem is to provide a central place to develop, maintain, and deploy harvesting tools for NFDI4Chem, chemistry repositories, schema.org/bioschemas sites, and other scientific data sources.

## Overview of Included Harvesters
### 1. BioSchemaScraper

_(original repository: ckanext-bioschemaharvester)_

A harvester for extracting (bio)schema-compatible dataset metadata from web pages, e.g. MassBank.
Unlike typical metadata endpoints, many chemistry repositories embed JSON-LD or schema.org markup directly in HTML pages. This tool scrapes such pages and extracts structured metadata.

**Key features**:

* Designed using the official CKAN harvester framework: gather → fetch → import

* Scrapes dataset pages using BeautifulSoup

* Tested primarily with MassBank

* Provides a “BioSchema Scraper/Harvest” option in the CKAN UI

* Stores metadata in custom migrated tables (does not overwrite core CKAN tables)

Dependencies (required before installation):
https://github.com/TIBHannover/ckanext-rdkit-visuals

### 2. OAI-PMH Harvester (General & DataCite support)

_(original repository: ckanext-oaipmh)_

A flexible, updated OAI-PMH harvester adapted for NFDI4Chem needs.
Originally based on OpenSearchData’s version, with significant improvements.

**Additional features compared to the original:**

* Support for the oai_datacite metadata schema (DataCite 4.0+)
* Integration of RDKit for cheminformatics enrichment. 
* Storage of chemical metadata in migrated custom tables
* Support for multiple OAI-PMH metadata formats (oai_dc, oai_datacite, oai_ddi)
* Optional HTTP GET enforcement
* Optional date-range harvesting `(from / until)`
* Optional authentication `(username, password)`


### 3. Chemotion Repository Harvester

A hybrid harvester tailored for the Chemotion Repository.
It combines:

scraping approaches similar to the BioSchemaScraper

timestamp-based incremental harvesting from the OAI-PMH implementation

Chemotion’s API and metadata structure differ from standard schema.org or OAI-PMH sources, so a dedicated harvester ensures correct mapping.

### 4. NMRXiv Harvester

A specialised harvester for the NMRXiv repository.
NMRXiv uses a different API (Swagger/OpenAPI based) and a metadata format specific to NMR experiments. Therefore, a separate harvester is provided to correctly interpret and process its metadata.

### 5. OAI-PMH Dublin Core Harvester

A lightweight OAI-PMH harvester that focuses exclusively on Dublin Core metadata.
Used primarily for repositories such as Radar4Chem, where the published schema is standard Dublin Core rather than DataCite or custom formats.

## Requirements
Ensure `ckanext-harvest` is installed and `harvest` is the sysadmin user. 
For more information, please check https://github.com/ckan/ckanext-harvest 

## Installation


To install ckanext-harvester4chem:

1. Activate your CKAN virtual environment, for example:

        . /usr/lib/ckan/default/bin/activate

2. Clone the source and install it on the virtualenv

        git clone https://github.com/bhavin2897/ckanext-harvester4chem.git
        cd ckanext-harvester4chem
        pip install -e .
	    pip install -r requirements.txt

3. Add the plugins to CKAN based on your requirement 
In `/etc/ckan/default/ckan.ini`

         ckan.plugins= ... harvest ... bioschemaharvester nmrxivharvester chemotionharvester oaipmh_harvester oaipmh_dc_harvester dataverse_harvester 

4. Restart CKAN. For example if you've deployed CKAN with Apache on Ubuntu:

       sudo service apache2 reload


## Config settings

None at present

**TODO:** Document any optional config settings here. For example:

	# The minimum number of hours to wait before re-checking a resource
	# (optional, default: 24).
	ckanext.harvester4chem.some_setting = some_default_value


## Developer installation

To install ckanext-harvester4chem for development, activate your CKAN virtualenv and
do:

    git clone https://github.com/bhavin2897/ckanext-harvester4chem.git
    cd ckanext-harvester4chem
    python setup.py develop
    pip install -r dev-requirements.txt


### Setting Up an OAI-PMH Harvester

1. Go to: `https://<your-ckan-site>/harvest/new`

2. Provide the OAI-PMH base URL as source (e.g. https://oai.datacite.org/oai/)

3. Choose a source type:
   * Dublin Core Harvester
   * DataCite OAI Harvester

4. Optional configuration examples: 

        {"type_chem": "Container", 
        "offset" : 0, 
        "limit" : 1000,
        "date_from" : "",  "date_to": "" }

5. Save the configuration.

6. From the harvest admin panel, trigger Reharvest.


### Running the Harvester Manually  

    # Activate environment
    . /usr/lib/ckan/default/bin/activate

    # Navigate to CKAN install
    cd /usr/lib/ckan/default/src/ckan

    # Start consumers
    ckan -c /etc/ckan/default/ckan.ini harvester gather_consumer &
    ckan -c /etc/ckan/default/ckan.ini harvester fetch_consumer &

    # Run harvesting job
    ckan -c /etc/ckan/default/ckan.ini harvester run


### Running the Harvester Automatically (Cronjob) 

You can also use them for the automatic harvesting usng cronjobs. 

In your `crontab -e` call the harvester from the ckan venv user. (Below example is from the default)

        00 12 * * * /usr/lib/ckan/default/bin/ckan ckan -c /etc/ckan/default/ckan.ini harvester job <source-id> 
       
        05 12 * * * /usr/lib/ckan/default/bin/ckan ckan -c /etc/ckan/default/ckan.ini harvester run  


This will create and run the job for a given harvest source ID 

## Tests

To run the tests, do:

    pytest --ckan-ini=test.ini


## Releasing a new version of ckanext-harvester4chem

If ckanext-harvester4chem should be available on PyPI you can follow these steps to publish a new version:

1. Update the version number in the `setup.py` file. See [PEP 440](http://legacy.python.org/dev/peps/pep-0440/#public-version-identifiers) for how to choose version numbers.

2. Make sure you have the latest version of necessary packages:

    pip install --upgrade setuptools wheel twine

3. Create a source and binary distributions of the new version:

       python setup.py sdist bdist_wheel && twine check dist/*

   Fix any errors you get.

4. Upload the source distribution to PyPI:

       twine upload dist/*

5. Commit any outstanding changes:

       git commit -a
       git push

6. Tag the new release of the project on GitHub with the version number from
   the `setup.py` file. For example if the version number in `setup.py` is
   0.0.1 then do:

       git tag 0.0.1
       git push --tags

## License

[AGPL](https://www.gnu.org/licenses/agpl-3.0.en.html)
