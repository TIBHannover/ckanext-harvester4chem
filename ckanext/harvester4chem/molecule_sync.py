import json
import logging
import re
import uuid

from sqlalchemy import text
import ckan.model as model
import ckan.plugins.toolkit as toolkit
from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors
from rdkit.Chem import inchi as rd_inchi

log = logging.getLogger(__name__)
MOLECULE_PACKAGE_TYPE = "molecule"
DATASET_MOLECULE_RELATION = "related_to"
TECHNICAL_MOLECULE_NAME = re.compile(
    r"^(?:nfdi4chem-mol[0-9]+|molecule-[a-z0-9-]+)"
    r"(?: \(unknown molecule\))?$", re.IGNORECASE
)
WRITE_LEGACY_CONFIG = "ckan.harvester4chem.write_legacy"


class MoleculeSyncError(Exception):
    """Chemistry metadata cannot be synchronized without corrupting data."""


def legacy_writes_enabled(value=None):
    """Resolve the explicit override or CKAN's boolean configuration value."""
    if value is not None:
        if isinstance(value, bool):
            return value
        return toolkit.asbool(value)
    return toolkit.asbool(toolkit.config.get(WRITE_LEGACY_CONFIG, False))


def clean_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
    return value if value != "" else None


def normalize_chemical_text(value):
    """Normalize text and unwrap a valid JSON-encoded string safely."""
    value = clean_value(value)
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        decoded = value
    if isinstance(decoded, str):
        decoded = decoded.strip()
        return decoded or None
    return value


def clean_names(values):
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    result, seen = [], set()
    for value in values:
        value = clean_value(value)
        if value is None:
            continue
        name = str(value).strip()
        if name and name.casefold() not in seen:
            result.append(name)
            seen.add(name.casefold())
    return result


def _chemical_display_name(value, inchi_key):
    value = clean_value(value)
    if not value or value.upper() == inchi_key:
        return None
    if TECHNICAL_MOLECULE_NAME.match(value):
        return None
    return value


def normalize_structure(inchi_code=None, inchi_key=None, smiles=None,
                        mol_formula=None, exact_mass=None):
    inchi_code = normalize_chemical_text(inchi_code)
    inchi_key = normalize_chemical_text(inchi_key)
    smiles = normalize_chemical_text(smiles)
    molecule = None
    if inchi_code:
        molecule = rd_inchi.MolFromInchi(inchi_code)
        if molecule is None:
            log.warning("HARVESTER4CHEM could not parse supplied InChI; trying SMILES")
    if molecule is None and smiles:
        molecule = Chem.MolFromSmiles(smiles)
        if molecule is None:
            log.warning("HARVESTER4CHEM could not parse supplied SMILES")
    if molecule is None:
        raise MoleculeSyncError("invalid or missing InChI/SMILES")
    canonical = Chem.MolToSmiles(molecule, canonical=True)
    normalized_inchi = rd_inchi.MolToInchi(molecule)
    normalized_key = rd_inchi.InchiToInchiKey(normalized_inchi).upper()
    if inchi_key and inchi_key.upper() != normalized_key:
        raise MoleculeSyncError(
            "InChIKey mismatch: supplied {0}, calculated {1}".format(
                inchi_key, normalized_key))
    exact_mass = clean_value(exact_mass)
    try:
        exact_mass = (Descriptors.ExactMolWt(molecule) if exact_mass is None
                      else float(exact_mass))
    except (TypeError, ValueError):
        raise MoleculeSyncError("invalid exact mass")
    return {"canonical_smiles": canonical, "inchi_code": normalized_inchi,
            "inchi_key": normalized_key,
            "mol_formula": clean_value(mol_formula) or
            rdMolDescriptors.CalcMolFormula(molecule),
            "exact_mass": exact_mass}


def _one(session, sql, params):
    return session.execute(text(sql), params).fetchone()


def synchronize_legacy_molecule_relation(package_id, values, session=None):
    """Create/reuse a legacy row and link only its public.molecules.id."""
    session = session or model.Session
    rows = session.execute(text("""
        SELECT id, canonical_smiles FROM public.molecules
        WHERE (inchi_key IS NOT NULL AND btrim(inchi_key)<>'' AND
               upper(trim(both '"' from btrim(inchi_key))) = :inchi_key)
           OR (inchi IS NOT NULL AND btrim(inchi)<>'' AND
               trim(both '"' from btrim(inchi)) = :inchi_code)
        ORDER BY id
    """), values).fetchall()
    exact = []
    for row in rows:
        try:
            canonical = normalize_structure(smiles=row[1])["canonical_smiles"]
        except MoleculeSyncError:
            canonical = clean_value(row[1])
        if canonical == values["canonical_smiles"]:
            exact.append(row)
    if rows and not exact:
        raise MoleculeSyncError("legacy identity has a different structure")
    if exact:
        legacy_id, status = exact[0][0], "existing"
    else:
        row = _one(session, """
            INSERT INTO public.molecules
                (inchi, canonical_smiles, inchi_key, exact_mass, mol_formula)
            VALUES (:inchi_code, :canonical_smiles, :inchi_key,
                    :exact_mass, :mol_formula) RETURNING id
        """, values)
        if not row:
            raise MoleculeSyncError("legacy molecule insert returned no row")
        legacy_id, status = row[0], "created"
    params = {"package_id": package_id, "legacy_id": legacy_id}
    linked = _one(session, """
        SELECT id FROM public.molecule_rel_data
        WHERE package_id=:package_id AND molecules_id=:legacy_id LIMIT 1
    """, params)
    if not linked:
        session.execute(text("""
            INSERT INTO public.molecule_rel_data (package_id, molecules_id)
            VALUES (:package_id, :legacy_id)
        """), params)
    return {"status": status, "legacy_molecule_id": legacy_id,
            "relationship": "existing" if linked else "created"}


