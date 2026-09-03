import pytest
from click.testing import CliRunner
import copy

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
        if "object_package_id=:package_id" in sql:
            return BackfillResult([(self.references.get(
                (package_id, "incoming"), 0),)])
        if "subject_package_id=:package_id" in sql:
            return BackfillResult([(self.references.get(
                (package_id, "outgoing"), 0),)])
        if "FROM public.molecule_rel_data" in sql:
            return BackfillResult([(self.references.get(
                (package_id, "legacy"), 0),)])
        if "FROM rdk.molecules m LEFT JOIN rdk.fingerprints" in sql:
            return BackfillResult(self.rdk_rows)
        raise AssertionError(sql)


def validate_pair(session=None, first=None, second=None):
    first = first or duplicate_package("nfdi4chem-mol100")
    second = second or duplicate_package(
        "nfdi4chem-mol200", created="2021-01-01")
    return cli.validate_duplicate_pair(
        session or PairSession(), ETHANOL_KEY, [first, second])


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
    assert plan["smiles_generated_inchi_keys"] == {
        "nfdi4chem-mol100": "JVTAAEKCZFNVCJ-UHFFFAOYSA-N",
        "nfdi4chem-mol200": "JVTAAEKCZFNVCJ-UHFFFAOYSA-N",
    }
    assert plan["smiles_stereochemistry_mismatches"] == [
        {"package": "nfdi4chem-mol100", "smiles": "CC(O)C(=O)O",
         "generated_inchi_key": "JVTAAEKCZFNVCJ-UHFFFAOYSA-N",
         "classification": "smiles_stereochemistry_mismatch"},
        {"package": "nfdi4chem-mol200", "smiles": "O=C(O)C(O)C",
         "generated_inchi_key": "JVTAAEKCZFNVCJ-UHFFFAOYSA-N",
         "classification": "smiles_stereochemistry_mismatch"},
    ]
    assert "@" in plan["canonical_isomeric_smiles_from_inchi"]
    assert plan["retained_package_smiles_update"] == {
        "package": "nfdi4chem-mol100",
        "field": "canonical_smiles",
        "value": plan["canonical_isomeric_smiles_from_inchi"],
        "before_soft_delete": "nfdi4chem-mol200",
    }


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
                        if "package_relationship" in sql]
    assert all("state='active'" not in sql for sql in relationship_sql)


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


def test_dedup_command_exposes_no_apply_option():
    result = CliRunner().invoke(
        cli.harvester4chem,
        ["deduplicate-molecule-packages", "--apply", "--manifest-out", "x"])
    assert result.exit_code != 0
    assert "No such option: --apply" in result.output


def test_dedup_sql_is_read_only_and_code_never_commits():
    source = open(cli.__file__, "r").read()
    dedup_source = source[source.index("DUPLICATE_GROUPS_SQL"):]
    sql = " ".join(str(cli.DUPLICATE_GROUPS_SQL).upper().split())
    assert "UPDATE " not in sql and "DELETE " not in sql and "INSERT " not in sql
    assert "SESSION.COMMIT" not in dedup_source.upper()
    assert "package_delete" not in dedup_source
    assert "package_update" not in dedup_source
    assert "package_patch" not in dedup_source


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
