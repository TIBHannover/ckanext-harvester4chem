import copy

import pytest

from ckanext.harvester4chem import molecule_sync


ETHANOL_INCHI = "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"
ETHANOL_KEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"


class Result(object):
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Savepoint(object):
    def __init__(self, session):
        self.session = session
        self.snapshot = copy.deepcopy(session.state)
        self.is_active = True

    def rollback(self):
        self.session.state = self.snapshot
        self.is_active = False


class FakeSession(object):
    def __init__(self):
        self.state = {
            "molecules": {},
            "fingerprints": {},
            "relationships": set(),
            "names": [],
        }
        self.sql = []
        self.fail_on = None

    def begin_nested(self):
        return Savepoint(self)

    def execute(self, statement, parameters=None):
        sql = " ".join(str(statement).split())
        parameters = parameters or {}
        self.sql.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("database unavailable")

        if sql.startswith("SELECT molecule_id, canonical_smiles"):
            row = self.state["molecules"].get(parameters["inchi_code"])
            return Result(row)
        if sql.startswith("INSERT INTO rdk.molecules"):
            current = self.state["molecules"].get(parameters["inchi_code"])
            molecule_id = current[0] if current else len(
                self.state["molecules"]
            ) + 101
            self.state["molecules"][parameters["inchi_code"]] = (
                molecule_id, parameters["canonical_smiles"],
                parameters["inchi_key"], parameters["mol_formula"],
                parameters["exact_mass"],
            )
            return Result((molecule_id,))
        if sql.startswith("INSERT INTO rdk.fingerprints"):
            molecule_id = parameters["molecule_id"]
            self.state["fingerprints"][molecule_id] = ("mfp2", "ffp2")
            return Result((molecule_id,))
        if sql.startswith("SELECT id FROM public.molecule_rel_data"):
            relation = (parameters["package_id"], parameters["molecule_id"])
            return Result((1,) if relation in self.state["relationships"]
                          else None)
        if sql.startswith("INSERT INTO public.molecule_rel_data"):
            self.state["relationships"].add(
                (parameters["package_id"], parameters["molecule_id"])
            )
            return Result()
        if sql.startswith("SELECT name_id FROM rdk.molecule_names"):
            matching = [item for item in self.state["names"]
                        if item[0] == parameters["molecule_id"] and
                        item[1].lower() == parameters["name"].lower()]
            return Result((1,) if matching else None)
        if sql.startswith("INSERT INTO rdk.molecule_names"):
            self.state["names"].append((
                parameters["molecule_id"], parameters["name"],
                parameters["name_type"], parameters["source"],
            ))
            return Result()
        raise AssertionError("Unexpected SQL: {0}".format(sql))


def synchronize(session, **overrides):
    values = {
        "package_id": "package-uuid",
        "inchi_code": ETHANOL_INCHI,
        "inchi_key": ETHANOL_KEY,
        "smiles": "CCO",
        "names": ["Ethanol"],
        "name_source": "test harvester",
        "session": session,
    }
    values.update(overrides)
    return molecule_sync.synchronize_molecule(**values)


def test_valid_new_molecule_creates_complete_rdkit_data_and_relationship():
    session = FakeSession()
    result = synchronize(session)

    assert result["status"] == "created"
    assert len(session.state["molecules"]) == 1
    assert session.state["fingerprints"] == {101: ("mfp2", "ffp2")}
    assert session.state["relationships"] == {("package-uuid", 101)}
    assert "morganbv_fp(molecule)" in " ".join(session.sql)
    assert "featmorganbv_fp(molecule)" in " ".join(session.sql)
    assert "molecules_id" in " ".join(session.sql)


def test_existing_molecule_is_reused_and_second_run_is_idempotent():
    session = FakeSession()
    synchronize(session)
    second = synchronize(session, names=["ethanol"])

    assert second["status"] == "updated"
    assert second["relationship"] == "existing"
    assert len(session.state["molecules"]) == 1
    assert len(session.state["fingerprints"]) == 1
    assert len(session.state["relationships"]) == 1
    assert len(session.state["names"]) == 1


def test_pubchem_name_is_preserved_and_case_insensitive_duplicate_skipped():
    session = FakeSession()
    session.state["names"].append((101, "ETHANOL", "synonym", "PubChem"))
    result = synchronize(session, names=["ethanol", "Ethyl alcohol"])

    assert result["names_created"] == 1
    assert session.state["names"][0] == (
        101, "ETHANOL", "synonym", "PubChem"
    )
    assert session.state["names"][1][1:] == (
        "Ethyl alcohol", "harvested_name", "test harvester"
    )


@pytest.mark.parametrize("smiles", ["not a smiles", "   "])
def test_invalid_or_missing_structure_has_no_partial_writes(smiles):
    session = FakeSession()
    with pytest.raises(molecule_sync.MoleculeSyncError):
        synchronize(session, inchi_code=None, inchi_key=None, smiles=smiles)
    assert not any(session.state.values())


def test_inchi_key_mismatch_fails_before_writes():
    session = FakeSession()
    with pytest.raises(molecule_sync.MoleculeSyncError, match="mismatch"):
        synchronize(session, inchi_key="AAAAAAAAAAAAAA-BBBBBBBBBB-C")
    assert not any(session.state.values())


def test_missing_optional_values_are_calculated():
    session = FakeSession()
    synchronize(session, inchi_code=None, inchi_key=None,
                mol_formula=" ", exact_mass=None, names=[None, " "])
    molecule = list(session.state["molecules"].values())[0]
    assert molecule[2] == ETHANOL_KEY
    assert molecule[3] == "C2H6O"
    assert molecule[4] == pytest.approx(46.041864812)
    assert session.state["names"] == []


def test_dry_run_executes_all_writes_then_rolls_back():
    session = FakeSession()
    before = copy.deepcopy(session.state)
    result = synchronize(session, dry_run=True)

    assert result["dry_run"] is True
    assert session.state == before
    assert any(sql.startswith("INSERT INTO rdk.molecules")
               for sql in session.sql)
    assert any(sql.startswith("INSERT INTO public.molecule_rel_data")
               for sql in session.sql)


def test_database_exception_is_propagated():
    session = FakeSession()
    session.fail_on = "INSERT INTO rdk.fingerprints"
    with pytest.raises(RuntimeError, match="database unavailable"):
        synchronize(session, dry_run=True)
    assert not any(session.state.values())


def test_sql_never_references_legacy_molecule_id():
    session = FakeSession()
    synchronize(session)
    relation_sql = [sql for sql in session.sql
                    if "public.molecule_rel_data" in sql]
    assert relation_sql
    assert all("molecules_id" in sql for sql in relation_sql)
    assert all("public.molecules" not in sql for sql in session.sql)