def _package_value(package, key):
    value = package.get(key)
    if value is not None:
        return clean_value(value)
    for extra in package.get("extras") or []:
        if (extra.get("key") == key and
                extra.get("state", "active") == "active"):
            return normalize_chemical_text(extra.get("value"))
    return None


def _candidate_ids(session, inchi_key):
    return session.execute(text("""
        SELECT DISTINCT p.id FROM public.package p
        JOIN public.package_extra e ON e.package_id=p.id
        WHERE p.type='molecule' AND p.state='active'
          AND e.state='active' AND e.key='inchi_key'
          AND e.value IS NOT NULL AND btrim(e.value)<>''
          AND upper(trim(both '"' from btrim(e.value)))=:inchi_key
        ORDER BY p.id
    """), {"inchi_key": inchi_key}).fetchall()


def _package_values(package):
    return normalize_structure(
        _package_value(package, "inchi"),
        _package_value(package, "inchi_key"),
        _package_value(package, "canonical_smiles") or
        _package_value(package, "smiles"),
        _package_value(package, "mol_formula"),
        _package_value(package, "exactmass") or
        _package_value(package, "exact_mass"))


def _allocate_molecule_package_name():
    """Preserve ``nfdi4chem-mol<number>`` with a vast random namespace.

    CKAN's package-name unique constraint remains the concurrency arbiter.
    A collision fails the transaction safely instead of using MAX()+1.
    """
    return "nfdi4chem-mol{0}".format(uuid.uuid4().int)


def _package_payload(values, names):
    names = clean_names(names)
    return {"id": str(uuid.uuid4()),
            "name": _allocate_molecule_package_name(),
            "title": names[0] if names else values["inchi_key"],
            "type": "molecule", "state": "active",
            "inchi": values["inchi_code"], "inchi_key": values["inchi_key"],
            "smiles": values["canonical_smiles"],
            "canonical_smiles": values["canonical_smiles"],
            "mol_formula": values["mol_formula"],
            "exactmass": values["exact_mass"],
            "alternate_name": json.dumps(names)}


def ensure_molecule_package(values, names=None, session=None,
                            action_getter=None, dry_run=False):
    """Use PostgreSQL, not Solr, to find/create an active molecule package."""
    session, action_getter = session or model.Session, action_getter or toolkit.get_action
    exact, different = [], []
    for row in _candidate_ids(session, values["inchi_key"]):
        package = action_getter("package_show")(
            {"ignore_auth": True}, {"id": row[0]})
        try:
            candidate = _package_values(package)
        except MoleculeSyncError:
            different.append(package["id"])
            continue
        if candidate["canonical_smiles"] == values["canonical_smiles"]:
            exact.append(package)
        else:
            different.append(package["id"])
    if different:
        raise MoleculeSyncError(
            "InChIKey {0} has chemically different molecule packages: {1}"
            .format(values["inchi_key"], ", ".join(different)))
    if exact:
        duplicates = [item["id"] for item in exact[1:]]
        if duplicates:
            log.warning("HARVESTER4CHEM duplicate molecule packages selected=%s duplicates=%s",
                        exact[0]["id"], ",".join(duplicates))
        return exact[0], duplicates, False
    payload = _package_payload(values, names)
    if dry_run:
        return payload, [], True
    package = action_getter("package_create")(
        {"model": model, "session": session, "ignore_auth": True,
         "user": "harvest", "defer_commit": True}, payload)
    return package, [], True


