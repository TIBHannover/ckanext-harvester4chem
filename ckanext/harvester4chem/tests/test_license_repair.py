"""Isolated repair tests. CKAN actions and HTTP are mocked; no production I/O."""

import ast
import copy
import csv
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import click
from click.testing import CliRunner
import requests
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


logic = types.ModuleType('ckan.logic')
logic.get_action = Mock()
with patch.dict(sys.modules, {'ckan': types.ModuleType('ckan'), 'ckan.logic': logic}):
    licenses = load_module('repair_license_utils', 'license_utils.py')
with patch.dict(sys.modules, {'ckanext.harvester4chem.license_utils': licenses}):
    repair = load_module('repair_under_test', 'license_repair.py')


def link(source='chemotion'):
    kind, url = {
        'chemotion': ('Chemotion repo Harvester', 'https://www.chemotion-repository.net/api/v1/publications'),
        'nmrxiv': ('nmrXiv Swagger Harvester', 'https://nmrxiv.org/api/v1/'),
        'massbank': ('Bioschema Sitemap ', 'https://massbank.eu/MassBank/sitemap.xml'),
    }[source]
    return dict(guid='ABCD123', current=True, harvest_source_id='source1',
                job_source_id='source1', source_id='source1', source_type=kind, source_url=url)


def package(name='dataset', license_id=None, **overrides):
    result = dict(id=name + '-id', name=name, url='https://www.chemotion-repository.net/home/publications/datasets/1',
                  license_id=license_id, type='dataset', state='active')
    result.update(overrides)
    return result


