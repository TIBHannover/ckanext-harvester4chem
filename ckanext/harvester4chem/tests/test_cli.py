from ckanext.harvester4chem.cli import VERIFY_SQL


def test_verification_queries_are_read_only_and_cover_required_checks():
    assert set(VERIFY_SQL) == {
        "packages_missing_molecules",
        "molecules_missing_fingerprints",
        "null_fingerprints",
        "relationships_missing_rdk_molecules",
        "duplicate_package_molecule_relationships",
        "package_rdkit_inchi_key_mismatches",
    }
    sql = " ".join(str(query) for query in VERIFY_SQL.values()).upper()
    assert "INSERT " not in sql
    assert "UPDATE " not in sql
    assert "DELETE " not in sql
