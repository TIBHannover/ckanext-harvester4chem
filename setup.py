# -*- coding: utf-8 -*-
# Always prefer setuptools over distutils
from setuptools import setup, find_packages
from codecs import open  # To use a consistent encoding
from os import path

here = path.abspath(path.dirname(__file__))

# Get the long description from the relevant file
with open(path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

setup(
    name='''ckanext-harvester4chem''',

    # Versions should comply with PEP440.  For a discussion on single-sourcing
    # the version across setup.py and the project code, see
    # http://packaging.python.org/en/latest/tutorial.html#version
    version='0.2.0',

    description='''Append all the harvesters in Search Service NFDI4Chem''',
    long_description=long_description,
    long_description_content_type="text/markdown",

    # The project's main homepage.
    url='https://github.com/bhavin2897/ckanext-harvester4chem',

    # Author details
    author='''Bhavin Katabathuni''',
    author_email='''bhavin.katabathuni@tib.eu''',

    # Choose your license
    license='AGPL',

    # What does your project relate to?
    keywords='''CKAN harvesting metadata NFDI4Chem OAI-PMH Dublin Core Schemaorg Bioschemas''',

    # You can just specify the packages manually here if your project is
    # simple. Or you can use find_packages().
    packages=find_packages(exclude=['contrib', 'docs', 'tests*']),
        namespace_packages=['ckanext'],

    install_requires=[
      # CKAN extensions should not list dependencies here, but in a separate
      # ``requirements.txt`` file.
      #
      # http://docs.ckan.org/en/latest/extensions/best-practices.html
      # add-third-party-libraries-to-requirements-txt
    ],

    # If there are data files included in your packages that need to be
    # installed, specify them here.  If using Python 2.6 or less, then these
    # have to be included in MANIFEST.in as well.
    include_package_data=True,
    package_data={
    },


    # To provide executable scripts, use entry points in preference to the
    # "scripts" keyword. Entry points provide cross-platform support and allow
    # pip to create the appropriate form of executable for the target platform.
    entry_points='''
        [ckan.plugins]
        
        harvester4chem =        ckanext.harvester4chem.plugin:Harvester4ChemPlugin
        bioschemaharvester=     ckanext.harvester4chem.harvesters.bioschemascrap:BioSchemaMUHarvester
        nmrxivharvester=        ckanext.harvester4chem.harvesters.nmrXiv_harvester:NMRxIVBioSchema
        chemotionharvester=     ckanext.harvester4chem.harvesters.chemotion_repo:ChemotionRepoHarvester
        oaipmh_harvester=       ckanext.harvester4chem.harvesters.oaipmh:OaipmhHarvester
        oaipmh_dc_harvester=    ckanext.harvester4chem.harvesters.oaipmh_dc:OaipmhDCHarvester
        dataverse_harvester=    ckanext.harvester4chem.harvesters.dataverse_harvester:DataVerseHarvester
        
        [babel.extractors]
        ckan = ckan.lib.extract:extract_ckan
    ''',

    # harvester4chem=         ckanext.harvester4chem.plugin:Harvester4ChemPlugin
    # If you are changing from the default layout of your extension, you may
    # have to change the message extractors, you can read more about babel
    # message extraction at
    # http://babel.pocoo.org/docs/messages/#extraction-method-mapping-and-configuration
    message_extractors={
        'ckanext': [
            ('**.py', 'python', None),
            ('**.js', 'javascript', None),
            ('**/templates/**.html', 'ckan', None),
        ],
    }
)