class FakeStore:
    def __init__(self, packages=None, provenance=None):
        self.packages = packages or [package()]
        self.provenance = provenance if provenance is not None else [link()]
        self.context = {'ignore_auth': True}
        self.patch = Mock(side_effect=self.apply)
        self.release = Mock()
        self.action = Mock()
        self.ids = {'CC-BY-SA-4.0', 'CC-BY-4.0', 'notspecified', 'CC0-1.0'}

    def registered(self):
        return self.ids

    def load(self, candidate, lock=False):
        for item in self.packages:
            if item['id'] == candidate.get('id') or (not candidate.get('id') and item['name'] == candidate.get('name')):
                return copy.deepcopy(item), copy.deepcopy(self.provenance)
        raise ValueError('package does not exist')

    def candidates(self, names, malformed, source, offset, limit):
        rows = sorted((copy.deepcopy(p) for p in self.packages if names is None or p['name'] in names),
                      key=lambda p: p['name'])
        return rows[offset:None if limit is None else offset + limit]

    def apply(self, identifier, license_id):
        for item in self.packages:
            if item['id'] == identifier:
                item['license_id'] = license_id
        return {'id': identifier, 'license_id': license_id}


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            {'id': 'CC-BY-SA-4.0', 'url': 'https://creativecommons.org/licenses/by-sa/4.0', 'title': 'Attribution-ShareAlike'},
            {'id': 'CC-BY-4.0', 'url': 'https://creativecommons.org/licenses/by/4.0/', 'title': 'Attribution'},
            {'id': 'CC0-1.0', 'url': 'https://creativecommons.org/publicdomain/zero/1.0/', 'title': 'CC0'},
        ]
        patcher = patch.object(licenses, 'get_action', Mock(return_value=lambda context, data: self.entries))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = FakeStore()
        self.client = Mock()
        self.client.license.return_value = 'https://creativecommons.org/licenses/by-sa/4.0/'
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def process(self, mode='dry-run', **kwargs):
        return repair.process(self.store.packages[0], self.store, self.client,
                              mode, kwargs.get('source', 'all'), kwargs.get('malformed', False))

    def execute(self, mode='dry-run', **kwargs):
        return repair.execute(self.store, self.client, mode, output_dir=self.temp.name,
                              emit=lambda value: None, **kwargs)

    def test_dry_run_chemotion_trailing_slash(self):
        row = self.process()
        self.assertEqual(row['status'], 'would_repair')
        self.assertEqual(row['resolved_license_id'], 'CC-BY-SA-4.0')
        self.store.patch.assert_not_called()

    def test_run_only_patch_id_and_license(self):
        action = Mock(return_value={'id': 'dataset-id', 'license_id': 'CC-BY-SA-4.0'})
        store = repair.Store(Mock(), Mock(return_value=action), {'ignore_auth': True})
        store.patch('dataset-id', 'CC-BY-SA-4.0')
        store.action.assert_called_once_with('package_patch')
        action.assert_called_once_with({'ignore_auth': True},
                                       {'id': 'dataset-id', 'license_id': 'CC-BY-SA-4.0'})
        self.assertEqual(self.process('run')['status'], 'repaired')
        self.store.patch.assert_called_once_with('dataset-id', 'CC-BY-SA-4.0')

    def test_missing_and_unknown_source_license(self):
        for value, status in [(None, 'missing_at_source'), ([], 'missing_at_source'),
                              ([None, ' '], 'missing_at_source'), ('unknown', 'unknown_license')]:
            with self.subTest(value=value):
                self.client.license.return_value = value
                self.assertEqual(self.process('run')['status'], status)
        self.store.patch.assert_not_called()

    def test_molecules_inactive_and_missing_package(self):
        for overrides in ({'type': 'molecule'}, {'state': 'deleted'}):
            self.store.packages = [package(**overrides)]
            self.assertEqual(self.process('run')['status'], 'skipped')
        row = repair.process({'name': 'absent'}, self.store, self.client, 'run', 'all', False)
        self.assertEqual(row['status'], 'failed')
        self.store.patch.assert_not_called()
        self.client.license.assert_not_called()

    def test_source_classification(self):
        for source in ('chemotion', 'nmrxiv', 'massbank'):
            self.assertEqual(repair.classify(package(), [link(source)])[0], source)
        unknown = dict(link('massbank'), source_url='https://other.example/sitemap.xml')
        self.assertIsNone(repair.classify(package(), [unknown])[0])
        self.assertIsNone(repair.classify(package(url='https://massbank.eu/record'), [])[0])
        self.assertEqual(repair.classify(package(url='https://nmrxiv.org/D1'), [])[0], 'nmrxiv')
        self.assertIsNone(repair.classify(package(), [dict(link(), job_source_id='conflict')])[0])
        self.assertIsNone(repair.classify(package(), [link(), dict(link(), source_id='other')])[0])
        self.assertIsNone(repair.classify(package(), [link(), dict(link(), guid='other')])[0])
        # Unsupported provenance must not be overridden by a familiar dataset URL.
        self.assertIsNone(repair.classify(package(), [unknown])[0])
        self.assertEqual(self.process('run', source='massbank')['status'], 'source_unknown')
        self.store.patch.assert_not_called()

    def test_no_provenance_cannot_fetch(self):
        self.store.provenance = []
        self.assertEqual(self.process('run')['status'], 'source_unknown')
        self.client.license.assert_not_called()
        self.store.patch.assert_not_called()

    def test_registered_and_malformed_license(self):
        for value in ('CC-BY-4.0', 'notspecified'):
            self.store.packages = [package(license_id=value)]
            self.assertEqual(self.process('run', malformed=True)['status'], 'already_correct')
        self.store.packages = [package(license_id='CC BY 4.0 Deed')]
        self.assertEqual(self.process('run', malformed=True)['status'], 'repaired')
        self.client.license.assert_not_called()
        self.assertEqual(self.store.packages[0]['license_id'], 'CC-BY-4.0')
        self.assertEqual(self.process('run', malformed=True)['status'], 'already_correct')
        self.store.patch.assert_called_once()

    def test_valid_legacy_id_is_never_replaced(self):
        self.store.ids.add('CC BY 4.0 Deed')
        self.store.packages = [package(license_id='CC BY 4.0 Deed')]
        self.assertEqual(self.process('run', malformed=True)['status'], 'already_correct')
        self.store.patch.assert_not_called()

    def test_network_and_patch_failure_isolated(self):
        self.store.packages = [package('a'), package('b')]
        self.client.license.side_effect = [requests.Timeout('timed out'), 'CC-BY-4.0']
        directory, summary = self.execute('run')
        self.assertEqual(summary['counts'], {'failed': 1, 'repaired': 1})
        self.assertEqual(len(list(csv.DictReader((directory / 'failed.csv').open()))), 1)
        self.store.packages = [package()]
        self.client.license.side_effect = None
        self.store.patch.side_effect = RuntimeError('package action failed')
        directory, summary = self.execute('run')
        self.assertEqual(summary['counts'], {'failed': 1})
        self.assertIn('package action failed', (directory / 'failed.csv').read_text())

    def test_resume_skips_finalized_and_validates_mode(self):
        directory, _ = self.execute('run')
        _, summary = repair.execute(self.store, self.client, 'run', resume=directory, emit=lambda value: None)
        self.assertEqual(summary['counts'], {'repaired': 1})
        self.store.patch.assert_called_once()
        previous = (directory / 'summary.json').read_text()
        with self.assertRaises(ValueError):
            repair.execute(self.store, self.client, 'dry-run', resume=directory)
        self.assertEqual((directory / 'summary.json').read_text(), previous)

    def test_interrupt_flushes_and_resumes_original_candidates(self):
        self.store.packages = [package('a'), package('b')]
        self.client.license.side_effect = ['CC-BY-4.0', KeyboardInterrupt()]
        directory, summary = self.execute('run')
        self.assertEqual(summary['state'], 'interrupted')
        self.assertEqual(summary['processed'], 1)
        self.client.license.side_effect = None
        self.client.license.return_value = 'CC-BY-4.0'
        _, summary = repair.execute(self.store, self.client, 'run', resume=directory, emit=lambda value: None)
        self.assertEqual(summary['processed'], 2)
        self.assertEqual(self.store.patch.call_count, 2)

    def test_crash_after_commit_before_journal_is_idempotent(self):
        def committed_then_interrupted(identifier, license_id):
            self.store.apply(identifier, license_id)
            raise KeyboardInterrupt()
        self.store.patch.side_effect = committed_then_interrupted
        directory, summary = self.execute('run')
        self.assertEqual(summary['processed'], 0)
        _, summary = repair.execute(self.store, self.client, 'run', resume=directory, emit=lambda value: None)
        self.assertEqual(summary['counts'], {'already_correct': 1})
        self.store.patch.assert_called_once()

    def test_partial_journal_tail_recovered(self):
        directory, _ = self.execute('run')
        with (directory / 'processed.jsonl').open('ab') as stream:
            stream.write(b'{"unfinished":')
        repair.execute(self.store, self.client, 'run', resume=directory, emit=lambda value: None)
        self.store.patch.assert_called_once()
        self.assertTrue((directory / 'processed.jsonl').read_bytes().endswith(b'\n'))

    def test_concurrent_license_change_is_preserved(self):
        original_load = self.store.load
        def changed(candidate, lock=False):
            if lock:
                self.store.packages[0]['license_id'] = 'CC-BY-4.0'
            return original_load(candidate, lock)
        self.store.load = changed
        self.assertEqual(self.process('run')['status'], 'already_correct')
        self.store.patch.assert_not_called()

    def test_concurrent_source_change_is_preserved(self):
        original_load = self.store.load
        def changed(candidate, lock=False):
            if lock:
                self.store.provenance = [link('nmrxiv')]
            return original_load(candidate, lock)
        self.store.load = changed
        self.assertEqual(self.process('run')['status'], 'source_unknown')
        self.store.patch.assert_not_called()

    def test_nmrxiv_deed_from_metadata(self):
        self.store.provenance = [link('nmrxiv')]
        self.client.license.return_value = {'name': 'CC BY 4.0 Deed'}
        self.assertEqual(self.process('run', source='nmrxiv')['status'], 'repaired')
        self.store.patch.assert_called_once_with('dataset-id', 'CC-BY-4.0')

    def test_all_reports_exist_and_dry_run_has_no_patch(self):
        directory, summary = self.execute()
        for filename in ['candidates.csv', 'repaired.csv', 'would-repair.csv',
                         'already-correct.csv', 'missing-at-source.csv',
                         'unknown-license.csv', 'source-unknown.csv', 'failed.csv',
                         'summary.json', 'repair.log']:
            self.assertTrue((directory / filename).exists(), filename)
        self.assertEqual(summary['counts'], {'would_repair': 1})
        self.store.patch.assert_not_called()

    def test_checkpoint_directory_cannot_be_used_concurrently(self):
        reports = repair.Reports(self.temp.name)
        try:
            with self.assertRaisesRegex(ValueError, 'locked'):
                repair.Reports(self.temp.name)
        finally:
            reports.close()

    def test_unexpected_package_action_result_is_reported(self):
        self.store.patch.side_effect = None
        self.store.patch.return_value = {'license_id': 'unexpected'}
        result = self.process('run')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('may have committed', result['reason'])

    def test_manifest(self):
        path = Path(self.temp.name) / 'manifest'
        path.write_text('# comment\n\na\nb\n')
        self.assertEqual(repair.read_manifest(path), ['a', 'b'])
        path.write_text('a\na\n')
        with self.assertRaisesRegex(ValueError, 'duplicate manifest entry'):
            repair.read_manifest(path)

    def test_no_chemistry_pipeline_or_sql_updates(self):
        tree = ast.parse((ROOT / 'license_repair.py').read_text())
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertFalse(any('molecule' in (name or '') or 'harvesters' in (name or '') for name in imports))
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(calls & {'commit', 'synchronize_harvested_package', 'rebuild', 'import_stage', 'fetch_stage', 'gather_stage'})
        self.assertNotIn('UPDATE package', (ROOT / 'license_repair.py').read_text())
        self.assertNotIn('UPDATE public.package', (ROOT / 'license_repair.py').read_text())


