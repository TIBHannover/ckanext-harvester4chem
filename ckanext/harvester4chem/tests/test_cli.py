import pytest
from click.testing import CliRunner
import copy
import json

from ckanext.harvester4chem import cli
from ckanext.harvester4chem.cli import VERIFY_SQL
from ckanext.harvester4chem import molecule_sync


LEGACY_DATASET_AUDIT = "legacy_dataset_chemistry_missing_molecule_package"
DATASET_EXTRA_AUDIT = "dataset_extra_inchikey_missing_molecule_package"
ETHANOL_KEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"


def _sql(label):
    return " ".join(str(VERIFY_SQL[label]).upper().split())


def _normalize(value):
    if value is None:
        return None
    return value.strip().strip('"').strip().upper()


def _missing_pairs(dataset_keys, molecule_keys):
    """Small deterministic model of both verifier queries' set semantics."""
    active_molecule_keys = {
        _normalize(item[2])
        for item in molecule_keys
        if item[1] and item[3] and _normalize(item[2])
    }
    pairs = {
        (item[0], _normalize(item[2]))
        for item in dataset_keys
        if item[1] and _normalize(item[2])
    }
    return len([pair for pair in pairs
                if pair[1] not in active_molecule_keys])


@pytest.mark.parametrize("flag, expected", [
    (None, None), ("--write-legacy", True), ("--no-write-legacy", False)])
def test_sync_package_legacy_override_is_optional(monkeypatch, flag, expected):
    captured = {}
    package = {"id": "dataset-id", "smiles": "CCO", "title": "Ethanol"}
    monkeypatch.setattr(cli.toolkit, "get_action",
                        lambda name: lambda context, data: package)

    def synchronize(**kwargs):
        captured.update(kwargs)
        return {"legacy": {"status": "skipped",
                           "relationship": "skipped"}}

    monkeypatch.setattr(cli, "synchronize_molecule", synchronize)
    monkeypatch.setattr(cli.model.Session, "rollback", lambda: None)
    args = ["sync-package", "dataset-id", "--dry-run"]
    if flag:
        args.append(flag)
    result = CliRunner().invoke(cli.harvester4chem, args)
    assert result.exit_code == 0
    assert captured["write_legacy"] is expected
    assert captured["dry_run"] is True


def test_verification_queries_are_read_only_and_cover_required_checks():
    assert set(VERIFY_SQL) == {
        "legacy_relationships_missing_public_molecule",
        "duplicate_legacy_package_molecule_relationships",
        LEGACY_DATASET_AUDIT,
        DATASET_EXTRA_AUDIT,
        "ambiguous_duplicate_molecule_packages",
        "molecule_packages_missing_rdk_molecule",
        "molecule_packages_with_rdkit_inchi_key_mismatch",
        "rdk_molecules_missing_fingerprints",
        "null_fingerprints",
        "dataset_molecule_package_relationships_missing",
        "ckan_relationships_referencing_inactive_or_missing_packages",
    }
    sql = " ".join(str(query) for query in VERIFY_SQL.values()).upper()
    assert "INSERT " not in sql
    assert "UPDATE " not in sql
    assert "DELETE " not in sql
    assert "M.MOLECULE_ID = R.MOLECULES_ID" not in sql
    assert "PACKAGE_EXTRA" in sql
    assert "STATE='ACTIVE'" in sql
    missing_relationship_sql = _sql(
        "dataset_molecule_package_relationships_missing"
    )
    assert "NOT EXISTS" in missing_relationship_sql
    assert "LEFT JOIN RELATIONSHIP_RELATIONSHIP" not in missing_relationship_sql


def test_legacy_dataset_audit_follows_only_legacy_relationship_mapping():
    sql = _sql(LEGACY_DATASET_AUDIT)
    assert "FROM PUBLIC.MOLECULE_REL_DATA REL" in sql
    assert "JOIN PUBLIC.MOLECULES LEGACY ON LEGACY.ID=REL.MOLECULES_ID" in sql
    assert "DATASET.ID=REL.PACKAGE_ID" in sql
    assert "DATASET.TYPE='DATASET'" in sql
    assert "DATASET.STATE='ACTIVE'" in sql
    assert "RDK.MOLECULES" not in sql
    assert "MOLECULE_ID" not in sql


def test_legacy_dataset_audit_uses_distinct_normalized_pairs_and_not_exists():
    sql = _sql(LEGACY_DATASET_AUDIT)
    assert "SELECT DISTINCT REL.PACKAGE_ID AS DATASET_ID" in sql
    assert "LEGACY.INCHI_KEY IS NOT NULL" in sql
    assert "BTRIM(LEGACY.INCHI_KEY)<>''" in sql
    assert "DM.NORMALIZED_INCHI_KEY<>''" in sql
    assert "NOT EXISTS" in sql
    assert "MOLECULE_PACKAGE.TYPE='MOLECULE'" in sql
    assert "MOLECULE_PACKAGE.STATE='ACTIVE'" in sql
    assert "MOLECULE_KEY.STATE='ACTIVE'" in sql
    assert "MOLECULE_KEY.VALUE IS NOT NULL" in sql
    assert "BTRIM(MOLECULE_KEY.VALUE)<>''" in sql
    assert "UPPER(BTRIM(BTRIM(BTRIM(MOLECULE_KEY.VALUE), '\"')))=" in sql


def test_authoritative_metric_returns_zero_for_matching_active_package():
    datasets = [("dataset-1", True, "KEY")]
    molecules = [("molecule-1", True, "KEY", True)]
    assert _missing_pairs(datasets, molecules) == 0


def test_authoritative_metric_reports_one_without_matching_package():
    assert _missing_pairs([("dataset-1", True, "KEY")], []) == 1


def test_authoritative_metric_ignores_inactive_dataset():
    assert _missing_pairs([("dataset-1", False, "KEY")], []) == 0


def test_authoritative_metric_ignores_inactive_molecule_package_and_extra():
    datasets = [("dataset-1", True, "KEY")]
    assert _missing_pairs(
        datasets, [("molecule-1", False, "KEY", True)]
    ) == 1
    assert _missing_pairs(
        datasets, [("molecule-1", True, "KEY", False)]
    ) == 1


def test_authoritative_metric_ignores_null_and_blank_keys():
    datasets = [
        ("dataset-1", True, None),
        ("dataset-1", True, "  "),
        ("dataset-1", True, ' " " '),
    ]
    assert _missing_pairs(datasets, []) == 0


def test_authoritative_metric_normalizes_quotes_whitespace_and_case():
    datasets = [("dataset-1", True, '  "abc-def"  ')]
    molecules = [("molecule-1", True, " ABC-DEF ", True)]
    assert _missing_pairs(datasets, molecules) == 0


def test_authoritative_metric_is_not_inflated_by_duplicate_packages():
    datasets = [("dataset-1", True, "KEY")]
    molecules = [
        ("molecule-1", True, "KEY", True),
        ("molecule-2", True, "KEY", True),
    ]
    assert _missing_pairs(datasets, molecules) == 0


def test_authoritative_metric_counts_distinct_dataset_chemical_pairs():
    datasets = [
        ("dataset-1", True, "KEY-A"),
        ("dataset-1", True, "key-a"),
        ("dataset-1", True, "KEY-B"),
        ("dataset-2", True, "KEY-A"),
    ]
    assert _missing_pairs(datasets, []) == 3


def test_dataset_extra_audit_has_separate_safe_query_semantics():
    sql = _sql(DATASET_EXTRA_AUDIT)
    assert "FROM PUBLIC.PACKAGE DATASET" in sql
    assert "JOIN PUBLIC.PACKAGE_EXTRA DATASET_KEY" in sql
    assert "DATASET.TYPE='DATASET'" in sql
    assert "DATASET.STATE='ACTIVE'" in sql
    assert "DATASET_KEY.KEY='INCHI_KEY'" in sql
    assert "DATASET_KEY.STATE='ACTIVE'" in sql
    assert "SELECT DISTINCT DATASET.ID AS DATASET_ID" in sql
    assert "NOT EXISTS" in sql
    assert "MOLECULE_PACKAGE.TYPE='MOLECULE'" in sql
    assert "MOLECULE_PACKAGE.STATE='ACTIVE'" in sql
    assert "MOLECULE_KEY.STATE='ACTIVE'" in sql


def test_dataset_extra_audit_matches_and_reports_missing_pairs_separately():
    dataset_extras = [
        ("dataset-1", True, ' "key-a" '),
        ("dataset-1", True, "KEY-B"),
        ("dataset-1", True, "key-b"),
        ("dataset-2", False, "KEY-C"),
        ("dataset-3", True, " "),
    ]
    molecule_extras = [("molecule-1", True, "KEY-A", True)]
    assert _missing_pairs(dataset_extras, molecule_extras) == 1


class BackfillResult(object):
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def scalar(self):
        return self.rows[0][0] if self.rows else None


