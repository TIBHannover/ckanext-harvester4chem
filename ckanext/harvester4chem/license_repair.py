"""License-only historical repair. No harvest pipeline or chemistry imports."""

import ast
from collections import Counter
import csv
from datetime import datetime
import fcntl
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import quote, urljoin, urlsplit

import requests
from sqlalchemy import bindparam, text

from ckanext.harvester4chem.license_utils import extract_license_values, resolve_license_id


PACKAGE_COLUMNS = 'id, name, url, license_id, type, state'
CANDIDATE_SQL = """
SELECT id, name, url, license_id, type, state FROM public.package
WHERE type='dataset' AND state='active'
  AND (license_id IS NULL OR btrim(license_id)=''
       OR (:malformed AND license_id NOT IN :registered))
ORDER BY name, id
"""
PROVENANCE_SQL = """
SELECT h.guid, h.current, h.harvest_source_id, j.source_id AS job_source_id,
       s.id AS source_id, s.url AS source_url, s.type AS source_type
FROM public.harvest_object h
LEFT JOIN public.harvest_job j ON j.id=h.harvest_job_id
LEFT JOIN public.harvest_source s ON s.id=coalesce(h.harvest_source_id,j.source_id)
WHERE h.package_id=:id AND (h.current OR
    (h.import_finished IS NOT NULL AND h.state='COMPLETE' AND NOT EXISTS (
        SELECT 1 FROM public.harvest_object current_object
        WHERE current_object.package_id=h.package_id AND current_object.current)))
ORDER BY h.current DESC, h.import_finished DESC NULLS LAST, h.id
"""
FIELDS = ['dataset_id', 'dataset_name', 'source', 'dataset_url',
          'current_license_id', 'source_license', 'resolved_license_id', 'status', 'reason']
STATUSES = ('repaired', 'would_repair', 'already_correct', 'missing_at_source',
            'unknown_license', 'source_unknown', 'failed', 'skipped')


