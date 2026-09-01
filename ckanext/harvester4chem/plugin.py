import ckan.plugins as plugins
import ckan.plugins.toolkit as toolkit

from ckanext.harvester4chem import cli


class Harvester4ChemPlugin(plugins.SingletonPlugin):
    plugins.implements(plugins.IConfigurer)
    plugins.implements(plugins.IClick)

    # IConfigurer

    def update_config(self, config_):
        toolkit.add_template_directory(config_, 'templates')
        toolkit.add_public_directory(config_, 'public')
        toolkit.add_resource('fanstatic',
            'harvester4chem')

    def get_commands(self):
        return cli.get_commands()
