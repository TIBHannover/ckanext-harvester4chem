from ckanext.harvester4chem.cli import VERIFY_SQL


def test_verification_queries_are_read_only_and_cover_required_checks():
    assert set(VERIFY_SQL) == {
        "legacy_relationships_missing_public_molecule",
        "duplicate_legacy_package_molecule_relationships",
        "dataset_chemistry_missing_molecule_package",
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
    missing_relationship_sql = str(
        VERIFY_SQL["dataset_molecule_package_relationships_missing"]
    ).upper()
    assert "NOT EXISTS" in missing_relationship_sql
    assert "LEFT JOIN RELATIONSHIP_RELATIONSHIP" not in missing_relationship_sql
