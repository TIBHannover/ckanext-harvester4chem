import copy
import os

import pytest

from ckanext.harvester4chem import molecule_sync

ETHANOL_INCHI = "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"
ETHANOL_KEY = "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"


class Result(object):
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


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
        self.state = {"legacy": {}, "legacy_rel": set(), "rdk": {},
                      "fingerprints": set(), "names": []}
        self.sql = []
        self.candidate_ids = []

    def begin_nested(self):
        return Savepoint(self)

    def execute(self, statement, params=None):
        sql, params = " ".join(str(statement).split()), params or {}
        self.sql.append((sql, copy.deepcopy(params)))
        if sql.startswith("SELECT id, canonical_smiles FROM public.molecules"):
            return Result([(key, value["canonical_smiles"])
                           for key, value in self.state["legacy"].items()
                           if value["inchi_key"] == params["inchi_key"]])
        if sql.startswith("INSERT INTO public.molecules"):
            legacy_id = len(self.state["legacy"]) + 10
            self.state["legacy"][legacy_id] = copy.deepcopy(params)
            return Result([(legacy_id,)])
        if sql.startswith("SELECT id FROM public.molecule_rel_data"):
            item = (params["package_id"], params["legacy_id"])
            return Result([(1,)] if item in self.state["legacy_rel"] else [])
        if sql.startswith("INSERT INTO public.molecule_rel_data"):
            self.state["legacy_rel"].add((params["package_id"],
                                          params["legacy_id"]))
            return Result()
        if sql.startswith("SELECT DISTINCT p.id FROM public.package"):
            return Result([(item,) for item in self.candidate_ids])
        if sql.startswith("SELECT molecule_id FROM rdk.molecules"):
            row = self.state["rdk"].get(params["inchi_code"])
            return Result([(row[0],)] if row else [])
        if sql.startswith("INSERT INTO rdk.molecules"):
            current = self.state["rdk"].get(params["inchi_code"])
            molecule_id = current[0] if current else len(self.state["rdk"]) + 101
            self.state["rdk"][params["inchi_code"]] = (
                molecule_id, copy.deepcopy(params))
            return Result([(molecule_id,)])
        if sql.startswith("INSERT INTO rdk.fingerprints"):
            self.state["fingerprints"].add(params["molecule_id"])
            return Result([(params["molecule_id"],)])
        if sql.startswith("SELECT name_id FROM rdk.molecule_names"):
            found = any(item[0] == params["molecule_id"] and
                        item[1].lower() == params["name"].lower()
                        for item in self.state["names"])
            return Result([(1,)] if found else [])
        if sql.startswith("INSERT INTO rdk.molecule_names"):
            self.state["names"].append((params["molecule_id"], params["name"]))
            return Result()
        raise AssertionError("Unexpected SQL: {0}".format(sql))


class Actions(object):
    def __init__(self):
        self.packages = {}
        self.relations = []
        self.calls = []
        self.contexts = []

    def get(self, name):
        def action(context, data):
            self.calls.append((name, copy.deepcopy(data)))
            self.contexts.append((name, copy.copy(context)))
            if name == "package_show":
                return self.packages[data["id"]]
            if name == "package_create":
                self.packages[data["id"]] = copy.deepcopy(data)
                return self.packages[data["id"]]
            if name == "relationship_relations_list":
                return [r for r in self.relations
                        if r["subject_id"] == data["subject_id"]]
            if name == "relationship_relation_create":
                self.relations.append(copy.deepcopy(data))
                return [data]
            raise AssertionError(name)
        return action


def run(session, actions, **overrides):
    values = {"package_id": "dataset-id", "inchi_code": ETHANOL_INCHI,
              "inchi_key": ETHANOL_KEY, "smiles": "CCO",
              "names": ["Ethanol"], "session": session,
              "action_getter": actions.get}
    values.update(overrides)
    return molecule_sync.synchronize_molecule(**values)


def existing_package(session, actions, names=None):
    values = molecule_sync.normalize_structure(smiles="CCO")
    package = molecule_sync._package_payload(values, names or ["Ethanol"])
    package["name"] = "nfdi4chem-mol12345"
    actions.packages[package["id"]] = package
    session.candidate_ids = [package["id"]]
    actions.relations.append({"subject_id": "dataset-id",
                              "object_id": package["id"],
                              "relation_type": "related_to"})
    return package