class BackfillSession(object):
    def __init__(self, packages=None):
        self.packages = packages or {}
        self.rows = []
        self.sql = []
        self.commits = 0
        self.rollbacks = 0
        self.snapshot = None

    def execute(self, statement, params=None):
        sql, params = " ".join(str(statement).split()), params or {}
        self.sql.append((sql, copy.deepcopy(params)))
        if sql.startswith("LOCK TABLE"):
            self.snapshot = copy.deepcopy(self.rows)
            return BackfillResult()
        if sql.startswith("SELECT id, name, title, type, state"):
            package = self.packages.get(params["name"])
            return BackfillResult([(
                package["id"], package["name"], package.get("title"),
                package["type"], package["state"]
            )] if package else [])
        if sql.startswith("SELECT key, value, state"):
            package = next(item for item in self.packages.values()
                           if item["id"] == params["package_id"])
            return BackfillResult([
                (item["key"], item["value"], item.get("state", "active"))
                for item in package.get("extras", [])
                if item.get("state", "active") == "active"
            ])
        raise AssertionError(sql)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        if self.snapshot is not None:
            self.rows = copy.deepcopy(self.snapshot)


def package(name, state="active", package_type="molecule", smiles="CCO",
            key="LFQSCWFLJHTTHZ-UHFFFAOYSA-N"):
    extras = [{"key": "inchi_key", "value": key, "state": "active"}]
    if smiles is not None:
        extras.append({"key": "smiles", "value": smiles, "state": "active"})
    return {"id": "id-" + name, "name": name, "title": "Ethanol",
            "type": package_type, "state": state, "extras": extras}


def test_manifest_parsing_ignores_blank_and_comment_lines(tmp_path):
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("\n# production batch\n mol-one \n\n mol-two\n")
    assert cli.parse_repair_manifest(str(manifest)) == ["mol-one", "mol-two"]


def test_manifest_rejects_duplicate_names(tmp_path):
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("mol-one\nmol-one\n")
    with pytest.raises(Exception, match="duplicate manifest entry"):
        cli.parse_repair_manifest(str(manifest))


@pytest.mark.parametrize("args", [[], ["--dry-run", "--apply"]])
def test_repair_requires_exactly_one_mode(tmp_path, args):
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("mol-one\n")
    result = CliRunner().invoke(
        cli.harvester4chem,
        ["repair-missing-rdk", "--manifest", str(manifest)] + args)
    assert result.exit_code != 0
    assert "exactly one" in result.output


@pytest.mark.parametrize("item, reason", [
    (package("inactive", state="deleted"), "not active"),
    (package("dataset", package_type="dataset"), "not molecule"),
    (package("no-structure", smiles=None), "missing InChI/SMILES"),
    (package("bad-key", key="AAAAAAAAAAAAAA-BBBBBBBBBB-C"), "mismatch"),
])
def test_backfill_package_metadata_rejections(item, reason):
    class NoSql(object):
        def execute(self, statement, params=None):
            raise AssertionError("validation should fail before PostgreSQL SQL")
    with pytest.raises(molecule_sync.MoleculeSyncError, match=reason):
        molecule_sync.validate_rdk_backfill_package(item, NoSql())


def test_existing_rdkit_molecule_is_rejected():
    class Existing(object):
        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            if sql.startswith("SELECT mol_from_smiles"):
                return BackfillResult([(True, True, True)])
            if sql.startswith("SELECT molecule_id"):
                values = molecule_sync.normalize_structure(smiles="CCO")
                return BackfillResult([(7, values["inchi_key"],
                                         values["inchi_code"])])
            raise AssertionError(sql)
    with pytest.raises(molecule_sync.MoleculeSyncError, match="already present"):
        molecule_sync.validate_rdk_backfill_package(package("existing"), Existing())


def _mock_batch(monkeypatch, count=28, fail_at=None):
    packages = {"mol-{0}".format(i): package("mol-{0}".format(i))
                for i in range(count)}
    session = BackfillSession(packages)

    def validate(item, current_session):
        return item

    def create(item, current_session):
        index = int(item["name"].split("-")[-1])
        if index == fail_at:
            raise molecule_sync.MoleculeSyncError("fingerprint insert failed")
        current_session.rows.append(item["name"])
        return 100 + index

    monkeypatch.setattr(cli, "validate_rdk_backfill_package", validate)
    monkeypatch.setattr(cli, "create_validated_rdk_backfill", create)
    return list(packages), session


def test_successful_28_style_apply_is_one_transaction(monkeypatch):
    names, session = _mock_batch(monkeypatch)
    results, summary = cli.repair_missing_rdk(names, "apply", session)
    assert len(results) == 28
    assert summary == {"mode": "apply", "requested": 28, "validated": 28,
                       "created": 28, "failed": 0, "rolled_back": False}
    assert session.commits == 1 and session.rollbacks == 0
    assert len(session.rows) == 28


def test_dry_run_executes_inserts_then_rolls_back(monkeypatch):
    names, session = _mock_batch(monkeypatch, count=2)
    _, summary = cli.repair_missing_rdk(names, "dry-run", session)
    assert summary["created"] == 2 and summary["rolled_back"] is True
    assert session.rows == []
    assert session.commits == 0 and session.rollbacks == 1


def test_final_insert_failure_rolls_back_complete_batch(monkeypatch):
    names, session = _mock_batch(monkeypatch, count=3, fail_at=2)
    results, summary = cli.repair_missing_rdk(names, "apply", session)
    assert results[-1]["reason"] == "fingerprint insert failed"
    assert results[0]["status"] == "rolled_back"
    assert summary["created"] == 0 and summary["rolled_back"] is True
    assert session.rows == [] and session.commits == 0


def test_preflight_failure_writes_nothing(monkeypatch):
    names, session = _mock_batch(monkeypatch, count=3)
    original = cli.validate_rdk_backfill_package

    def fail_final(item, current_session):
        if item["name"] == "mol-2":
            raise molecule_sync.MoleculeSyncError("InChIKey mismatch")
        return original(item, current_session)

    monkeypatch.setattr(cli, "validate_rdk_backfill_package", fail_final)
    _, summary = cli.repair_missing_rdk(names, "apply", session)
    assert summary["created"] == 0 and summary["failed"] == 1
    assert session.rows == [] and session.commits == 0


def test_idempotent_second_run_reports_already_present(monkeypatch):
    names, session = _mock_batch(monkeypatch, count=1)

    def validate(item, current_session):
        if item["name"] in current_session.rows:
            raise molecule_sync.MoleculeSyncError(
                "already present in rdk.molecules")
        return item

    monkeypatch.setattr(cli, "validate_rdk_backfill_package", validate)
    _, first = cli.repair_missing_rdk(names, "apply", session)
    results, second = cli.repair_missing_rdk(names, "dry-run", session)
    assert first["created"] == 1
    assert results[0]["reason"] == "already present in rdk.molecules"
    assert second["created"] == 0 and second["failed"] == 1
    assert session.rows == names


def test_backfill_path_has_no_legacy_or_ckan_action_writes(monkeypatch):
    names, session = _mock_batch(monkeypatch, count=1)
    monkeypatch.setattr(cli.toolkit, "get_action",
                        lambda name: pytest.fail("CKAN action called: " + name))
    cli.repair_missing_rdk(names, "dry-run", session)
    sql = " ".join(item[0].lower() for item in session.sql)
    assert "public.molecules" not in sql
    assert "molecule_rel_data" not in sql
    assert "relationship_relationship" not in sql


def test_all_technical_alternate_names_are_filtered():
    values = molecule_sync.normalize_structure(smiles="CCO")
    item = package("nfdi4chem-mol1067")
    item["title"] = "nfdi4chem-mol1067"
    item["extras"].append({"key": "alternate_name",
                           "value": '["nfdi4chem-mol1067", "Ethanol"]',
                           "state": "active"})
    assert molecule_sync._rdk_names(item, values) == ["Ethanol"]


def test_insert_only_sql_cannot_update_existing_rdk_molecule():
    source = open(molecule_sync.__file__, "r").read()
    assert 'conflict = "DO NOTHING" if insert_only' in source
    assert "commit(" not in source


def duplicate_package(name, title="Ethanol", smiles="CCO", formula="C2H6O",
                      created="2020-01-01", extra=None):
    item = package(name, smiles=smiles)
    item["title"] = title
    item["metadata_created"] = created
    item["active_dataset_relationships"] = 0
    item["extras"].append({"key": "inchi", "value":
                           "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3",
                           "state": "active"})
    if formula is not None:
        item["extras"].append({"key": "mol_formula", "value": formula,
                               "state": "active"})
    item["extras"].extend(extra or [])
    return item


class PairSession(object):
    def __init__(self, references=None, rdk_rows=None):
        self.references = references or {}
        self.rdk_rows = ([(42, "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3",
                           True, True, True)] if rdk_rows is None
                         else rdk_rows)
        self.sql = []

    def execute(self, statement, params=None):
        sql, params = " ".join(str(statement).split()), params or {}
        self.sql.append(sql)
        package_id = params.get("package_id")
        if "FROM public.relationship_relationship" in sql and \
                "object_id IN" in sql:
            return BackfillResult([(self.references.get(
                (package_id, "incoming"), 0),)])
        if "FROM public.relationship_relationship" in sql and \
                "subject_id IN" in sql:
            return BackfillResult([(self.references.get(
                (package_id, "outgoing"), 0),)])
        if "FROM public.molecule_rel_data" in sql:
            return BackfillResult([(self.references.get(
                (package_id, "legacy"), 0),)])
        if "FROM rdk.molecules m LEFT JOIN rdk.fingerprints" in sql:
            return BackfillResult(self.rdk_rows)
        raise AssertionError(sql)


