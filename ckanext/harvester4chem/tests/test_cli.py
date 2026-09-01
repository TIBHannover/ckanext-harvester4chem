import pytest
from click.testing import CliRunner

from ckanext.harvester4chem import cli
from ckanext.harvester4chem.cli import VERIFY_SQL


LEGACY_DATASET_AUDIT = "legacy_dataset_chemistry_missing_molecule_package"
DATASET_EXTRA_AUDIT = "dataset_extra_inchikey_missing_molecule_package"


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