def test_normalize_chemical_text_handles_json_and_whitespace():
    assert molecule_sync.normalize_chemical_text(None) is None
    assert molecule_sync.normalize_chemical_text('  "{0}"  '.format(
        ETHANOL_INCHI)) == ETHANOL_INCHI
    assert molecule_sync.normalize_chemical_text("  CCO  ") == "CCO"


def test_quoted_inchi_is_parsed():
    values = molecule_sync.normalize_structure(
        inchi_code=' "{0}" '.format(ETHANOL_INCHI))
    assert values["inchi_key"] == ETHANOL_KEY


def test_invalid_inchi_falls_back_to_valid_smiles(monkeypatch):
    warnings = []
    monkeypatch.setattr(molecule_sync.log, "warning",
                        lambda message, *args: warnings.append(message))
    values = molecule_sync.normalize_structure("not-inchi", smiles=" CCO ")
    assert values["inchi_key"] == ETHANOL_KEY
    assert any("could not parse supplied InChI" in item for item in warnings)


def test_supplied_inchi_key_mismatch_fails():
    with pytest.raises(molecule_sync.MoleculeSyncError, match="mismatch"):
        molecule_sync.normalize_structure(
            inchi_code=ETHANOL_INCHI,
            inchi_key="AAAAAAAAAAAAAA-BBBBBBBBBB-C")


def test_inchi_key_mismatch_performs_no_write():
    session, actions = FakeSession(), Actions()
    with pytest.raises(molecule_sync.MoleculeSyncError, match="mismatch"):
        run(session, actions, inchi_key="AAAAAAAAAAAAAA-BBBBBBBBBB-C")
    assert not any(session.state.values())
    assert actions.calls == []


def test_workflow_keeps_legacy_and_rdkit_identifiers_separate():
    session, actions = FakeSession(), Actions()
    existing_package(session, actions)
    result = run(session, actions)
    assert result["legacy"]["legacy_molecule_id"] == 10
    assert result["rdk_molecule_id"] == 101
    assert session.state["legacy_rel"] == {("dataset-id", 10)}
    inserts = [(sql, params) for sql, params in session.sql
               if sql.startswith("INSERT INTO public.molecule_rel_data")]
    assert inserts[0][1]["legacy_id"] == 10
    assert 101 not in inserts[0][1].values()


def test_existing_legacy_molecule_is_reused():
    session = FakeSession()
    values = molecule_sync.normalize_structure(smiles="CCO")
    session.state["legacy"][77] = copy.deepcopy(values)
    result = molecule_sync.synchronize_legacy_molecule_relation(
        "dataset-id", values, session)
    assert result["status"] == "existing"
    assert result["legacy_molecule_id"] == 77
    assert session.state["legacy_rel"] == {("dataset-id", 77)}


def test_package_and_ckan_relationship_are_created_in_correct_direction():
    session, actions = FakeSession(), Actions()
    values = molecule_sync.normalize_structure(smiles="CCO")
    package, _, created = molecule_sync.ensure_molecule_package(
        values, names=["Ethanol"], session=session,
        action_getter=actions.get)
    assert created is True
    assert package["type"] == "molecule"
    assert package["name"].startswith("nfdi4chem-mol")
    assert package["name"][len("nfdi4chem-mol"):].isdigit()
    create_call = [call for call in actions.calls if call[0] == "package_create"]
    assert create_call
    create_context = [context for name, context in actions.contexts
                      if name == "package_create"][0]
    assert create_context["defer_commit"] is True


def test_new_relationship_write_is_blocked_before_committing_action():
    actions = Actions()
    with pytest.raises(molecule_sync.MoleculeSyncError,
                       match="commits independently"):
        molecule_sync.ensure_dataset_molecule_package_relationship(
            "dataset-id", "molecule-id", action_getter=actions.get)
    assert not any(name == "relationship_relation_create"
                   for name, _ in actions.calls)


def test_second_execution_is_idempotent():
    session, actions = FakeSession(), Actions()
    package = existing_package(session, actions)
    first = run(session, actions)
    second = run(session, actions, names=["ethanol"])
    assert second["molecule_package"] == "existing"
    assert len(session.state["legacy"]) == 1
    assert len(session.state["legacy_rel"]) == 1
    assert list(actions.packages) == [package["id"]]
    assert len(actions.relations) == 1
    assert len(session.state["rdk"]) == 1
    assert len(session.state["fingerprints"]) == 1
    assert len([name for _, name in session.state["names"]
                if name.lower() == "ethanol"]) == 1