def test_apply_loader_uses_resolved_uuid_for_active_extras():
    class LoaderSession(object):
        def __init__(self):
            self.extra_parameter = None

        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            if "FROM public.package WHERE" in sql:
                return BackfillResult([(
                    "uuid-6123", "nfdi4chem-mol6123", "Canary", "molecule",
                    "active", "2020-01-01")])
            if "FROM public.package_extra" in sql:
                self.extra_parameter = params["package_id"]
                return BackfillResult([
                    ("inchi", '"InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"',
                     "active"),
                    ("inchi_key", ETHANOL_KEY, "active"),
                    ("smiles", "CCO", "active"),
                ])
            if "FROM public.relationship_relationship" in sql:
                return BackfillResult([(0,)])
            raise AssertionError(sql)

    session = LoaderSession()
    package = cli._load_dedup_package(session, "nfdi4chem-mol6123")
    assert session.extra_parameter == "uuid-6123"
    assert cli._package_value(package, "inchi") == \
        "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"


def test_production_canary_package_show_extras_pass_apply_preflight(
        monkeypatch):
    expected = "AAWKCAAKYFTATH-STJFUXCQSA-N"
    authoritative = "InChI=1S/canary"

    def canary(name, created):
        return {
            "id": "id-" + name, "name": name, "title": "Canary molecule",
            "type": "molecule", "state": "active",
            "metadata_created": created, "active_dataset_relationships": 0,
            "extras": [
                {"key": "inchi", "value": '  "' + authoritative + '"  '},
                {"key": "inchi_key", "value": " " + expected + " "},
                {"key": "smiles", "value": " canary-smiles "},
                {"key": "mol_formula", "value": "C2H6O"},
            ],
        }

    packages = {
        "nfdi4chem-mol6123": canary("nfdi4chem-mol6123", "2020-01-01"),
        "nfdi4chem-mol8908": canary("nfdi4chem-mol8908", "2021-01-01"),
    }

    def fake_inchi(value, inchi_key=None, mol_formula=None, exact_mass=None):
        assert value == authoritative and inchi_key == expected
        return {"inchi_code": authoritative, "inchi_key": expected,
                "canonical_smiles": "canary-smiles",
                "calculated_formula": "C2H6O"}

    def fake_smiles(value, inchi_key=None, mol_formula=None, exact_mass=None):
        assert value == "canary-smiles"
        return {"inchi_key": expected}

    monkeypatch.setattr(cli, "normalize_inchi_structure", fake_inchi)
    monkeypatch.setattr(cli, "normalize_smiles_structure", fake_smiles)
    monkeypatch.setattr(
        cli, "_load_dedup_package",
        lambda session, name: packages[name])
    session = PairSession(rdk_rows=[
        (42, authoritative, True, True, True)])
    entry = {"inchi_key": expected,
             "keep_package": "nfdi4chem-mol6123",
             "remove_package": "nfdi4chem-mol8908"}
    keep, remove, plan = cli._dedup_preflight_entry(session, entry)
    assert keep["name"] == entry["keep_package"]
    assert remove["name"] == entry["remove_package"]
    assert plan["status"] == "validated"


def validate_pair(session=None, first=None, second=None):
    first = first or duplicate_package("nfdi4chem-mol100")
    second = second or duplicate_package(
        "nfdi4chem-mol200", created="2021-01-01")
    return cli.validate_duplicate_pair(
        session or PairSession(), ETHANOL_KEY, [first, second])


def test_shared_chemistry_extractor_supports_ckan_package_shapes():
    quoted = {
        "name": "nfdi4chem-mol6123",
        "extras": [
            {"key": "InChI", "value":
             '  "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"  '},
            {"key": "INCHI_KEY", "value": "  " + ETHANOL_KEY + "  "},
            {"key": "smiles", "value": " CCO "},
        ],
    }
    extracted = cli.extract_package_chemistry(quoted)
    assert extracted["values"]["inchi"] == \
        "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"
    assert extracted["values"]["inchi_key"] == ETHANOL_KEY
    assert extracted["values"]["smiles"] == "CCO"
    assert extracted["available_chemistry_extra_keys"] == [
        "inchi", "inchi_key", "smiles"]

    top_level = {"InChI": "  InChI=1S/example  ",
                 "canonical_smiles": " CCO ", "extras": []}
    top = cli.extract_package_chemistry(top_level)
    assert top["values"]["inchi"] == "InChI=1S/example"
    assert top["values"]["canonical_smiles"] == "CCO"


@pytest.mark.parametrize("value,state", [(None, "absent"), ("   ", "blank")])
def test_inchi_failure_reports_package_state_keys_and_stage(value, state):
    bad = duplicate_package("nfdi4chem-mol6123")
    bad["extras"] = [extra for extra in bad["extras"]
                     if extra["key"] != "inchi"]
    if value is not None:
        bad["extras"].append({"key": "inchi", "value": value})
    with pytest.raises(molecule_sync.MoleculeSyncError) as raised:
        validate_pair(first=bad)
    message = str(raised.value)
    assert "nfdi4chem-mol6123" in message
    assert "InChI is " + state in message
    assert "validation_stage=package_inchi_extraction" in message
    assert "inchi_key" in message


def test_invalid_inchi_reports_unparsable_and_stage():
    bad = duplicate_package("nfdi4chem-mol6123")
    for extra in bad["extras"]:
        if extra["key"] == "inchi":
            extra["value"] = "not-an-inchi"
    with pytest.raises(molecule_sync.MoleculeSyncError) as raised:
        validate_pair(first=bad)
    message = str(raised.value)
    assert "nfdi4chem-mol6123" in message
    assert "InChI is unparsable" in message
    assert "validation_stage=package_inchi_parsing" in message


def test_identical_duplicate_pair_validates_and_selects_oldest():
    plan = validate_pair()
    assert plan["keep_package"] == "nfdi4chem-mol100"
    assert plan["remove_package"] == "nfdi4chem-mol200"
    assert plan["equivalent_differing_smiles"] is False


def test_duplicate_chemistry_normalizes_json_quotes_and_whitespace():
    first = duplicate_package("nfdi4chem-mol100")
    for extra in first["extras"]:
        if extra["key"] in ("inchi", "inchi_key", "smiles"):
            extra["value"] = '  "{0}"  '.format(extra["value"])
    assert validate_pair(first=first)["inchi_key"] == ETHANOL_KEY


def test_equivalent_different_smiles_are_allowed():
    plan = validate_pair(second=duplicate_package(
        "nfdi4chem-mol200", smiles="OCC", created="2021-01-01"))
    assert plan["equivalent_differing_smiles"] is True


def test_generated_inchi_key_mismatch_blocks_pair():
    bad = duplicate_package("nfdi4chem-mol200", smiles="CC")
    with pytest.raises(molecule_sync.MoleculeSyncError, match="mismatch"):
        validate_pair(second=bad)


def test_smiles_stereochemistry_mismatch_is_planned_not_blocked():
    authoritative_inchi = (
        "InChI=1S/C3H6O3/c1-2(4)3(5)6/h2,4H,1H3,(H,5,6)/t2-/m0/s1")
    authoritative_key = "JVTAAEKCZFNVCJ-REOHCLBHSA-N"
    first = duplicate_package(
        "nfdi4chem-mol100", smiles="CC(O)C(=O)O", formula="C3H6O3")
    second = duplicate_package(
        "nfdi4chem-mol200", smiles="O=C(O)C(O)C", formula="C3H6O3")
    for item in (first, second):
        for extra in item["extras"]:
            if extra["key"] == "inchi":
                extra["value"] = authoritative_inchi
            elif extra["key"] == "inchi_key":
                extra["value"] = authoritative_key
    session = PairSession(rdk_rows=[
        (42, authoritative_inchi, True, True, True)])
    plan = cli.validate_duplicate_pair(
        session, authoritative_key, [first, second])
    assert plan["status"] == "validated_with_warning"
    assert plan["warning"] == "smiles_stereochemistry_mismatch"
    assert plan["expected_inchi_key"] == authoritative_key
    assert plan["package_smiles_inchi_keys"] == {
        "nfdi4chem-mol100": "JVTAAEKCZFNVCJ-UHFFFAOYSA-N",
        "nfdi4chem-mol200": "JVTAAEKCZFNVCJ-UHFFFAOYSA-N",
    }
    assert plan["smiles_stereochemistry_mismatch_details"] == [
        {"package": "nfdi4chem-mol100", "smiles": "CC(O)C(=O)O",
         "generated_inchi_key": "JVTAAEKCZFNVCJ-UHFFFAOYSA-N",
         "classification": "smiles_stereochemistry_mismatch"},
        {"package": "nfdi4chem-mol200", "smiles": "O=C(O)C(O)C",
         "generated_inchi_key": "JVTAAEKCZFNVCJ-UHFFFAOYSA-N",
         "classification": "smiles_stereochemistry_mismatch"},
    ]
    assert "@" in plan["corrected_canonical_isomeric_smiles"]
    assert plan["corrected_smiles_inchi_key"] == authoritative_key
    assert plan["planned_smiles_replacement"] is True


PRODUCTION_STEREOCHEMISTRY_CASES = [
    ("AUTOLBMXDDTRRT-JGVFFNPUSA-N",
     "AUTOLBMXDDTRRT-UHFFFAOYSA-N"),
    ("OGDVEMNWJVYAJL-LEPYJNQMSA-N",
     "OGDVEMNWJVYAJL-UHFFFAOYSA-N"),
    ("PWKSKIMOESPYIA-BYPYZUCNSA-N",
     "PWKSKIMOESPYIA-UHFFFAOYSA-N"),
    ("UHEFGGUIARHISN-UUKMXZOPSA-N",
     "UHEFGGUIARHISN-LYBXBRPPSA-N"),
    ("YYJWBYNQJLBIGS-SNAWJCMRSA-N",
     "YYJWBYNQJLBIGS-UHFFFAOYSA-N"),
]