class CandidateSQLTests(unittest.TestCase):
    def test_real_selection_sql_filters_and_orders(self):
        engine = create_engine('sqlite://')
        connection = engine.connect()
        self.addCleanup(engine.dispose)
        self.addCleanup(connection.close)
        connection.connection.create_function('btrim', 1, lambda value: value.strip() if value else value)
        connection.execute("ATTACH DATABASE ':memory:' AS public")
        connection.execute('CREATE TABLE public.package (id text, name text, url text, license_id text, type text, state text)')
        for item in [package('b'), package('a', ' '), package('valid', 'CC-BY-4.0'),
                     package('malformed', 'CC BY 4.0 Deed'), package('mol', type='molecule'),
                     package('inactive', state='deleted'), package('empty', '')]:
            connection.execute(repair.text('INSERT INTO public.package VALUES (:id,:name,:url,:license_id,:type,:state)'), item)
        store = repair.Store(connection, Mock(return_value=lambda c, d: [{'id': 'CC-BY-4.0'}]), {})
        store.links = Mock(return_value=[])
        self.assertEqual([r['name'] for r in store.candidates(None, False, 'all', 0, None)], ['a', 'b', 'empty'])
        self.assertEqual([r['name'] for r in store.candidates(None, True, 'all', 0, None)], ['a', 'b', 'empty', 'malformed'])
        self.assertEqual([r['name'] for r in store.candidates(None, False, 'all', 1, 1)], ['b'])
        self.assertEqual([r['name'] for r in store.candidates(['b'], False, 'all', 0, None)], ['b'])
        self.assertEqual(store.candidates(None, False, 'nmrxiv', 0, None), [])