def test_rdk_is_synchronized_from_molecule_package_metadata():
    session = FakeSession()
    package = molecule_sync._package_payload(
        molecule_sync.normalize_structure(smiles="CCO"), ["Ethyl alcohol"])
    package["title"] = "Package title"
    result = molecule_sync.synchronize_molecule_package_with_rdk(package, session)
    stored = session.state["rdk"][ETHANOL_INCHI][1]
    assert result == 101
    assert stored["inchi_key"] == ETHANOL_KEY
    assert (101, "Package title") in session.state["names"]


def test_technical_package_name_is_not_inserted_as_synonym():
    session = FakeSession()
    package = molecule_sync._package_payload(
        molecule_sync.normalize_structure(smiles="CCO"), ["Ethanol"])
    package["name"] = "nfdi4chem-mol12345"
    package["title"] = package["name"]
    molecule_sync.synchronize_molecule_package_with_rdk(package, session)
    assert (101, "nfdi4chem-mol12345") not in session.state["names"]
    assert (101, "Ethanol") in session.state["names"]


def test_inactive_package_extra_is_ignored():
    package = {"extras": [
        {"key": "inchi_key", "value": "inactive", "state": "deleted"},
        {"key": "inchi_key", "value": ETHANOL_KEY, "state": "active"},
    ]}
    assert molecule_sync._package_value(package, "inchi_key") == ETHANOL_KEY


def test_candidate_lookup_requires_active_nonblank_extras():
    session = FakeSession()
    molecule_sync._candidate_ids(session, ETHANOL_KEY)
    sql = session.sql[-1][0]
    assert "e.state='active'" in sql
    assert "e.value IS NOT NULL" in sql
    assert "btrim(e.value)<>''" in sql


def test_exact_duplicate_packages_reuse_one_and_report_others():
    session, actions = FakeSession(), Actions()
    values = molecule_sync.normalize_structure(smiles="CCO")
    first = molecule_sync._package_payload(values, ["one"])
    second = molecule_sync._package_payload(values, ["two"])
    actions.packages = {first["id"]: first, second["id"]: second}
    session.candidate_ids = [first["id"], second["id"]]
    package, duplicates, created = molecule_sync.ensure_molecule_package(
        values, session=session, action_getter=actions.get)
    assert package["id"] == first["id"]
    assert duplicates == [second["id"]]
    assert created is False


def test_chemically_different_duplicate_package_fails_safely():
    session, actions = FakeSession(), Actions()
    values = molecule_sync.normalize_structure(smiles="CCO")
    bad = molecule_sync._package_payload(values, ["bad"])
    bad["smiles"] = bad["canonical_smiles"] = "CC"
    bad["inchi"] = None
    # Keep the colliding key to model corrupt/ambiguous package metadata.
    actions.packages[bad["id"]] = bad
    session.candidate_ids = [bad["id"]]
    with pytest.raises(molecule_sync.MoleculeSyncError):
        molecule_sync.ensure_molecule_package(
            values, session=session, action_getter=actions.get)


def test_dry_run_validates_every_stage_and_performs_no_writes():
    session, actions = FakeSession(), Actions()
    before = copy.deepcopy(session.state)
    result = run(session, actions, dry_run=True)
    assert result["dry_run"] is True
    assert session.state == before
    assert actions.packages == {}
    assert actions.relations == []
    assert any(name == "relationship_relations_list" for name, _ in actions.calls)
    assert any(sql.startswith("INSERT INTO rdk.molecules")
               for sql, _ in session.sql)


def test_invalid_input_fails_before_any_write():
    session, actions = FakeSession(), Actions()
    with pytest.raises(molecule_sync.MoleculeSyncError):
        run(session, actions, inchi_code=None, inchi_key=None,
            smiles="not a molecule")
    assert not any(session.state.values())


@pytest.mark.skipif(
    os.environ.get("HARVESTER4CHEM_TEST_RDK") != "1",
    reason="set HARVESTER4CHEM_TEST_RDK=1 only for an isolated RDKit test DB",
)
def test_postgresql_rdkit_functions_available():
    from sqlalchemy import text
    import ckan.model as model

    row = model.Session.execute(text("""
        SELECT mol_to_smiles(mol_from_smiles(CAST('CCO' AS cstring))),
               morganbv_fp(mol_from_smiles(CAST('CCO' AS cstring))) IS NOT NULL
    """)).fetchone()
    model.Session.rollback()
    assert row[0] == "CCO"
    assert row[1] is True