def read_manifest(path):
    names, seen = [], set()
    with open(path, encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            name = line.strip()
            if not name or name.startswith('#'):
                continue
            if name in seen:
                raise ValueError('duplicate manifest entry {!r} at line {}'.format(name, number))
            names.append(name)
            seen.add(name)
    return names


def host_is(url, domain):
    try:
        host = urlsplit(url or '').hostname or ''
        return host == domain or host.endswith('.' + domain)
    except ValueError:
        return False


def classify(package, links):
    """Prefer current harvest provenance; never treat 'everything else' as MassBank."""
    current = [link for link in links if link['current']]
    links = current or links
    if links:
        if current and len({link['guid'] for link in current}) != 1:
            return None, None, 'multiple current harvest identifiers'
        if any(link['harvest_source_id'] and link['job_source_id'] and
               link['harvest_source_id'] != link['job_source_id'] for link in links):
            return None, None, 'harvest object/job source conflict'
        if len({link['source_id'] for link in links}) != 1 or not links[0]['source_id']:
            return None, None, 'ambiguous or missing harvest source'
        link = links[0]  # SQL orders newest successful object first.
        kind = (link['source_type'] or '').strip().casefold()
        url = link['source_url']
        if kind == 'chemotion repo harvester' and host_is(url, 'chemotion-repository.net'):
            return 'chemotion', link, 'harvest source'
        if kind == 'nmrxiv swagger harvester' and host_is(url, 'nmrxiv.org'):
            return 'nmrxiv', link, 'harvest source'
        if kind == 'bioschema sitemap' and host_is(url, 'massbank.eu'):
            return 'massbank', link, 'MassBank Bioschema Sitemap source'
        return None, link, 'unrecognized source type/endpoint: ' + kind
    # URL fallback can identify provenance for reporting, but is insufficient
    # to reconstruct source API GUIDs. Missing provenance never triggers a fetch.
    for source, domain in (('chemotion', 'chemotion-repository.net'), ('nmrxiv', 'nmrxiv.org')):
        if host_is(package.get('url'), domain):
            return source, None, 'URL fallback; harvest API identifier unavailable'
    return None, None, 'no reliable harvest source association'


class Store:
    def __init__(self, session, action_getter, context):
        self.session, self.action, self.context = session, action_getter, context

    def registered(self):
        return {entry['id'] for entry in self.action('license_list')(self.context.copy(), {})
                if entry.get('id')}

    def links(self, identifier):
        return [dict(row) for row in self.session.execute(text(PROVENANCE_SQL), {'id': identifier})]

    def load(self, candidate, lock=False):
        field = 'id' if candidate.get('id') else 'name'
        query = 'SELECT {} FROM public.package WHERE {}=:value'.format(PACKAGE_COLUMNS, field)
        if lock:
            query += ' FOR UPDATE NOWAIT'
        row = self.session.execute(text(query), {'value': candidate[field]}).fetchone()
        if row is None:
            raise ValueError('package does not exist')
        package = dict(row)
        return package, self.links(package['id'])

    def candidates(self, names, malformed, source, offset, limit):
        if limit == 0:
            return []
        if names is not None:
            rows = []
            for name in sorted(names):
                try:
                    package, links = self.load({'name': name})
                    package['_source'] = classify(package, links)[0]
                    rows.append(package)
                except ValueError:
                    rows.append({'name': name})
        else:
            query = text(CANDIDATE_SQL).bindparams(bindparam('registered', expanding=True))
            rows = [dict(row) for row in self.session.execute(query, {
                'malformed': malformed, 'registered': sorted(self.registered())})]
        selected = []
        for scanned, row in enumerate(rows, 1):
            if scanned % 500 == 0:
                getattr(self, 'progress', print)('Selecting candidates: scanned {}'.format(scanned))
            if names is None:
                kind, _, _ = classify(row, self.links(row['id']))
                row['_source'] = kind
                if source != 'all' and kind is not None and kind != source:
                    continue
            selected.append(row)
            if limit is not None and len(selected) >= offset + limit:
                break
        return selected[offset:None if limit is None else offset + limit]

    def release(self):
        self.session.rollback()  # Ends read transactions / releases a per-package lock.

    def patch(self, identifier, license_id):
        return self.action('package_patch')(self.context.copy(),
                                           {'id': identifier, 'license_id': license_id})


class JsonLDParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks, self.parts, self.inside = [], [], False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'script' and dict(attrs).get('type', '').lower() == 'application/ld+json':
            self.inside, self.parts = True, []

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if self.inside and tag.lower() == 'script':
            self.blocks.append(''.join(self.parts))
            self.inside = False


def dataset_license(metadata):
    """Select Dataset JSON-LD nodes, not an unrelated molecule or publisher."""
    nodes = metadata if isinstance(metadata, list) else metadata.get('@graph', [metadata])
    datasets = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        types = node.get('@type', [])
        types = [types] if isinstance(types, str) else types
        if any(str(value).rstrip('/').rsplit('/', 1)[-1] == 'Dataset' for value in types):
            datasets.append(node)
    if not datasets and isinstance(metadata, dict) and '@graph' not in metadata:
        datasets = [metadata] if not metadata.get('@type') else []
    if len(datasets) != 1:
        raise ValueError('expected exactly one Dataset metadata node')
    return datasets[0].get('license', datasets[0].get('rights'))


class MetadataClient:
    """Bounded metadata GETs only; explicit retries and polite pacing."""
    def __init__(self, session=None, delay=0.5, sleep=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({'User-Agent': 'NFDI4Chem-license-repair/1.0'})
        self.delay, self.sleep = delay, sleep

    def close(self):
        self.session.close()

    def get(self, url, params=None):
        parsed = urlsplit(url)
        original_host = parsed.hostname
        if (parsed.scheme not in ('http', 'https') or not original_host
                or parsed.username or parsed.password):
            raise ValueError('invalid source endpoint')
        for attempt in range(3):
            self.sleep(self.delay)
            try:
                target = url
                request_params = params
                for redirect in range(4):
                    with self.session.get(target, params=request_params, timeout=(10, 30),
                                          stream=True, allow_redirects=False) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            target = urljoin(response.url, response.headers['Location'])
                            parts = urlsplit(target)
                            if (parts.hostname != original_host or parts.scheme not in ('http', 'https')
                                    or parts.username or parts.password):
                                raise ValueError('cross-host or invalid metadata redirect')
                            request_params = None
                            continue
                        if response.status_code == 429 or 500 <= response.status_code < 600:
                            raise requests.HTTPError('transient HTTP {}'.format(response.status_code),
                                                     response=response)
                        response.raise_for_status()
                        chunks, size = [], 0
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > 4 * 1024 * 1024:
                                raise ValueError('metadata exceeds 4 MiB limit')
                            chunks.append(chunk)
                        return b''.join(chunks).decode('utf-8-sig')
                raise ValueError('too many metadata redirects')
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as error:
                response = getattr(error, 'response', None)
                transient = response is None or response.status_code == 429 or response.status_code >= 500
                if not transient or attempt == 2:
                    raise
                retry_after = response.headers.get('Retry-After', '') if response is not None else ''
                self.sleep(min(60, int(retry_after)) if retry_after.isdigit() else 2 ** (attempt + 1))
        raise AssertionError('unreachable')

    def license(self, source, link):
        guid = link.get('guid')
        if not isinstance(guid, str) or not guid.strip():
            raise ValueError('missing harvest GUID')
        if source == 'chemotion':
            # Same Container endpoint and query convention as fetch_stage.
            url = link['source_url'].rstrip('/') + '/download_json?Container&id=0&inchikey=' + quote(guid, safe='')
            return dataset_license(json.loads(self.get(url)))
        if source == 'nmrxiv':
            url = link['source_url'].rstrip('/') + '/schemas/bioschemas/' + quote(guid, safe='')
            return dataset_license(json.loads(self.get(url)))
        if source == 'massbank':
            page = self.get('https://massbank.eu/MassBank/RecordDisplay', {'id': guid})
            parser = JsonLDParser()
            parser.feed(page)
            nodes = []
            for block in parser.blocks:
                try:
                    metadata = json.loads(block)
                except ValueError:
                    # Existing MassBank scraper accepts Python-literal JSON-LD.
                    metadata = ast.literal_eval(block)
                nodes.extend(metadata if isinstance(metadata, list) else metadata.get('@graph', [metadata]))
            return dataset_license(nodes)
        raise ValueError('unsupported source')


def record(package):
    return dict(dataset_id=package.get('id'), dataset_name=package.get('name'),
                dataset_url=package.get('url'), current_license_id=package.get('license_id'),
                source=package.get('_source'), source_license=None,
                resolved_license_id=None, status=None, reason='')


def outcome(row, status, reason):
    row.update(status=status, reason=reason)
    return row


def process(candidate, store, client, mode, source, malformed):
    row = record(candidate)
    try:
        package, links = store.load(candidate)
        row = record(package)
        if package['type'] != 'dataset' or package['state'] != 'active':
            return outcome(row, 'skipped', 'not an active dataset')
        kind, link, reason = classify(package, links)
        row['source'] = kind
        if kind is None or (source != 'all' and source != kind):
            return outcome(row, 'source_unknown', reason if kind is None else 'requested source mismatch')
        current = package.get('license_id')
        if current in store.registered():
            row['resolved_license_id'] = current
            return outcome(row, 'already_correct', 'registered license preserved')
        missing = current is None or not current.strip()
        if not missing and not malformed:
            return outcome(row, 'skipped', 'non-missing license; use --include-malformed')
        store.release()  # Never keep a read transaction open during network I/O.
        if missing:
            if link is None:
                return outcome(row, 'source_unknown', reason)
            value = client.license(kind, link)
        else:
            value = current  # Only normalize the supplied historical value.
        # Bound report size; never store a complete source metadata object.
        row['source_license'] = json.dumps(value, ensure_ascii=True)[:4096]
        if value is None or value == '' or value == [] or value == {}:
            return outcome(row, 'missing_at_source', 'no source license/rights')
        if isinstance(value, str) and not value.strip():
            return outcome(row, 'missing_at_source', 'blank source license/rights')
        if isinstance(value, (list, tuple)) and not list(extract_license_values(value)):
            return outcome(row, 'missing_at_source', 'empty source license/rights list')
        resolved = resolve_license_id(value, store.context, package['id'])
        row['resolved_license_id'] = resolved
        if not resolved:
            return outcome(row, 'unknown_license', 'source value not resolvable in CKAN registry')
        # Re-read while holding only this package's lock. Do not overwrite a
        # concurrent harvest/manual edit or patch a package whose provenance changed.
        latest, latest_links = store.load({'id': package['id']}, lock=mode == 'run')
        latest_kind, latest_link, _ = classify(latest, latest_links)
        if latest['type'] != 'dataset' or latest['state'] != 'active':
            return outcome(row, 'skipped', 'package no longer an active dataset')
        if latest_kind != kind or latest_link != link or latest.get('url') != package.get('url'):
            return outcome(row, 'source_unknown', 'source association changed during resolution')
        registered = store.registered()
        if latest.get('license_id') in registered:
            row['current_license_id'] = latest['license_id']
            return outcome(row, 'already_correct', 'concurrent registered license preserved')
        if latest.get('license_id') != current:
            return outcome(row, 'skipped', 'license changed concurrently; retry in a new run')
        if resolved not in registered:
            return outcome(row, 'unknown_license', 'resolved ID is no longer registered')
        if mode == 'dry-run':
            return outcome(row, 'would_repair', 'validated; no package action called')
        patched = store.patch(package['id'], resolved)
        if not isinstance(patched, dict) or patched.get('license_id') != resolved:
            raise ValueError('package_patch returned an unexpected license; inspect package (action may have committed)')
        return outcome(row, 'repaired', 'package_patch completed')
    except Exception as error:
        return outcome(row, 'failed', '{}: {}'.format(type(error).__name__, error)[:1000])
    finally:
        store.release()


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    directory_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class Reports:
    """Fsynced JSONL is authoritative; CSVs are regenerated from it on resume."""
    def __init__(self, directory):
        self.path = Path(directory)
        self.lock = (self.path / '.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError('run directory is already locked by another process')
        self.streams, self.writers, self.rows = {}, {}, []
        self.journal = None
        try:
            path = self.path / 'processed.jsonl'
            if path.exists():
                # A killed process can leave an incomplete final append. Only
                # that unfinished line is discarded, never a complete bad line.
                with path.open('r+b') as stream:
                    position = 0
                    for line in stream:
                        if not line.endswith(b'\n'):
                            stream.truncate(position)
                            break
                        self.rows.append(json.loads(line.decode('utf-8')))
                        position = stream.tell()
            self.journal = path.open('a', encoding='utf-8')
            self.log = (self.path / 'repair.log').open('a', encoding='utf-8')
            if not (self.path / 'candidates.csv').exists():
                with (self.path / 'candidates.csv').open('w', newline='', encoding='utf-8') as stream:
                    csv.DictWriter(stream, fieldnames=FIELDS).writeheader()
            for status in STATUSES:
                stream = (self.path / (status.replace('_', '-') + '.csv')).open(
                    'w', newline='', encoding='utf-8')
                self.streams[status] = stream
                writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction='ignore')
                writer.writeheader()
                self.writers[status] = writer
            for row in self.rows:
                self.writers[row['status']].writerow(row)
            self.checkpoint()
        except BaseException:
            self.close()
            raise

    def append(self, row, key):
        row = dict(row, candidate_key=key)
        self.journal.write(json.dumps(row, ensure_ascii=True) + '\n')
        self.journal.flush()
        os.fsync(self.journal.fileno())
        self.rows.append(row)
        self.writers[row['status']].writerow(row)
        self.log.write('{} {} {} {}\n'.format(datetime.utcnow().isoformat(),
                       row['dataset_name'], row['status'], row['reason']))
        self.log.flush()

    def checkpoint(self):
        for stream in self.streams.values():
            stream.flush()
            os.fsync(stream.fileno())

    def summary(self, state, total, options):
        self.checkpoint()
        value = dict(options, state=state, total_candidates=total, processed=len(self.rows),
                     counts=dict(Counter(row['status'] for row in self.rows)),
                     updated_at=datetime.utcnow().isoformat() + 'Z')
        atomic_json(self.path / 'summary.json', value)
        return value

    def close(self):
        for stream in self.streams.values():
            stream.close()
        if self.journal:
            self.journal.close()
        if hasattr(self, 'log'):
            self.log.close()
        self.lock.close()


def execute(store, client, mode, source=None, malformed=None, names=None,
            limit=None, offset=0, batch_size=100, output_dir=None, resume=None,
            site='', emit=print):
    """Create immutable candidates before patching; resume only this snapshot."""
    if mode not in ('dry-run', 'run'):
        raise ValueError('explicit dry-run or run mode required')
    if resume:
        directory = Path(resume)
        saved_options = json.loads((directory / 'run.json').read_text())
        if saved_options['mode'] != mode or saved_options['site'] != site:
            raise ValueError('resume mode/site does not match original run')
        if source is not None and source != saved_options['source']:
            raise ValueError('resume source does not match original run')
        if malformed is not None and malformed != saved_options['malformed']:
            raise ValueError('resume license selection mode does not match original run')
        if names is not None or limit is not None or offset or output_dir:
            raise ValueError('resume uses original candidates; no selection/output overrides allowed')
    else:
        parent = Path(output_dir or '/var/tmp')
        parent.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(
            prefix='harvester4chem-license-repair-' + datetime.utcnow().strftime('%Y%m%dT%H%M%SZ-'),
            dir=str(parent)))
    emit('Reports: ' + str(directory))
    reports = Reports(directory)
    candidates, options, state = [], {}, 'failed'
    try:
        if resume:
            options = saved_options
            source, malformed = options['source'], options['malformed']
            candidates = json.loads((directory / 'candidates.json').read_text())
        else:
            source, malformed = source or 'all', bool(malformed)
            options = dict(mode=mode, source=source, malformed=malformed, site=site,
                           limit=limit, offset=offset, version=1)
            store.progress = emit
            candidates = store.candidates(names, malformed, source, offset, limit)
            store.release()
            atomic_json(directory / 'candidates.json', candidates)
            with (directory / 'candidates.csv').open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(record(candidate) for candidate in candidates)
            atomic_json(directory / 'run.json', options)
        done = {row['candidate_key'] for row in reports.rows}
        for candidate in candidates:
            key = candidate.get('id') or candidate['name']
            if key in done:
                continue
            result = process(candidate, store, client, mode, source, malformed)
            reports.append(result, key)
            done.add(key)
            if len(done) % batch_size == 0:
                summary = reports.summary('running', len(candidates), options)
                emit('Processed {}/{} {}'.format(len(done), len(candidates), summary['counts']))
        state = 'complete'
    except KeyboardInterrupt:
        state = 'interrupted'
        emit('Interrupted; resume reports at ' + str(directory))
    finally:
        try:
            summary = reports.summary(state, len(candidates), options)
            emit('Processed {}/{} {}'.format(summary['processed'], len(candidates), summary['counts']))
        finally:
            reports.close()
            store.release()
            client.close()
    return directory, summary