@pytest.mark.parametrize("expected_key,smiles_key",
                         PRODUCTION_STEREOCHEMISTRY_CASES)
def test_production_stereochemistry_cases_validate_with_warning(
        monkeypatch, expected_key, smiles_key):
    authoritative_inchi = "InChI=1S/mock"
    corrected_smiles = "corrected-isomeric-smiles"

    def fake_inchi(value, inchi_key=None, mol_formula=None, exact_mass=None):
        assert inchi_key == expected_key
        return {"inchi_code": authoritative_inchi,
                "inchi_key": expected_key,
                "canonical_smiles": corrected_smiles,
                "calculated_formula": "C2H6O"}

    def fake_smiles(value, inchi_key=None, mol_formula=None, exact_mass=None):
        key = expected_key if value == corrected_smiles else smiles_key
        if inchi_key is not None and key != inchi_key:
            raise molecule_sync.MoleculeSyncError("SMILES key mismatch")
        return {"inchi_key": key}

    monkeypatch.setattr(cli, "normalize_inchi_structure", fake_inchi)
    monkeypatch.setattr(cli, "normalize_smiles_structure", fake_smiles)
    first = duplicate_package("nfdi4chem-mol100", smiles="package-smiles-1")
    second = duplicate_package("nfdi4chem-mol200", smiles="package-smiles-2")
    session = PairSession(rdk_rows=[
        (42, authoritative_inchi, True, True, True)])
    plan = cli.validate_duplicate_pair(
        session, expected_key, [first, second])
    assert plan["status"] == "validated_with_warning"
    assert plan["package_smiles_inchi_keys"] == {
        "nfdi4chem-mol100": smiles_key,
        "nfdi4chem-mol200": smiles_key,
    }
    assert plan["corrected_canonical_isomeric_smiles"] == corrected_smiles
    assert plan["corrected_smiles_inchi_key"] == expected_key


def test_invalid_smiles_still_blocks_pair():
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="invalid or missing SMILES"):
        validate_pair(second=duplicate_package(
            "nfdi4chem-mol200", smiles="not-a-smiles"))


def test_invalid_inchi_still_blocks_pair():
    bad = duplicate_package("nfdi4chem-mol200")
    for extra in bad["extras"]:
        if extra["key"] == "inchi":
            extra["value"] = "not-an-inchi"
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="invalid or missing InChI"):
        validate_pair(second=bad)


def test_missing_formula_is_planned_for_backfill_and_complete_package_wins():
    missing = duplicate_package(
        "nfdi4chem-mol100", formula=None, created="2019-01-01")
    complete = duplicate_package(
        "nfdi4chem-mol200", formula="C2H6O", created="2021-01-01")
    plan = validate_pair(first=missing, second=complete)
    assert plan["keep_package"] == "nfdi4chem-mol200"
    assert plan["missing_formulas"] == ["nfdi4chem-mol100"]
    assert "mol_formula" in plan["metadata_plan"]["retained"]


def test_conflicting_nonblank_formula_blocks_pair():
    bad = duplicate_package("nfdi4chem-mol200", formula="C2H4")
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="conflicting molecular formula"):
        validate_pair(second=bad)


def test_different_genuine_title_becomes_planned_synonym():
    second = duplicate_package(
        "nfdi4chem-mol200", title="Ethyl alcohol", created="2021-01-01")
    plan = validate_pair(second=second)
    assert plan["differing_titles"] is True
    assert plan["metadata_plan"]["planned_synonym"] == "Ethyl alcohol"


def test_technical_title_is_excluded_as_synonym():
    second = duplicate_package(
        "nfdi4chem-mol200 (Unknown Molecule)",
        title="nfdi4chem-mol200 (Unknown Molecule)", created="2021-01-01")
    plan = validate_pair(second=second)
    assert plan["metadata_plan"]["planned_synonym"] is None


@pytest.mark.parametrize("direction", ["incoming", "outgoing", "legacy"])
def test_reference_in_any_direction_blocks_pair(direction):
    session = PairSession({("id-nfdi4chem-mol200", direction): 1})
    with pytest.raises(molecule_sync.MoleculeSyncError, match="reference blocks"):
        validate_pair(session=session)


def test_inactive_relationship_still_blocks_cleanup():
    session = PairSession({("id-nfdi4chem-mol100", "outgoing"): 1})
    with pytest.raises(molecule_sync.MoleculeSyncError, match="reference blocks"):
        validate_pair(session=session)
    relationship_sql = [sql for sql in session.sql
                        if "relationship_relationship" in sql]
    assert relationship_sql
    assert all("package_relationship" not in sql for sql in relationship_sql)


@pytest.mark.parametrize("rows, reason", [
    ([], "found 0"),
    ([(1, "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", True, True, True),
      (2, "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", True, True, True)], "found 2"),
    ([(1, "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", False, False, False)],
     "missing or null"),
    ([(1, "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", True, True, False)],
     "missing or null"),
])
def test_rdkit_identity_and_fingerprint_checks(rows, reason):
    with pytest.raises(molecule_sync.MoleculeSyncError, match=reason):
        validate_pair(session=PairSession(rdk_rows=rows))


def test_canonical_selection_uses_full_deterministic_priority():
    first = duplicate_package(
        "nfdi4chem-mol999", title="nfdi4chem-mol999", created="2010-01-01")
    second = duplicate_package(
        "nfdi4chem-mol100", title="Ethanol", created="2020-01-01")
    assert validate_pair(first=first, second=second)["keep_package"] == \
        "nfdi4chem-mol100"
    first["title"] = second["title"]
    first["metadata_created"] = second["metadata_created"]
    assert validate_pair(first=first, second=second)["keep_package"] == \
        "nfdi4chem-mol100"


def test_manifest_output_contains_only_required_columns(tmp_path):
    output = tmp_path / "dedup.csv"
    cli._write_dedup_manifest(str(output), [validate_pair()])
    assert output.read_text().splitlines() == [
        "inchi_key,keep_package,remove_package",
        ETHANOL_KEY + ",nfdi4chem-mol100,nfdi4chem-mol200",
    ]


def test_warning_pair_is_in_manifest_and_summary(monkeypatch, tmp_path):
    class WarningAuditSession(object):
        def __init__(self):
            self.rolled_back = 0

        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            assert not sql.upper().startswith(("INSERT", "UPDATE", "DELETE"))
            if statement is cli.DUPLICATE_GROUPS_SQL:
                return BackfillResult([(ETHANOL_KEY, ["one", "two"])])
            if "FROM public.package" in sql:
                return BackfillResult([(10,)])
            if "FROM rdk.molecules" in sql:
                return BackfillResult([(9,)])
            if "FROM rdk.fingerprints" in sql:
                return BackfillResult([(9,)])
            raise AssertionError(sql)

        def rollback(self):
            self.rolled_back += 1

        def commit(self):
            pytest.fail("dry-run audit must never commit")

    packages = {
        "one": duplicate_package("nfdi4chem-mol100"),
        "two": duplicate_package("nfdi4chem-mol200"),
    }
    monkeypatch.setattr(cli, "_load_dedup_package",
                        lambda session, package_id: packages[package_id])
    warning_plan = {
        "status": "validated_with_warning",
        "warning": "smiles_stereochemistry_mismatch",
        "planned_smiles_replacement": True,
        "inchi_key": ETHANOL_KEY,
        "keep_package": "nfdi4chem-mol100",
        "remove_package": "nfdi4chem-mol200",
        "differing_titles": False,
        "equivalent_differing_smiles": True,
        "missing_formulas": [],
        "relationships_requiring_migration": 0,
    }
    monkeypatch.setattr(cli, "validate_duplicate_pair",
                        lambda session, key, items: warning_plan)
    session = WarningAuditSession()
    output = tmp_path / "warning.csv"
    plans, blocked, summary = cli.deduplicate_molecule_packages_dry_run(
        session, str(output))
    assert plans == [warning_plan] and blocked == []
    assert summary["smiles_stereochemistry_mismatches"] == 1
    assert summary["planned_smiles_replacements"] == 1
    assert summary["database_changed"] is False
    assert session.rolled_back == 1
    assert ETHANOL_KEY in output.read_text()


def test_dedup_apply_requires_strong_confirmation(tmp_path):
    manifest = tmp_path / "dedup.csv"
    manifest.write_text("inchi_key,keep_package,remove_package\n")
    result = CliRunner().invoke(
        cli.harvester4chem,
        ["deduplicate-molecule-packages", "--apply",
         "--manifest", str(manifest), "--expected-pairs", "0",
         "--audit-log", str(tmp_path / "audit.jsonl")])
    assert result.exit_code != 0
    assert "SOFT_DELETE_VALIDATED_DUPLICATES" in result.output


def test_dedup_sql_is_read_only_and_code_never_commits():
    source = open(cli.__file__, "r").read()
    start = source.index("def deduplicate_molecule_packages_dry_run")
    dedup_source = source[start:source.index("@click.group", start)]
    sql = " ".join(str(cli.DUPLICATE_GROUPS_SQL).upper().split())
    assert "UPDATE " not in sql and "DELETE " not in sql and "INSERT " not in sql
    assert "SESSION.COMMIT" not in dedup_source.upper()
    assert "package_delete" not in dedup_source
    assert "package_update" not in dedup_source
    assert "package_patch" not in dedup_source


