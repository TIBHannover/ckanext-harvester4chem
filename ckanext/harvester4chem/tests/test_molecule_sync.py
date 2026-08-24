import copy

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

    def get(self, name):
        def action(context, data):
            self.calls.append((name, copy.deepcopy(data)))
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


def test_workflow_keeps_legacy_and_rdkit_identifiers_separate():
    session, actions = FakeSession(), Actions()
    result = run(session, actions)
    assert result["legacy"]["legacy_molecule_id"] == 10
    assert result["rdk_molecule_id"] == 101
    assert session.state["legacy_rel"] == {("dataset-id", 10)}
    inserts = [(sql, params) for sql, params in session.sql
               if sql.startswith("INSERT INTO public.molecule_rel_data")]
    assert inserts[0][1]["legacy_id"] == 10
    assert 101 not in inserts[0][1].values()


def test_package_and_ckan_relationship_are_created_in_correct_direction():
    session, actions = FakeSession(), Actions()
    result = run(session, actions)
    package = actions.packages[result["molecule_package_id"]]
    assert package["type"] == "molecule"
    assert package["name"] == "molecule-" + ETHANOL_KEY.lower()
    assert actions.relations == [{"subject_id": "dataset-id",
                                  "object_id": package["id"],
                                  "relation_type": "related_to"}]


def test_second_execution_is_idempotent():
    session, actions = FakeSession(), Actions()
    first = run(session, actions)
    session.candidate_ids = [first["molecule_package_id"]]
    second = run(session, actions, names=["ethanol"])
    assert second["molecule_package"] == "existing"
    assert len(session.state["legacy"]) == 1
    assert len(session.state["legacy_rel"]) == 1
    assert len(actions.packages) == 1
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
