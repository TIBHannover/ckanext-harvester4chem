# Historical license repair (CKAN 2.9 / Python 3.7)

The command is registered in the existing `harvester4chem` group through the
existing plugin's IClick implementation. It does not run automatically.

```text
ckan -c /etc/ckan/default/ckan.ini harvester4chem repair-licenses
  (--dry-run | --run)
  [--source chemotion|nmrxiv|massbank|all]
  [--dataset NAME | --manifest PATH]
  [--missing-only | --include-malformed]
  [--limit N] [--offset N] [--batch-size N]
  [--output-dir PARENT] [--resume RUN_DIRECTORY]
```

Exactly one execution mode is required. Default source is `all`; default license
selection is missing-only. `--include-malformed` additionally selects nonempty
IDs absent from the current CKAN registry. It never replaces registered IDs,
even if they contain spaces or say `Deed`, or are `notspecified`.

## Selection and provenance

Bulk selection reads only active `type='dataset'` packages with NULL or
`btrim(license_id)=''` (plus unregistered IDs when requested). It excludes
molecule packages and inactive datasets. Ordering is by package name then ID.
Source filtering happens before offset/limit. Unknown provenance is included
for reporting and counts toward the limit, but can never trigger a guessed
MassBank fetch or patch. Known datasets from other sources are excluded.

`--dataset` selects exactly the supplied package name and cannot combine with
limit/offset. Manifests contain exact package names, one per line; blank lines
and whole-line `#` comments are ignored, duplicates rejected, names sorted.
Explicit names are audited even if missing, inactive, or already licensed; they
are reported as failed/skipped/already-correct, never silently modified.

The installed CKAN harvest model was inspected. Provenance follows:

```text
package.id -> harvest_object.package_id
harvest_object.harvest_source_id -> harvest_source.id
harvest_object.harvest_job_id -> harvest_job.id -> harvest_job.source_id
```

Current harvest objects are preferred. If none are current, successfully
completed objects (`import_finished` set, `state='COMPLETE'`) are considered,
newest first. Multiple originating source IDs, conflicting direct/job source
IDs, or different current GUIDs are rejected as `source_unknown`.
`harvest_source.type` is the harvester's `info()['name']`, not the CKAN plugin
configuration name. The known repository implementations are matched with their
source URL host:

| Source | Harvester type | Endpoint host |
| --- | --- | --- |
| Chemotion | `Chemotion repo Harvester` | `chemotion-repository.net` and subdomains |
| nmrXiv | `nmrXiv Swagger Harvester` | `nmrxiv.org` and subdomains |
| MassBank | `Bioschema Sitemap ` (whitespace ignored) | `massbank.eu` and subdomains |

The repository explicitly implements MassBank in `bioschemascrap.py`, including
its record-display URL. This does **not** establish that every production
MassBank package uses that source type: the command checks each stored
association. An OAI-PMH or other unexpected source is reported as unknown,
not silently routed through the scraper. No “all remaining packages = MassBank”
classification exists. Source titles are not trusted for classification.

Without harvest links, Chemotion/nmrXiv dataset URL host checks can classify the
source for reporting or normalizing an existing value. They do not invent API
GUIDs: missing-license retrieval without a harvest identifier is reported as
`source_unknown`. A MassBank dataset URL alone is insufficient.

Optional read-only production source inventory, not executed by this task:

```sql
SELECT s.id, s.type, s.url, COUNT(DISTINCT p.id) AS datasets
FROM harvest_source s
JOIN harvest_object h ON h.harvest_source_id = s.id AND h.current
JOIN package p ON p.id = h.package_id
WHERE p.type = 'dataset' AND p.state = 'active'
GROUP BY s.id, s.type, s.url
ORDER BY s.type, s.url;
```

## Metadata retrieval and resolution

No gather, fetch, import, or resource-rebuild method is called.