def test_apply_manifest_rejects_duplicates_and_expected_count(tmp_path):
    duplicate = tmp_path / "duplicate.csv"
    duplicate.write_text(
        "inchi_key,keep_package,remove_package\n"
        "AAAAAAAAAAAAAA-UHFFFAOYSA-N,keep-one,remove-one\n"
        "AAAAAAAAAAAAAA-UHFFFAOYSA-N,keep-two,remove-two\n")
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="duplicate manifest InChIKey"):
        cli.parse_dedup_manifest(str(duplicate))
    valid = tmp_path / "valid.csv"
    valid.write_text(
        "inchi_key,keep_package,remove_package\n"
        + ETHANOL_KEY + ",keep-one,remove-one\n")
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="expected-pairs"):
        cli.apply_dedup_manifest(None, str(valid), 2,
                                 str(tmp_path / "audit.jsonl"))


def test_apply_completes_preflight_before_first_mutation(monkeypatch, tmp_path):
    manifest = tmp_path / "dedup.csv"
    manifest.write_text(
        "inchi_key,keep_package,remove_package\n"
        "AAAAAAAAAAAAAA-UHFFFAOYSA-N,keep-one,remove-one\n"
        "BBBBBBBBBBBBBB-UHFFFAOYSA-N,keep-two,remove-two\n")
    actions = []

    class Session(object):
        def rollback(self):
            pass

    def preflight(session, entry):
        if entry["keep_package"] == "keep-two":
            raise molecule_sync.MoleculeSyncError("tampered")
        return {}, {}, {}

    monkeypatch.setattr(cli, "_dedup_preflight_entry", preflight)
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="no mutations"):
        cli.apply_dedup_manifest(
            Session(), str(manifest), 2, str(tmp_path / "audit.jsonl"),
            action_getter=lambda name: actions.append(name))
    assert actions == []
    audit = [json.loads(line) for line in
             (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert {item["status"] for item in audit} == {
        "preflight_validated", "preflight_failed"}
    failed = next(item for item in audit
                  if item["status"] == "preflight_failed")
    assert failed["validation_result"] is not None
    assert failed["validation_result"]["validation_stage"] == "preflight"


def test_apply_pair_patches_deletes_and_preserves_synonym(monkeypatch,
                                                           tmp_path):
    manifest = tmp_path / "dedup.csv"
    manifest.write_text(
        "inchi_key,keep_package,remove_package\n" + ETHANOL_KEY +
        ",keep-one,remove-one\n")
    keep = duplicate_package("keep-one", smiles="C(C)O", formula=None)
    remove = duplicate_package("remove-one", title="Ethyl alcohol")
    plan = {"status": "validated_with_warning",
            "warning": "smiles_stereochemistry_mismatch",
            "planned_smiles_replacement": True,
            "corrected_canonical_isomeric_smiles": "CCO",
            "calculated_formula": "C2H6O",
            "already_applied_candidate": False}
    monkeypatch.setattr(cli, "_dedup_preflight_entry",
                        lambda session, entry: (keep, remove, plan))

    class Result(object):
        def fetchone(self):
            return None

        def scalar(self):
            return 42

    class Session(object):
        def __init__(self):
            self.sql = []
            self.commits = 0

        def execute(self, statement, params=None):
            self.sql.append(str(statement))
            return Result()

        def commit(self):
            self.commits += 1

        def rollback(self):
            pass

    calls = []

    def actions(name):
        def action(context, data):
            calls.append((name, data))
            return {"state": "deleted"} if name == "package_delete" else data
        return action

    session = Session()
    results = cli.apply_dedup_manifest(
        session, str(manifest), 1, str(tmp_path / "audit.jsonl"), actions)
    assert [item[0] for item in calls] == ["package_patch", "package_delete"]
    assert calls[0][1]["smiles"] == "CCO"
    assert calls[0][1]["canonical_smiles"] == "CCO"
    assert calls[0][1]["mol_formula"] == "C2H6O"
    assert calls[1][1] == {"id": remove["id"]}
    assert any("INSERT INTO rdk.molecule_names" in sql for sql in session.sql)
    assert not any("DELETE" in sql.upper() for sql in session.sql)
    assert session.commits == 1
    assert results[0]["synonym_changes"] == ["Ethyl alcohol"]
    assert results[0]["solr_result"]["removed"] == \
        "removed_by_package_delete"


def test_technical_removed_title_is_not_inserted(monkeypatch, tmp_path):
    keep = duplicate_package("keep-one")
    remove = duplicate_package("nfdi4chem-mol123 (Unknown Molecule)",
                               title="nfdi4chem-mol123 (Unknown Molecule)")
    assert cli._dedup_synonym(keep, remove, ETHANOL_KEY) is None


def test_already_applied_entry_performs_no_writes(monkeypatch, tmp_path):
    manifest = tmp_path / "dedup.csv"
    manifest.write_text(
        "inchi_key,keep_package,remove_package\n" + ETHANOL_KEY +
        ",keep-one,remove-one\n")
    keep = duplicate_package("keep-one")
    remove = duplicate_package("remove-one")
    remove["state"] = "deleted"
    plan = {"status": "validated", "calculated_formula": "C2H6O",
            "already_applied_candidate": True}
    monkeypatch.setattr(cli, "_dedup_preflight_entry",
                        lambda session, entry: (keep, remove, plan))

    class Result(object):
        def scalar(self):
            return 42

    class Session(object):
        def execute(self, statement, params=None):
            return Result()

    actions = []
    result = cli.apply_dedup_manifest(
        Session(), str(manifest), 1, str(tmp_path / "audit.jsonl"),
        lambda name: actions.append(name))
    assert actions == []
    assert result[0]["status"] == "already_applied"
    assert result[0]["solr_result"]["removed"] == "already_removed"


def test_partial_failure_is_audited_and_stops(monkeypatch, tmp_path):
    manifest = tmp_path / "dedup.csv"
    manifest.write_text(
        "inchi_key,keep_package,remove_package\n"
        "AAAAAAAAAAAAAA-UHFFFAOYSA-N,keep-one,remove-one\n"
        "BBBBBBBBBBBBBB-UHFFFAOYSA-N,keep-two,remove-two\n")

    def preflight(session, entry):
        keep = duplicate_package(entry["keep_package"])
        remove = duplicate_package(entry["remove_package"])
        return keep, remove, {
            "status": "validated", "calculated_formula": "C2H6O",
            "already_applied_candidate": False}

    monkeypatch.setattr(cli, "_dedup_preflight_entry", preflight)

    class Result(object):
        def scalar(self):
            return 42

    class Session(object):
        def execute(self, statement, params=None):
            return Result()

        def rollback(self):
            pass

    deletions = []

    def actions(name):
        def action(context, data):
            if name == "package_delete":
                deletions.append(data["id"])
                if len(deletions) == 2:
                    raise RuntimeError("second deletion failed")
            return {"ok": True}
        return action

    with pytest.raises(RuntimeError, match="second deletion failed"):
        cli.apply_dedup_manifest(
            Session(), str(manifest), 2, str(tmp_path / "audit.jsonl"), actions)
    audit = [json.loads(line) for line in
             (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [item["status"] for item in audit] == ["applied", "failed"]


def test_empty_dedup_audit_rolls_back_and_only_writes_manifest(tmp_path):
    class ReadOnlyAuditSession(object):
        def __init__(self):
            self.rolled_back = 0

        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            assert not sql.upper().startswith(("INSERT", "UPDATE", "DELETE"))
            if statement is cli.DUPLICATE_GROUPS_SQL:
                return BackfillResult([])
            if "FROM public.package" in sql:
                return BackfillResult([(25495,)])
            if "FROM rdk.molecules" in sql:
                return BackfillResult([(25413,)])
            if "FROM rdk.fingerprints" in sql:
                return BackfillResult([(25413,)])
            raise AssertionError(sql)

        def rollback(self):
            self.rolled_back += 1

        def commit(self):
            pytest.fail("dry-run audit must never commit")

    session = ReadOnlyAuditSession()
    output = tmp_path / "empty.csv"
    plans, blocked, summary = cli.deduplicate_molecule_packages_dry_run(
        session, str(output))
    assert plans == [] and blocked == []
    assert summary["database_changed"] is False
    assert session.rolled_back == 1
    assert output.read_text().strip() == \
        "inchi_key,keep_package,remove_package"


def recovery_manifest(tmp_path, rows=None):
    output = tmp_path / "recovery.csv"
    output.write_text(
        "inchi_key,dataset_package,keep_package,remove_package,relation_type\n" +
        (rows or ETHANOL_KEY +
         ",dataset-one,keep-one,remove-one,related_to\n"))
    return str(output)


def recovery_preflight(monkeypatch, dataset_extra="absent", legacy=None,
                       keep_key=ETHANOL_KEY, remove_key=ETHANOL_KEY,
                       rdk_rows=None):
    def item(name, type_, state, key):
        extras = []
        if key != "absent":
            extras.append({"key": "inchi_key",
                           "value": "" if key == "blank" else key,
                           "state": "active"})
        return {"id": "id-" + name, "name": name, "title": name,
                "type": type_, "state": state, "extras": extras,
                "metadata_created": "2020-01-01",
                "active_dataset_relationships": 0}

    packages = {
        "dataset-one": item("dataset-one", "dataset", "active",
                            dataset_extra),
        "keep-one": item("keep-one", "molecule", "active", keep_key),
        "remove-one": item("remove-one", "molecule", "deleted", remove_key),
    }
    monkeypatch.setattr(cli, "_load_dedup_package",
                        lambda session, name: packages[name])

    class Session(object):
        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            if "FROM public.molecule_rel_data relationship" in sql:
                return BackfillResult(legacy or [])
            if "FROM rdk.molecules m LEFT JOIN rdk.fingerprints" in sql:
                rows = ([(42, True, True, True)] if rdk_rows is None
                        else rdk_rows)
                return BackfillResult(rows)
            if "FROM public.relationship_relationship" in sql:
                return BackfillResult([(0,)])
            raise AssertionError(sql)

    entry = {"inchi_key": ETHANOL_KEY, "dataset_package": "dataset-one",
             "keep_package": "keep-one", "remove_package": "remove-one",
             "relation_type": "related_to"}
    return cli._recovery_preflight_entry(Session(), entry)


@pytest.mark.parametrize("dataset_extra", ["blank", "absent"])
def test_recovery_uses_matching_legacy_when_dataset_extra_missing(
        monkeypatch, dataset_extra):
    result = recovery_preflight(
        monkeypatch, dataset_extra=dataset_extra,
        legacy=[(7, "OTHER-KEY"), (8, ' "' + ETHANOL_KEY + '" ')])
    checks = result[3]
    assert checks["dataset_inchi_key_extra_state"] == dataset_extra
    assert checks["matching_legacy_molecule_ids"] == [8]
    assert checks["dataset_identity_source"] == "legacy_relationship"
    assert checks["rdk_molecule_id"] == 42


def test_recovery_prefers_matching_nonblank_dataset_extra(monkeypatch):
    checks = recovery_preflight(
        monkeypatch, dataset_extra=ETHANOL_KEY,
        legacy=[(8, "DIFFERENTLEGACY-UHFFFAOYSA-N")])[3]
    assert checks["dataset_identity_source"] == "dataset_extra"
    assert checks["matching_legacy_molecule_ids"] == []


def test_recovery_conflicting_nonblank_extra_never_falls_back(monkeypatch):
    with pytest.raises(cli.DedupPreflightError,
                       match="nonblank dataset InChIKey conflicts"):
        recovery_preflight(
            monkeypatch, dataset_extra="AAAAAAAAAAAAAA-UHFFFAOYSA-N",
            legacy=[(8, ETHANOL_KEY)])


@pytest.mark.parametrize("legacy", [[], [(7, "AAAAAAAAAAAAAA-UHFFFAOYSA-N")]])
def test_recovery_requires_matching_legacy_identity(monkeypatch, legacy):
    with pytest.raises(cli.DedupPreflightError,
                       match="no matching legacy"):
        recovery_preflight(monkeypatch, dataset_extra="blank", legacy=legacy)


def test_recovery_retained_key_and_rdkit_remain_strict(monkeypatch):
    with pytest.raises(cli.DedupPreflightError,
                       match="retained molecule InChIKey"):
        recovery_preflight(
            monkeypatch, dataset_extra=ETHANOL_KEY,
            keep_key="AAAAAAAAAAAAAA-UHFFFAOYSA-N")
    with pytest.raises(cli.DedupPreflightError) as raised:
        recovery_preflight(
            monkeypatch, dataset_extra=ETHANOL_KEY, rdk_rows=[])
    checks = raised.value.validation_result
    assert checks["stages"]["rdk_and_fingerprint"] is None
    assert checks["rdk_molecule_id"] is None


def test_dedup_reference_audit_uses_relationship_table_and_ids_or_names():
    session = PairSession({("id-nfdi4chem-mol200", "incoming"): 1})
    with pytest.raises(molecule_sync.MoleculeSyncError, match="reference blocks"):
        validate_pair(session=session)
    sql = " ".join(session.sql)
    assert "public.relationship_relationship" in sql
    assert "object_id IN (:package_id,:package_name)" in sql
    assert "subject_id IN (:package_id,:package_name)" in sql
    assert "public.package_relationship" not in sql


def test_recovery_manifest_rejects_tampering_and_count(tmp_path):
    duplicate = recovery_manifest(
        tmp_path,
        ETHANOL_KEY + ",dataset-one,keep-one,remove-one,related_to\n" +
        ETHANOL_KEY + ",dataset-one,keep-one,remove-two,related_to\n")
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="duplicate logical"):
        cli.parse_relationship_recovery_manifest(duplicate)
    valid = recovery_manifest(tmp_path)
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="expected-relationships"):
        cli.recover_dedup_relationships(
            None, valid, 2, str(tmp_path / "audit.jsonl"))


def test_recovery_calls_create_once_and_verifies_reciprocal_rows(
        monkeypatch, tmp_path):
    manifest = recovery_manifest(tmp_path)
    dataset = {"id": "dataset-id", "name": "dataset-one"}
    keep = {"id": "keep-id", "name": "keep-one"}
    remove = {"id": "remove-id", "name": "remove-one"}
    checks = {"relationship": {"forward_rows": 0, "reverse_rows": 0}}
    monkeypatch.setattr(
        cli, "_recovery_preflight_entry",
        lambda session, entry: (dataset, keep, remove, checks, False))

    class Result(object):
        def scalar(self):
            return 1

    class Session(object):
        def __init__(self):
            self.sql = []

        def execute(self, statement, params=None):
            self.sql.append((str(statement), params))
            return Result()

        def rollback(self):
            pass

    calls = []

    def actions(name):
        def action(context, data):
            calls.append((name, data))
            return [{"relation_type": "related_to"},
                    {"relation_type": "related_to"}]
        return action

    def reindexer(package, getter):
        return {"package_id": package["id"], "package_name": package["name"],
                "package_type": package.get("type"), "status": "reindexed",
                "attempts": 1, "error": None,
                "cached_reindex_reused": False}

    result, summary = cli.recover_dedup_relationships(
        Session(), manifest, 1, str(tmp_path / "audit.jsonl"),
        apply_mode=True, action_getter=actions,
        reindexer=reindexer)
    assert [name for name, data in calls] == [
        "relationship_relation_create"]
    assert calls[0][1] == {"subject_id": "dataset-id",
                           "object_id": "keep-id",
                           "relation_type": "related_to"}
    assert result[0]["reciprocal_row_verification"] == {
        "forward_rows": 1, "reverse_rows": 1}
    assert result[0]["status"] == "created"
    assert summary == {"requested": 1, "created": 1,
                       "already_present": 0, "relationship_failures": 0,
                       "reindex_warnings": 0, "completed": 1}


def test_recovery_already_present_is_idempotent(monkeypatch, tmp_path):
    manifest = recovery_manifest(tmp_path)
    package = {"id": "id", "name": "name"}
    checks = {"relationship": {"forward_rows": 1, "reverse_rows": 1}}
    monkeypatch.setattr(
        cli, "_recovery_preflight_entry",
        lambda session, entry: (package, package, package, checks, True))

    class Session(object):
        def rollback(self):
            pass

    actions = []
    indexed = []

    def reindexer(package, getter):
        indexed.append(package["id"])
        return {"package_id": package["id"], "package_name": package["name"],
                "package_type": package.get("type"), "status": "reindexed",
                "attempts": 1, "error": None,
                "cached_reindex_reused": False}

    result, summary = cli.recover_dedup_relationships(
        Session(), manifest, 1, str(tmp_path / "audit.jsonl"),
        apply_mode=True, action_getter=lambda name: actions.append(name),
        reindexer=reindexer)
    assert actions == []
    assert indexed == ["id", "id"]
    assert result[0]["status"] == "already_present"
    assert summary["already_present"] == 1


def test_recovery_complete_preflight_precedes_mutation(monkeypatch, tmp_path):
    manifest = recovery_manifest(
        tmp_path,
        "AAAAAAAAAAAAAA-UHFFFAOYSA-N,dataset-one,keep-one,remove-one,related_to\n"
        "BBBBBBBBBBBBBB-UHFFFAOYSA-N,dataset-two,keep-two,remove-two,related_to\n")
    actions = []

    def preflight(session, entry):
        if entry["dataset_package"] == "dataset-two":
            raise molecule_sync.MoleculeSyncError("inactive dataset")
        package = {"id": "id", "name": "name"}
        return package, package, package, {}, False

    monkeypatch.setattr(cli, "_recovery_preflight_entry", preflight)

    class Session(object):
        def rollback(self):
            pass

    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="no mutations"):
        cli.recover_dedup_relationships(
            Session(), manifest, 2, str(tmp_path / "audit.jsonl"),
            apply_mode=True, action_getter=lambda name: actions.append(name))
    assert actions == []


def test_recovery_index_normalizes_optional_collections_and_preserves_extras():
    original_extras = [{"key": "one", "value": "two"}]
    package = {"id": "package-id", "name": "package", "type": "dataset"}
    shown = [dict(package), dict(package, extras=None),
             dict(package, extras=original_extras)]
    indexed = []

    def getter(name):
        assert name == "package_show"
        return lambda context, data: shown.pop(0)

    class Index(object):
        def index_package(self, value):
            indexed.append(value)

    for unused in range(3):
        result = cli._reindex_recovery_package(
            package, getter, index_factory=Index, sleep=lambda delay: None)
        assert result["status"] == "reindexed"
    assert indexed[0]["extras"] == []
    assert indexed[1]["extras"] == []
    assert indexed[2]["extras"] is original_extras
    for value in indexed:
        assert value["resources"] == []
        assert value["tags"] == []
        assert value["groups"] == []
        assert value["organization"] == {}


@pytest.mark.parametrize("failing_id", ["dataset-id", "keep-id"])
def test_recovery_individual_index_retry_succeeds(failing_id):
    packages = {
        "dataset-id": {"id": "dataset-id", "name": "dataset",
                       "type": "dataset"},
        "keep-id": {"id": "keep-id", "name": "keep", "type": "molecule"},
    }
    shows, indexes, sleeps = [], [], []

    def getter(name):
        def show(context, data):
            shows.append(data["id"])
            return dict(packages[data["id"]])
        return show

    class Index(object):
        def index_package(self, package):
            indexes.append(package["id"])
            if package["id"] == failing_id and indexes.count(failing_id) == 1:
                raise RuntimeError("temporary Solr error")

    def reindexer(package, action_getter):
        return cli._reindex_recovery_package(
            package, action_getter, index_factory=Index,
            sleep=sleeps.append, retry_delay=0.25)

    result = cli._reindex_recovery_pair(
        packages["dataset-id"], packages["keep-id"], getter,
        reindexer, set())
    assert result["dataset"]["status"] == "reindexed"
    assert result["retained_molecule"]["status"] == "reindexed"
    assert result[("dataset" if failing_id == "dataset-id" else
                   "retained_molecule")]["attempts"] == 2
    assert shows.count(failing_id) == 2
    assert sleeps == [0.25]


def test_recovery_dataset_permanent_failure_still_attempts_molecule():
    dataset = {"id": "dataset-id", "name": "dataset", "type": "dataset"}
    keep = {"id": "keep-id", "name": "keep", "type": "molecule"}
    attempted = []

    def reindexer(package, getter):
        attempted.append(package["id"])
        if package["id"] == "dataset-id":
            return {"package_id": package["id"], "package_name": package["name"],
                    "package_type": package["type"], "status": "failed",
                    "attempts": 3, "error": "Solr unavailable",
                    "cached_reindex_reused": False}
        return {"package_id": package["id"], "package_name": package["name"],
                "package_type": package["type"], "status": "reindexed",
                "attempts": 1, "error": None,
                "cached_reindex_reused": False}

    result = cli._reindex_recovery_pair(
        dataset, keep, None, reindexer, set())
    assert attempted == ["dataset-id", "keep-id"]
    assert result["dataset"]["attempts"] == 3
    assert result["retained_molecule"]["status"] == "reindexed"


def test_recovery_retained_molecule_cache_only_keeps_successes():
    dataset = {"id": "dataset-id", "name": "dataset", "type": "dataset"}
    keep = {"id": "keep-id", "name": "keep", "type": "molecule"}
    calls, cache = [], set()

    def successful(package, getter):
        calls.append(package["id"])
        return {"package_id": package["id"], "package_name": package["name"],
                "package_type": package["type"], "status": "reindexed",
                "attempts": 1, "error": None,
                "cached_reindex_reused": False}

    cli._reindex_recovery_pair(dataset, keep, None, successful, cache)
    second = cli._reindex_recovery_pair(dataset, keep, None, successful, cache)
    assert calls == ["dataset-id", "keep-id", "dataset-id"]
    assert second["retained_molecule"]["cached_reindex_reused"] is True

    failed_cache, failures = set(), []

    def failed(package, getter):
        failures.append(package["id"])
        return {"package_id": package["id"], "package_name": package["name"],
                "package_type": package["type"], "status": "failed",
                "attempts": 3, "error": "still down",
                "cached_reindex_reused": False}

    cli._reindex_recovery_pair(dataset, keep, None, failed, failed_cache)
    cli._reindex_recovery_pair(dataset, keep, None, failed, failed_cache)
    assert failures.count("keep-id") == 2
    assert failed_cache == set()


def test_recovery_solr_warning_is_audited_and_next_entry_continues(
        monkeypatch, tmp_path):
    manifest = recovery_manifest(
        tmp_path,
        "AAAAAAAAAAAAAA-UHFFFAOYSA-N,dataset-one,keep-one,remove-one,related_to\n"
        "BBBBBBBBBBBBBB-UHFFFAOYSA-N,dataset-two,keep-one,remove-two,related_to\n")

    def preflight(session, entry):
        dataset = {"id": entry["dataset_package"],
                   "name": entry["dataset_package"], "type": "dataset"}
        keep = {"id": "keep-one", "name": "keep-one", "type": "molecule"}
        remove = {"id": entry["remove_package"],
                  "name": entry["remove_package"], "type": "molecule"}
        checks = {"dataset_identity_source": "legacy_relationship",
                  "relationship": {"forward_rows": 0, "reverse_rows": 0}}
        return dataset, keep, remove, checks, False

    monkeypatch.setattr(cli, "_recovery_preflight_entry", preflight)

    class Result(object):
        def scalar(self):
            return 1

    class Session(object):
        def execute(self, statement, params=None):
            return Result()

        def rollback(self):
            pass

    created, indexed = [], []

    def actions(name):
        def action(context, data):
            created.append(data["subject_id"])
            return {"ok": True}
        return action

    def reindexer(package, getter):
        indexed.append(package["id"])
        failed = package["id"] == "dataset-one"
        return {"package_id": package["id"], "package_name": package["name"],
                "package_type": package["type"],
                "status": "failed" if failed else "reindexed",
                "attempts": 3 if failed else 1,
                "error": "Solr unavailable" if failed else None,
                "cached_reindex_reused": False}

    audit = tmp_path / "audit.jsonl"
    results, summary = cli.recover_dedup_relationships(
        Session(), manifest, 2, str(audit), apply_mode=True,
        action_getter=actions, reindexer=reindexer)
    assert created == ["dataset-one", "dataset-two"]
    assert [item["status"] for item in results] == [
        "created_with_reindex_warning", "created"]
    assert results[0]["processing_continued_after_warning"] is True
    assert results[0]["errors"] == [{
        "package_id": "dataset-one", "package_name": "dataset-one",
        "package_type": "dataset", "error": "Solr unavailable"}]
    assert results[1]["retained_molecule_reindex_result"][
        "cached_reindex_reused"] is True
    assert summary == {"requested": 2, "created": 2,
                       "already_present": 0, "relationship_failures": 0,
                       "reindex_warnings": 1, "completed": 2}
    records = [json.loads(line) for line in audit.read_text().splitlines()]
    assert records == results
    assert all(list(record).count("identity_source") == 1
               for record in records)


def test_recovery_already_present_solr_failure_is_warning(monkeypatch, tmp_path):
    manifest = recovery_manifest(tmp_path)
    dataset = {"id": "dataset-id", "name": "dataset", "type": "dataset"}
    keep = {"id": "keep-id", "name": "keep", "type": "molecule"}
    checks = {"relationship": {"forward_rows": 1, "reverse_rows": 1}}
    monkeypatch.setattr(
        cli, "_recovery_preflight_entry",
        lambda session, entry: (dataset, keep, keep, checks, True))

    class Session(object):
        def rollback(self):
            pass

    def failed(package, getter):
        return {"package_id": package["id"], "package_name": package["name"],
                "package_type": package["type"], "status": "failed",
                "attempts": 3, "error": "index failed",
                "cached_reindex_reused": False}

    results, summary = cli.recover_dedup_relationships(
        Session(), manifest, 1, str(tmp_path / "audit.jsonl"),
        apply_mode=True, action_getter=lambda name: pytest.fail(
            "must not recreate an existing relationship"), reindexer=failed)
    assert results[0]["status"] == "already_present_with_reindex_warning"
    assert summary["reindex_warnings"] == 1


def test_recovery_missing_reciprocal_row_remains_failure(monkeypatch, tmp_path):
    manifest = recovery_manifest(tmp_path)
    package = {"id": "id", "name": "name", "type": "dataset"}
    checks = {"relationship": {"forward_rows": 0, "reverse_rows": 0}}
    monkeypatch.setattr(
        cli, "_recovery_preflight_entry",
        lambda session, entry: (package, package, package, checks, False))

    class Result(object):
        def __init__(self, value):
            self.value = value

        def scalar(self):
            return self.value

    class Session(object):
        def __init__(self):
            self.counts = iter([1, 0])
            self.rolled_back = 0

        def execute(self, statement, params=None):
            return Result(next(self.counts))

        def rollback(self):
            self.rolled_back += 1

    session = Session()
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="did not create reciprocal"):
        cli.recover_dedup_relationships(
            session, manifest, 1, str(tmp_path / "audit.jsonl"),
            apply_mode=True,
            action_getter=lambda name: lambda context, data: {"ok": True},
            reindexer=lambda package, getter: pytest.fail(
                "relationship failure must not reindex"))
    record = json.loads((tmp_path / "audit.jsonl").read_text())
    assert record["status"] == "failed"
    assert record["forward_row_count"] == 1
    assert record["reverse_row_count"] == 0
    assert session.rolled_back == 1