class HTTPTests(unittest.TestCase):
    def client(self, payload, status=200):
        response = Mock(status_code=status, url='https://nmrxiv.org/api/v1/schemas/bioschemas/D1', headers={})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [payload.encode('utf-8')]
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError('HTTP {}'.format(status), response=response)
        session = Mock()
        session.get.return_value = response
        return repair.MetadataClient(session=session, sleep=Mock()), response

    def test_json_sources_request_only_metadata(self):
        for source in ('chemotion', 'nmrxiv'):
            client, _ = self.client(json.dumps({'@type': 'Dataset', 'license': {'name': 'CC-BY-4.0'}}))
            self.assertEqual(client.license(source, link(source)), {'name': 'CC-BY-4.0'})
            self.assertEqual(client.session.get.call_count, 1)
            args, kwargs = client.session.get.call_args
            self.assertIn('download_json?Container&id=0&inchikey=' if source == 'chemotion' else '/schemas/bioschemas/', args[0])
            self.assertEqual(kwargs['timeout'], (10, 30))

    def test_massbank_selects_dataset_not_molecule(self):
        data = [{'@type': 'MolecularEntity', 'license': 'wrong'},
                {'@type': 'Dataset', 'license': ['CC-BY-4.0']}]
        client, _ = self.client('<script type="application/ld+json">' + json.dumps(data) + '</script>')
        self.assertEqual(client.license('massbank', link('massbank')), ['CC-BY-4.0'])
        self.assertEqual(client.session.get.call_args[1]['params'], {'id': 'ABCD123'})

    def test_http_retry_and_not_found(self):
        for status, attempts in [(429, 3), (503, 3), (404, 1)]:
            client, _ = self.client('', status)
            with self.assertRaises(requests.HTTPError):
                client.get('https://nmrxiv.org/api')
            self.assertEqual(client.session.get.call_count, attempts)
        client, _ = self.client('')
        client.session.get.side_effect = requests.Timeout('slow')
        with self.assertRaises(requests.Timeout):
            client.get('https://nmrxiv.org/api')
        self.assertEqual(client.session.get.call_count, 3)

    def test_bad_metadata_and_size_limit(self):
        client, _ = self.client('not json')
        with self.assertRaises(ValueError):
            client.license('nmrxiv', link('nmrxiv'))
        client, response = self.client('')
        response.iter_content.return_value = [b'x' * (4 * 1024 * 1024 + 1)]
        with self.assertRaisesRegex(ValueError, '4 MiB'):
            client.get('https://nmrxiv.org/api')

    def test_cross_host_redirect_rejected(self):
        client, response = self.client('', status=302)
        response.headers = {'Location': 'https://other.example/download'}
        with self.assertRaisesRegex(ValueError, 'cross-host'):
            client.get('https://nmrxiv.org/api')
        self.assertEqual(client.session.get.call_count, 1)