def synchronize_molecule_package_with_rdk(package, session=None,
                                          name_source="CKAN"):
    """Upsert rdk.* from a type=molecule package and return its RDKit ID."""
    if package.get("type") != "molecule" or package.get("state", "active") != "active":
        raise MoleculeSyncError("RDKit source must be an active molecule package")
    session, values = session or model.Session, _package_values(package)
    row = _one(session, """
        INSERT INTO rdk.molecules
          (molecule, canonical_smiles, inchi_key, inchi_code, mol_formula, exact_mass)
        VALUES (mol_from_smiles(CAST(:canonical_smiles AS cstring)),
                :canonical_smiles, :inchi_key, :inchi_code, :mol_formula, :exact_mass)
        ON CONFLICT (inchi_code) DO UPDATE SET
          molecule=EXCLUDED.molecule, canonical_smiles=EXCLUDED.canonical_smiles,
          inchi_key=EXCLUDED.inchi_key,
          mol_formula=COALESCE(EXCLUDED.mol_formula,rdk.molecules.mol_formula),
          exact_mass=COALESCE(EXCLUDED.exact_mass,rdk.molecules.exact_mass)
        RETURNING molecule_id
    """, values)
    if not row:
        raise MoleculeSyncError("RDKit molecule upsert returned no row")
    molecule_id = row[0]
    if not _one(session, """
        INSERT INTO rdk.fingerprints (molecule_id,mfp2,ffp2)
        SELECT molecule_id,morganbv_fp(molecule),featmorganbv_fp(molecule)
        FROM rdk.molecules WHERE molecule_id=:molecule_id
        ON CONFLICT (molecule_id) DO UPDATE SET mfp2=EXCLUDED.mfp2,ffp2=EXCLUDED.ffp2
        RETURNING molecule_id
    """, {"molecule_id": molecule_id}):
        raise MoleculeSyncError("fingerprint upsert returned no row")
    alternate = _package_value(package, "alternate_name")
    try:
        alternate = json.loads(alternate) if isinstance(alternate, str) else alternate
    except (TypeError, ValueError):
        pass
    title = _chemical_display_name(package.get("title"), values["inchi_key"])
    names = clean_names([title] + clean_names(alternate))
    for name in names:
        params = {"molecule_id": molecule_id, "name": name,
                  "source": clean_value(name_source) or "CKAN"}
        if not _one(session, """
            SELECT name_id FROM rdk.molecule_names WHERE molecule_id=:molecule_id
            AND lower(name)=lower(:name) LIMIT 1
        """, params):
            session.execute(text("""
                INSERT INTO rdk.molecule_names (molecule_id,name,type,source)
                VALUES (:molecule_id,:name,'harvested_name',:source)
            """), params)
    return molecule_id


def ensure_dataset_molecule_package_relationship(dataset_id, molecule_id,
                                                 action_getter=None,
                                                 dry_run=False,
                                                 molecule_name=None):
    action_getter = action_getter or toolkit.get_action
    context = {"model": model, "session": model.Session,
               "ignore_auth": True, "user": "harvest"}
    relations = action_getter("relationship_relations_list")(
        context, {"subject_id": dataset_id}) or []
    object_refs = {molecule_id, molecule_name} - {None}
    exists = any(item.get("object_id") in object_refs and
                 item.get("relation_type") == DATASET_MOLECULE_RELATION
                 for item in relations)
    if not exists and not dry_run:
        raise MoleculeSyncError(
            "new CKAN relationship blocked: installed "
            "relationship_relation_create commits independently"
        )
    return "existing" if exists else "created"


def synchronize_molecule(package_id, inchi_code=None, inchi_key=None,
                         smiles=None, mol_formula=None, exact_mass=None,
                         names=None, name_source="CKAN", session=None,
                         dry_run=False, action_getter=None, write_legacy=None):
    package_id, session = clean_value(package_id), session or model.Session
    if not package_id:
        raise MoleculeSyncError("missing dataset package ID")
    values = normalize_structure(inchi_code, inchi_key, smiles,
                                 mol_formula, exact_mass)
    savepoint = session.begin_nested() if dry_run else None
    try:
        if legacy_writes_enabled(write_legacy):
            legacy = synchronize_legacy_molecule_relation(
                package_id, values, session)
        else:
            legacy = {"status": "skipped", "relationship": "skipped",
                      "reason": "legacy writes disabled"}
        package, duplicates, created = ensure_molecule_package(
            values, names, session, action_getter, dry_run)
        rdk_molecule_id = synchronize_molecule_package_with_rdk(
            package, session, name_source)
        relation = ensure_dataset_molecule_package_relationship(
            package_id, package["id"], action_getter, dry_run,
            molecule_name=package.get("name"))
        result = {"legacy": legacy, "molecule_package_id": package["id"],
                  "molecule_package": "created" if created else "existing",
                  "duplicate_molecule_package_ids": duplicates,
                  "rdk_molecule_id": rdk_molecule_id,
                  "ckan_relationship": relation, "dry_run": bool(dry_run)}
    except Exception:
        if savepoint is not None and savepoint.is_active:
            savepoint.rollback()
        raise
    if savepoint is not None and savepoint.is_active:
        savepoint.rollback()
    return result


def synchronize_harvested_package(package_dict, harvest_object, names=None,
                                  name_source="CKAN", session=None,
                                  dry_run=False, write_legacy=None):
    package_id = getattr(harvest_object, "package_id", None) or package_dict.get("id")
    package = model.Package.get(package_id)
    if package is not None:
        package_id = package.id
    try:
        return synchronize_molecule(
            package_id, package_dict.get("inchi"), package_dict.get("inchi_key"),
            package_dict.get("smiles"), package_dict.get("mol_formula"),
            package_dict.get("exactmass") if package_dict.get("exactmass") is not None else package_dict.get("exact_mass"),
            names, name_source, session, dry_run,
            write_legacy=write_legacy)
    except Exception as error:
        log.error("HARVESTER4CHEM molecule_sync package=%s failed: %s", package_id, error)
        raise