def cleanup_manifest(tmp_path, rows=None):
    output = tmp_path / "cleanup.csv"
    output.write_text(
        "dataset_id,dataset_name,molecule_id,molecule_name,relation_type\n" +
        (rows or "dataset-id,dataset-name,molecule-id,molecule-name,related_to\n"))
    return str(output)


def cleanup_packages():
    dataset = {"id": "dataset-id", "name": "dataset-name",
               "type": "dataset", "state": "deleted"}
    molecule = {"id": "molecule-id", "name": "molecule-name",
                "type": "molecule", "state": "active"}
    return {value: package for package in (dataset, molecule)
            for value in (package["id"], package["name"])}


class CleanupSession(object):
    def __init__(self, counts):
        self.counts = iter(counts)
        self.sql = []
        self.rolled_back = 0

    def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)

        class Result(object):
            def __init__(self, value):
                self.value = value

            def scalar(self):
                return self.value

        return Result(next(self.counts))

    def rollback(self):
        self.rolled_back += 1


def test_cleanup_manifest_and_dry_run_are_read_only(tmp_path):
    manifest = cleanup_manifest(tmp_path)
    packages = cleanup_packages()
    session = CleanupSession([1, 1])
    actions = []
    results = cli.cleanup_inactive_relationships(
        session, manifest, 1, str(tmp_path / "audit.jsonl"),
        package_loader=lambda unused, value: packages[value],
        action_getter=lambda name: actions.append(name))
    assert actions == []
    assert session.rolled_back == 1
    assert results[0]["status"] == "validated"
    assert results[0]["forward_rows_before"] == 1
    assert results[0]["reverse_rows_before"] == 1