- Chemotion: one GET to the stored source endpoint plus
  `/download_json?Container&id=0&inchikey=<encoded harvest GUID>`, matching the
  existing Container API convention without the duplicate GET in fetch_stage.
- nmrXiv: one GET to the stored source endpoint plus
  `/schemas/bioschemas/<encoded harvest GUID>`.
- MassBank: GET `https://massbank.eu/MassBank/RecordDisplay?id=<harvest GUID>`;
  parse embedded JSON-LD and select the Dataset node, never the molecule node.
  The legacy scraper's Python-literal representation is supported with bounded
  `ast.literal_eval`, never `eval`. Multiple Dataset nodes are rejected rather
  than choosing a positional array entry.

Only `license` or `rights` from the Dataset metadata is passed to the shared
`license_utils.resolve_license_id`. Missing/empty metadata is `missing_at_source`;
unregistered values are `unknown_license`. Existing malformed values are resolved
directly without a network request. The only permanent-resolver change is a
bounded alias for observed CC `Deed`, `Legal Code`, and `CC0 1.0 Universal` labels.
Targets always come from CKAN's current `license_list`, including space-containing
IDs if those are what the registry actually exposes. No registry file is edited.

Requests share a Session; connect/read timeouts are 10/30 seconds, with at most
three attempts for timeouts, connection errors, 429 and 5xx. Backoff is bounded;
numeric Retry-After is honored up to 60 seconds. At least 0.5 seconds separates
attempts. 404 and invalid JSON are reported per dataset. Responses are limited to
4 MiB and three redirects; cross-host redirects and URL credentials are rejected.
No linked downloadable resource is fetched.

## Writes, concurrency and interruption

Dry-run performs reads, metadata requests and report writes only. It never calls
`package_patch`, commits a database transaction, or invokes indexing/chemistry.

Run rechecks package existence, active dataset type, source association, current
license and registry membership immediately before calling:

```python
get_action('package_patch')(context, {'id': dataset_id, 'license_id': resolved_id})
```

The context includes model/session, `user='harvest'`, `auth_user_obj=None` and
`ignore_auth=True`. The recheck uses a per-package `SELECT ... FOR UPDATE NOWAIT`
lock during run only. A concurrent valid license is preserved; changed provenance,
URL or license prevents patching. A busy row goes to `failed.csv` for a later retry.
No lock or read transaction is held while retrieving source metadata.

CKAN's normal package action performs its own commit and indexing. The command
never directly executes a SQL UPDATE, and intentionally supplies no title, notes,
resources, relationships or other fields. CKAN's normal metadata timestamp,
activity and configured package-action hooks may run; those standard action
side effects are not suppressed. There is no explicit Solr rebuild or call to
molecule/RDKit synchronization anywhere in this repair path.

`--batch-size` (default 100) controls CSV/summary checkpoints and progress, **not**
a multi-package transaction. One failed package does not roll back earlier
successful package actions. Per-package read/error transactions are rolled back.
CLI exit is nonzero on interruption or any failed datasets; other unresolved
statuses are reported in the summary and are not treated as action failures.

## Reports and resume

Every invocation creates a private timestamped directory under `/var/tmp`, or
under the parent supplied by `--output-dir`. It contains:

```text
candidates.csv          candidates.json       run.json
repaired.csv            would-repair.csv      already-correct.csv
missing-at-source.csv   unknown-license.csv   source-unknown.csv
failed.csv              skipped.csv           summary.json
processed.jsonl         repair.log            .lock
```

CSVs contain dataset ID/name, source, URL, old license, source license, resolved
ID, status and reason. Source license values are capped at 4096 report characters;
complete metadata documents are not recorded. `processed.jsonl` is the append-only
source of truth and is flushed/fsynced after every finalized result. CSVs are
checkpointed in batches, and regenerated from the journal when resuming.

`candidates.json` is an immutable selection snapshot, written before any patch.
`--resume` uses it instead of selecting missing rows again, so changes in the
missing population cannot shift offsets. Resume must use the original mode and
site; provided source/license-selection flags must agree. Selection/output
options cannot be overridden. Batch size may change. A file lock prevents two
processes from using the same report directory simultaneously.