class CLITests(unittest.TestCase):
    def setUp(self):
        # Compile the real command/group definitions without importing the
        # unrelated existing chemistry commands and their RDKit dependencies.
        tree = ast.parse((ROOT / 'cli.py').read_text())
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ('harvester4chem', 'repair_licenses_command', 'get_commands')]
        self.namespace = {'click': click, 'model': Mock(), 'toolkit': Mock(config={})}
        exec(compile(tree, str(ROOT / 'cli.py'), 'exec'), self.namespace)
        self.group = self.namespace['get_commands']()[0]
        self.fake = types.ModuleType('ckanext.harvester4chem.license_repair')
        self.fake.Store, self.fake.MetadataClient = Mock(), Mock()
        self.fake.execute = Mock(return_value=('/tmp/mock', {'state': 'complete'}))
        self.fake.read_manifest = repair.read_manifest
        patcher = patch.dict(sys.modules, {'ckanext.harvester4chem.license_repair': self.fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_help_and_mode_exclusivity(self):
        runner = CliRunner()
        self.assertIn('repair-licenses', runner.invoke(self.group, ['--help']).output)
        for args in ([], ['--run', '--dry-run']):
            result = runner.invoke(self.group, ['repair-licenses'] + args)
            self.assertNotEqual(result.exit_code, 0)
        self.fake.execute.assert_not_called()

    def test_exact_dataset_and_options(self):
        runner = CliRunner()
        result = runner.invoke(self.group, ['repair-licenses', '--dataset', 'one-name', '--dry-run'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.fake.execute.call_args[1]['names'], ['one-name'])
        self.assertEqual(self.fake.execute.call_args[0][2], 'dry-run')
        for args in (['--dataset', 'one', '--limit', '1'], ['--include-malformed', '--missing-only'], ['--batch-size', '0']):
            self.assertNotEqual(runner.invoke(self.group, ['repair-licenses', '--dry-run'] + args).exit_code, 0)


if __name__ == '__main__':
    unittest.main()