def test_cleanup_complete_preflight_precedes_delete(tmp_path):
    manifest = cleanup_manifest(
        tmp_path,
        "dataset-id,dataset-name,molecule-id,molecule-name,related_to\n"
        "dataset-two,dataset-two-name,molecule-two,molecule-two-name,related_to\n")
    packages = cleanup_packages()
    packages.update({
        "dataset-two": {"id": "dataset-two", "name": "dataset-two-name",
                        "type": "dataset", "state": "active"},
        "dataset-two-name": {"id": "dataset-two", "name": "dataset-two-name",
                             "type": "dataset", "state": "active"},
    })
    actions = []
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="preflight failed"):
        cli.cleanup_inactive_relationships(
            CleanupSession([1, 1]), manifest, 2,
            str(tmp_path / "audit.jsonl"), apply_mode=True,
            package_loader=lambda unused, value: packages[value],
            action_getter=lambda name: actions.append(name))
    assert actions == []


def test_cleanup_apply_deletes_reciprocal_only_and_reindexes_molecule(tmp_path):
    manifest = cleanup_manifest(tmp_path)
    packages = cleanup_packages()
    session = CleanupSession([1, 1, 0, 0])
    actions, indexed = [], []

    def getter(name):
        assert name == "relationship_relation_delete"

        def delete(context, data):
            actions.append((name, data))
            return {"deleted": True}
        return delete

    def reindexer(package, action_getter):
        indexed.append(package)
        return {"package_id": package["id"], "package_name": package["name"],
                "package_type": package["type"], "status": "reindexed",
                "attempts": 1, "error": None,
                "cached_reindex_reused": False}

    results = cli.cleanup_inactive_relationships(
        session, manifest, 1, str(tmp_path / "audit.jsonl"), apply_mode=True,
        action_getter=getter, reindexer=reindexer,
        package_loader=lambda unused, value: packages[value])
    assert actions == [("relationship_relation_delete", {
        "subject_id": "dataset-id", "object_id": "molecule-id",
        "relation_type": "related_to"})]
    assert indexed == [packages["molecule-id"]]
    assert results[0]["status"] == "deleted"
    assert results[0]["forward_rows_after"] == 0
    assert results[0]["reverse_rows_after"] == 0
    assert all("DELETE" not in sql.upper() for sql in session.sql)