Every finalized status, including failures and unresolved values, is skipped on
resume. To retry these after fixing the cause, create a **new** manifest of their
`dataset_name` values and start a new run. Do not edit the snapshot or journal.
If killed between a successful package commit and journal append, the retry
rechecks the database and reports `already_correct` without patching again.
An incomplete final JSONL line is discarded on resume; other journal corruption
is an error. Ctrl+C flushes reports, marks the summary interrupted, and prints
the directory. If interrupted during initial selection before `run.json` exists,
no patches have occurred: start a fresh invocation instead of resuming it.

Offsets are for slicing a **new** selection. Do not increment offsets across
successive missing-only repair runs: successful writes shrink that population.
Use a complete snapshot/resume or an explicit manifest for stable operational batches.

## Operator examples (not executed)

```bash
# Verify registration
ckan -c /etc/ckan/default/ckan.ini harvester4chem --help

# First intended single-package dry-run
ckan -c /etc/ckan/default/ckan.ini harvester4chem repair-licenses \
  --dataset 10-14272-aafjwsujjwijew-uhfffaoysa-n-chmo0000470 --dry-run

# Only after reviewing that report, the corresponding write command
ckan -c /etc/ckan/default/ckan.ini harvester4chem repair-licenses \
  --dataset 10-14272-aafjwsujjwijew-uhfffaoysa-n-chmo0000470 --run

# All nmrXiv candidates, including unregistered human-readable labels
ckan -c /etc/ckan/default/ckan.ini harvester4chem repair-licenses \
  --source nmrxiv --include-malformed --dry-run
ckan -c /etc/ckan/default/ckan.ini harvester4chem repair-licenses \
  --source nmrxiv --include-malformed --run

# Chemotion batch: missing licenses only
ckan -c /etc/ckan/default/ckan.ini harvester4chem repair-licenses \
  --source chemotion --missing-only --limit 500 --offset 0 --batch-size 100 --dry-run

# MassBank inspection, with no writes
ckan -c /etc/ckan/default/ckan.ini harvester4chem repair-licenses \
  --source massbank --dry-run

# Resume an interrupted write run using its actual printed directory
ckan -c /etc/ckan/default/ckan.ini harvester4chem repair-licenses \
  --source chemotion --run --resume /var/tmp/harvester4chem-license-repair-TIMESTAMP-SUFFIX

# Explicit manifest
ckan -c /etc/ckan/default/ckan.ini harvester4chem repair-licenses \
  --manifest /path/to/package-names.txt --include-malformed --dry-run
```

Read-only verification after an operator-authorized single-package run:

```sql
SELECT name, license_id FROM package
WHERE name = '10-14272-aafjwsujjwijew-uhfffaoysa-n-chmo0000470';
```

## Tests

Run in a Python 3.7 CKAN environment with requests, Click and SQLAlchemy:

```bash
python -m unittest discover -s ckanext/harvester4chem/tests -p 'test_license*.py' -v
```

The local IDE interpreter is a minimal venv. The tests can use the existing CKAN
site-packages **after** the standard library (PYTHONPATH would shadow stdlib
`typing` with a legacy backport):

```bash
./venv/bin/python - <<'PY'
import sys, unittest
sys.path.append('/usr/lib/ckan/default/lib/python3.7/site-packages')
suite = unittest.defaultTestLoader.discover(
    'ckanext/harvester4chem/tests', pattern='test_license*.py')
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
PY
```

CKAN actions and HTTP responses are mocked. The actual candidate SELECT is also
exercised against an isolated in-memory SQLite fixture. Click tests execute the
real command/group definitions without importing unrelated chemistry commands.
Production PostgreSQL row-locking, live API metadata and configured package-action
hooks require the operator's single-dataset deployment test; no repair invocation
against production was made during implementation.
