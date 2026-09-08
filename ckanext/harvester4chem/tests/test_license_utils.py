"""Run with unittest or pytest; no CKAN installation, database or network needed."""

import ast
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


# Load only the shared utility with a scoped CKAN action stub. Do not import
# harvesters (which load RDKit and database models) or alter other tests' CKAN.
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'license_utils_under_test', str(ROOT / 'license_utils.py'))
licenses = importlib.util.module_from_spec(spec)
logic = types.ModuleType('ckan.logic')
logic.get_action = Mock()
with patch.dict(sys.modules, {'ckan': types.ModuleType('ckan'), 'ckan.logic': logic}):
    spec.loader.exec_module(licenses)


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            {'id': 'CC BY-SA 4.0', 'title': 'Attribution-ShareAlike 4.0 International',
             'url': 'https://creativecommons.org/licenses/by-sa/4.0'},
            {'id': 'CC-BY-4.0', 'title': 'Attribution 4.0 International',
             'url': 'https://creativecommons.org/licenses/by/4.0'},
            {'id': 'custom', 'title': 'Custom License',
             'url': 'https://example.org/License'},
        ]
        self.action = Mock(return_value=self.entries)
        self.getter = Mock(return_value=self.action)
        patcher = patch.object(licenses, 'get_action', self.getter)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_chemotion_regression(self):
        for source in [
            'https://creativecommons.org/licenses/by-sa/4.0/',
            'http://creativecommons.org/licenses/by-sa/4.0/',
            ' HTTPS://CREATIVECOMMONS.ORG/licenses/by-sa/4.0/ ',
            'CC BY-SA 4.0', 'cc by-sa 4.0', 'CC-BY-SA-4.0',
            ' attribution-sharealike 4.0 international ',
        ]:
            with self.subTest(source=source):
                self.assertEqual(licenses.resolve_license_id(
                    source, {}, '10-14272-aafjwsujjwijew-uhfffaoysa-n-chmo0000470'
                ), 'CC BY-SA 4.0')

    def test_reverse_slash(self):
        self.entries[0]['url'] += '/'
        self.assertEqual(licenses.resolve_license_id(
            'https://creativecommons.org/licenses/by-sa/4.0', {}), 'CC BY-SA 4.0')

    def test_observed_legacy_labels_resolve_only_to_registered_ids(self):
        pairs = [
            ('CC BY 4.0 Deed', 'CC-BY-4.0'),
            ('CC0 1.0 Universal ', 'CC0-1.0'),
            ('CC BY-NC-SA 4.0 Deed', 'CC-BY-NC-SA-4.0'),
            ('CC BY-NC-ND 4.0 Legal Code', 'CC-BY-NC-ND-4.0'),
            ('CC BY-SA 4.0 Deed ', 'CC BY-SA 4.0'),
            ('CC BY-NC 4.0 Deed', 'CC BY-NC 4.0'),
            ('CC BY-ND 4.0 Deed', 'CC-BY-ND-4.0'),
        ]
        for source, target in pairs:
            with self.subTest(source=source):
                self.entries[:] = [{'id': target, 'url': '', 'title': ''}]
                self.assertEqual(licenses.resolve_license_id(source, {}), target)
                self.entries.clear()
                with self.assertLogs(licenses.log, level='WARNING'):
                    self.assertIsNone(licenses.resolve_license_id(source, {}))

    def test_legacy_aliases_do_not_accept_modified_terms(self):
        for source in ('CC BY 4.0 Deed modified', 'CC0 1.0 Universal with exceptions'):
            with self.assertLogs(licenses.log, level='WARNING'):
                self.assertIsNone(licenses.resolve_license_id(source, {}))

    def test_representations(self):
        for source in [
            'CC-BY-4.0', 'cc-by-4.0',
            {'@id': 'https://creativecommons.org/licenses/by/4.0/'},
            {'url': 'https://creativecommons.org/licenses/by/4.0/'},
            {'name': 'CC-BY-4.0'},
            ['https://creativecommons.org/licenses/by/4.0/'],
            [{'url': 'https://creativecommons.org/licenses/by/4.0/'}],
            {'@id': 'unknown', 'name': 'CC-BY-4.0'},
            'CC BY 4.0 (Attribution)',
        ]:
            with self.subTest(source=source):
                self.assertEqual(licenses.resolve_license_id(source, {}), 'CC-BY-4.0')

    def test_missing(self):
        for source in [None, '', [], {}, [None, ' ']]:
            with self.subTest(source=source):
                self.assertIsNone(licenses.resolve_license_id(source, {}))
        self.getter.assert_not_called()

    def test_unknown_is_logged(self):
        for source in [
            'https://example.org/unknown-license',
            'http://example.org/License',  # No arbitrary scheme equivalence.
            'https://example.org/license',  # Paths are case sensitive.
            'https://example.org/License?different=1',
            'https://example.org/License#different',
            'https://[invalid', 42, {'unexpected': 'CC-BY-4.0'},
            'CC BY 4.0 modified terms', 'Custom-License',
        ]:
            with self.subTest(source=source):
                with self.assertLogs(licenses.log, level='WARNING') as logs:
                    self.assertIsNone(licenses.resolve_license_id(source, {}, 'dataset-123'))
                self.assertIn('unknown license for dataset dataset-123', logs.output[0])

    def test_oai_rights_any_position(self):
        for index in range(3):
            with self.subTest(index=index):
                rights = ['Copyright the authors', 'info:eu-repo/semantics/openAccess']
                rights.insert(index, 'https://creativecommons.org/licenses/by/4.0/')
                self.assertEqual(licenses.resolve_license_id(rights, {}), 'CC-BY-4.0')

    def test_registry_is_live_and_context_is_copied(self):
        context = {'user': 'harvester'}
        self.assertEqual(licenses.resolve_license_id('CC-BY-4.0', context), 'CC-BY-4.0')
        self.getter.assert_called_once_with('license_list')
        args = self.action.call_args[0]
        self.assertEqual(args, (context, {}))
        self.assertIsNot(args[0], context)
        self.entries.clear()
        with self.assertLogs(licenses.log, level='WARNING'):
            self.assertIsNone(licenses.resolve_license_id('CC-BY-4.0', context))

    def test_priority_and_ambiguity(self):
        self.entries.append({'id': 'CC BY 4.0', 'url': '', 'title': ''})
        self.assertEqual(licenses.resolve_license_id(
            ['https://creativecommons.org/licenses/by-sa/4.0', 'CC BY 4.0'], {}
        ), 'CC BY 4.0')
        with self.assertLogs(licenses.log, level='WARNING'):
            self.assertIsNone(licenses.resolve_license_id('CC-BY 4.0', {}))

    def test_registry_failure_does_not_break_import(self):
        self.action.side_effect = RuntimeError('registry unavailable')
        package = {'id': 'dataset'}
        with self.assertLogs(licenses.log, level='WARNING'):
            licenses.apply_license(package, 'CC-BY-4.0', {})
        self.assertNotIn('license_id', package)

    def test_not_specified_requires_explicit_source(self):
        self.entries.append({'id': 'unspecified', 'title': 'License Not Specified'})
        package = {'id': 'dataset'}
        licenses.apply_license(package, None, {})
        self.assertNotIn('license_id', package)
        licenses.apply_license(package, 'License Not Specified', {})
        self.assertEqual(package['license_id'], 'unspecified')

    def test_existing_license_protection(self):
        for source in [None, 'unknown']:
            with self.subTest(source=source):
                existing = {'id': 'dataset', 'license_id': 'CC-BY-4.0'}
                licenses.apply_license(existing, source, {})
                self.assertEqual(existing['license_id'], 'CC-BY-4.0')
                incoming = {'id': 'dataset'}
                licenses.apply_license(incoming, source, {})
                self.assertNotIn('license_id', incoming)

    def test_importer_license_assignment(self):
        # Execute each import stage's actual license statement in isolation;
        # full stages would perform unrelated network/DB/molecule operations.
        for name, field in [
            ('chemotion_repo', 'license'), ('nmrXiv_harvester', 'license'),
            ('bioschemascrap', 'license'), ('oaipmh', 'rights'),
            ('oaipmh_dc', 'rights'), ('dataverse_harvester', 'rights'),
        ]:
            path = ROOT / 'harvesters' / (name + '.py')
            tree = ast.parse(path.read_text())
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Expr)
                     and isinstance(node.value, ast.Call)
                     and isinstance(node.value.func, ast.Name)
                     and node.value.func.id == 'apply_license']
            self.assertEqual(len(calls), 1)
            for source, expected in [(None, None), ('unknown', None),
                                     (['CC-BY-4.0'], 'CC-BY-4.0')]:
                with self.subTest(harvester=name, source=source):
                    package = {'id': 'dataset'}
                    module = ast.parse('')
                    module.body = calls
                    exec(compile(module, str(path), 'exec'), {
                        'apply_license': licenses.apply_license, 'package_dict': package,
                        'content': {} if source is None else {field: source}, 'context': {},
                    })
                    if expected:
                        self.assertEqual(package['license_id'], expected)
                    else:
                        self.assertNotIn('license_id', package)


if __name__ == '__main__':
    unittest.main()