def test_cleanup_already_deleted_rerun_is_idempotent(tmp_path):
    manifest = cleanup_manifest(tmp_path)
    packages = cleanup_packages()
    calls = []
    results = cli.cleanup_inactive_relationships(
        CleanupSession([0, 0]), manifest, 1,
        str(tmp_path / "audit.jsonl"), apply_mode=True,
        package_loader=lambda unused, value: packages[value],
        action_getter=lambda name: calls.append(name),
        reindexer=lambda package, getter: calls.append("reindex"))
    assert calls == []
    assert results[0]["status"] == "already_deleted"


def test_cleanup_reciprocal_verification_failure_is_real_failure(tmp_path):
    manifest = cleanup_manifest(tmp_path)
    packages = cleanup_packages()
    session = CleanupSession([1, 1, 0, 1])
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="did not delete both reciprocal"):
        cli.cleanup_inactive_relationships(
            session, manifest, 1, str(tmp_path / "audit.jsonl"),
            apply_mode=True,
            package_loader=lambda unused, value: packages[value],
            action_getter=lambda name: lambda context, data: {
                "deleted": True},
            reindexer=lambda package, getter: pytest.fail(
                "failed relationship verification must not reindex"))
    record = json.loads((tmp_path / "audit.jsonl").read_text())
    assert record["status"] == "failed"
    assert record["forward_rows_after"] == 0
    assert record["reverse_rows_after"] == 1
    assert session.rolled_back == 1


def test_cleanup_reuses_three_attempt_solr_retry(tmp_path):
    manifest = cleanup_manifest(tmp_path)
    packages = cleanup_packages()
    attempts, shown, sleeps = [], [], []

    def action_getter(name):
        if name == "relationship_relation_delete":
            return lambda context, data: {"deleted": True}
        assert name == "package_show"

        def show(context, data):
            shown.append(data["id"])
            return dict(packages[data["id"]])
        return show

    class Index(object):
        def index_package(self, package):
            attempts.append(package["id"])
            if len(attempts) < 3:
                raise RuntimeError("temporary Solr failure")

    def reindexer(package, getter):
        return cli._reindex_recovery_package(
            package, getter, index_factory=Index, sleep=sleeps.append,
            retry_delay=0.2)

    results = cli.cleanup_inactive_relationships(
        CleanupSession([1, 1, 0, 0]), manifest, 1,
        str(tmp_path / "audit.jsonl"), apply_mode=True,
        action_getter=action_getter, reindexer=reindexer,
        package_loader=lambda unused, value: packages[value])
    assert results[0]["status"] == "deleted"
    assert results[0]["molecule_reindex_result"]["attempts"] == 3
    assert shown == ["molecule-id"] * 3
    assert sleeps == [0.2, 0.2]


def test_cleanup_solr_warning_continues_to_next_relationship(tmp_path):
    rows = (
        "dataset-id,dataset-name,molecule-id,molecule-name,related_to\n"
        "dataset-two,dataset-two-name,molecule-two,molecule-two-name,related_to\n")
    manifest = cleanup_manifest(tmp_path, rows)
    packages = cleanup_packages()
    for package in (
            {"id": "dataset-two", "name": "dataset-two-name",
             "type": "dataset", "state": "deleted"},
            {"id": "molecule-two", "name": "molecule-two-name",
             "type": "molecule", "state": "active"}):
        packages[package["id"]] = package
        packages[package["name"]] = package
    deleted = []

    def getter(name):
        def delete(context, data):
            deleted.append(data["subject_id"])
            return {"deleted": True}
        return delete

    def reindexer(package, unused):
        failed = package["id"] == "molecule-id"
        return {"package_id": package["id"], "package_name": package["name"],
                "package_type": package["type"],
                "status": "failed" if failed else "reindexed",
                "attempts": 3 if failed else 1,
                "error": "Solr unavailable" if failed else None,
                "cached_reindex_reused": False}

    results = cli.cleanup_inactive_relationships(
        CleanupSession([1, 1, 1, 1, 0, 0, 0, 0]), manifest, 2,
        str(tmp_path / "audit.jsonl"), apply_mode=True,
        action_getter=getter, reindexer=reindexer,
        package_loader=lambda unused, value: packages[value])
    assert deleted == ["dataset-id", "dataset-two"]
    assert [item["status"] for item in results] == [
        "deleted_with_reindex_warning", "deleted"]
    assert results[0]["error"] == "Solr unavailable"


def test_cleanup_rejects_bad_manifest_and_duplicate_pairs(tmp_path):
    malformed = tmp_path / "bad.csv"
    malformed.write_text("dataset_id,dataset_name\nid,name\n")
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="columns must be exactly"):
        cli.parse_inactive_relationship_manifest(str(malformed))
    duplicate = cleanup_manifest(
        tmp_path,
        "dataset-id,dataset-name,molecule-id,molecule-name,related_to\n"
        "dataset-id,other-name,molecule-id,other-molecule,related_to\n")
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="duplicate logical"):
        cli.parse_inactive_relationship_manifest(duplicate)


def test_cleanup_cli_requires_mode_and_confirmation(tmp_path):
    manifest = cleanup_manifest(tmp_path)
    common = ["--manifest", manifest, "--expected-relationships", "1",
              "--audit-log", str(tmp_path / "audit.jsonl")]
    missing_mode = CliRunner().invoke(
        cli.harvester4chem, ["cleanup-inactive-relationships"] + common)
    assert missing_mode.exit_code != 0
    assert "exactly one" in missing_mode.output
    missing_confirmation = CliRunner().invoke(
        cli.harvester4chem,
        ["cleanup-inactive-relationships", "--apply"] + common)
    assert missing_confirmation.exit_code != 0
    assert "DELETE_STALE_RELATIONSHIPS" in missing_confirmation.output
