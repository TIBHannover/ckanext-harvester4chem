import logging

from sqlalchemy import text

import ckan.model as model

from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors
from rdkit.Chem import inchi as rd_inchi


log = logging.getLogger(__name__)


class MoleculeSyncError(Exception):
    """Chemistry metadata cannot be synchronized without corrupting data."""


def clean_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
    return value if value != "" else None


def clean_names(values):
    if values is None:
        return []
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    result = []
    seen = set()
    for value in values:
        name = clean_value(value)
        if name is None:
            continue
        name = str(name).strip()
        key = name.casefold()
        if name and key not in seen:
            result.append(name)
            seen.add(key)
    return result


def normalize_structure(inchi_code=None, inchi_key=None, smiles=None,
                        mol_formula=None, exact_mass=None):
    """Parse and normalize chemistry values without touching the database."""
    inchi_code = clean_value(inchi_code)
    supplied_key = clean_value(inchi_key)
    smiles = clean_value(smiles)

    if inchi_code:
        molecule = rd_inchi.MolFromInchi(inchi_code)
        invalid_label = "InChI"
    elif smiles:
        molecule = Chem.MolFromSmiles(smiles)
        invalid_label = "SMILES"
    else:
        raise MoleculeSyncError("missing InChI and SMILES")

    if molecule is None:
        raise MoleculeSyncError("invalid {0}".format(invalid_label))

    canonical_smiles = Chem.MolToSmiles(molecule, canonical=True)
    calculated_inchi = rd_inchi.MolToInchi(molecule)
    calculated_key = rd_inchi.InchiToInchiKey(calculated_inchi)
    if not calculated_inchi or not calculated_key:
        raise MoleculeSyncError("RDKit could not generate InChI identity")
    if supplied_key and supplied_key.upper() != calculated_key.upper():
        raise MoleculeSyncError(
            "InChIKey mismatch: supplied {0}, calculated {1}".format(
                supplied_key, calculated_key
            )
        )

    exact_mass = clean_value(exact_mass)
    if exact_mass is None:
        exact_mass = Descriptors.ExactMolWt(molecule)
    else:
        try:
            exact_mass = float(exact_mass)
        except (TypeError, ValueError):
            raise MoleculeSyncError("invalid exact mass")

    return {
        "canonical_smiles": canonical_smiles,
        "inchi_code": calculated_inchi,
        "inchi_key": calculated_key,
        "mol_formula": clean_value(mol_formula) or
                       rdMolDescriptors.CalcMolFormula(molecule),
        "exact_mass": exact_mass,
    }


def _one(session, sql, parameters):
    return session.execute(text(sql), parameters).fetchone()


def _existing_molecule(session, inchi_code):
    return _one(session, """
        SELECT molecule_id, canonical_smiles, inchi_key, mol_formula,
               exact_mass
        FROM rdk.molecules
        WHERE inchi_code = :inchi_code
    """, {"inchi_code": inchi_code})


def _upsert_molecule(session, values):
    row = _one(session, """
        INSERT INTO rdk.molecules (
            molecule, canonical_smiles, inchi_key, inchi_code,
            mol_formula, exact_mass
        ) VALUES (
            mol_from_smiles(CAST(:canonical_smiles AS cstring)),
            :canonical_smiles, :inchi_key, :inchi_code,
            :mol_formula, :exact_mass
        )
        ON CONFLICT (inchi_code) DO UPDATE SET
            molecule = EXCLUDED.molecule,
            canonical_smiles = EXCLUDED.canonical_smiles,
            inchi_key = EXCLUDED.inchi_key,
            mol_formula = COALESCE(EXCLUDED.mol_formula,
                                   rdk.molecules.mol_formula),
            exact_mass = COALESCE(EXCLUDED.exact_mass,
                                  rdk.molecules.exact_mass)
        RETURNING molecule_id
    """, values)
    if row is None:
        raise MoleculeSyncError("RDKit molecule upsert returned no row")
    return row[0]


def _upsert_fingerprints(session, molecule_id):
    row = _one(session, """
        INSERT INTO rdk.fingerprints (molecule_id, mfp2, ffp2)
        SELECT molecule_id, morganbv_fp(molecule), featmorganbv_fp(molecule)
        FROM rdk.molecules
        WHERE molecule_id = :molecule_id
        ON CONFLICT (molecule_id) DO UPDATE SET
            mfp2 = EXCLUDED.mfp2,
            ffp2 = EXCLUDED.ffp2
        RETURNING molecule_id
    """, {"molecule_id": molecule_id})
    if row is None:
        raise MoleculeSyncError("fingerprint upsert returned no row")


def _upsert_relationship(session, package_id, molecule_id):
    existing = _one(session, """
        SELECT id FROM public.molecule_rel_data
        WHERE package_id = :package_id AND molecules_id = :molecule_id
        LIMIT 1
    """, {"package_id": package_id, "molecule_id": molecule_id})
    if existing:
        return False
    session.execute(text("""
        INSERT INTO public.molecule_rel_data (package_id, molecules_id)
        VALUES (:package_id, :molecule_id)
    """), {"package_id": package_id, "molecule_id": molecule_id})
    return True


def _upsert_names(session, molecule_id, names, name_type, source):
    inserted = 0
    for name in names:
        existing = _one(session, """
            SELECT name_id FROM rdk.molecule_names
            WHERE molecule_id = :molecule_id
              AND lower(name) = lower(:name)
            LIMIT 1
        """, {"molecule_id": molecule_id, "name": name})
        if existing:
            continue
        session.execute(text("""
            INSERT INTO rdk.molecule_names
                (molecule_id, name, type, source)
            VALUES (:molecule_id, :name, :name_type, :source)
        """), {
            "molecule_id": molecule_id,
            "name": name,
            "name_type": name_type,
            "source": source,
        })
        inserted += 1
    return inserted


def synchronize_molecule(package_id, inchi_code=None, inchi_key=None,
                         smiles=None, mol_formula=None, exact_mass=None,
                         names=None, name_source="CKAN",
                         name_type="harvested_name", session=None,
                         dry_run=False):
    """Synchronize one molecule. The caller owns commit and rollback."""
    package_id = clean_value(package_id)
    if not package_id:
        raise MoleculeSyncError("missing dataset package ID")
    source = clean_value(name_source) or "CKAN"
    name_type = clean_value(name_type) or "harvested_name"
    values = normalize_structure(
        inchi_code, inchi_key, smiles, mol_formula, exact_mass
    )
    session = session or model.Session
    savepoint = session.begin_nested() if dry_run else None
    try:
        existing = _existing_molecule(session, values["inchi_code"])
        molecule_id = _upsert_molecule(session, values)
        _upsert_fingerprints(session, molecule_id)
        linked = _upsert_relationship(session, package_id, molecule_id)
        inserted_names = _upsert_names(
            session, molecule_id, clean_names(names), name_type, source
        )
        result = {
            "status": "updated" if existing else "created",
            "molecule_id": molecule_id,
            "relationship": "created" if linked else "existing",
            "names_created": inserted_names,
            "dry_run": bool(dry_run),
        }
    except Exception:
        if savepoint is not None and savepoint.is_active:
            savepoint.rollback()
        raise
    if savepoint is not None and savepoint.is_active:
        savepoint.rollback()
    return result


def synchronize_harvested_package(package_dict, harvest_object, names=None,
                                  name_source="CKAN", session=None,
                                  dry_run=False):
    package_id = getattr(harvest_object, "package_id", None)
    package_id = package_id or package_dict.get("id")
    package = model.Package.get(package_id)
    if package is not None:
        package_id = package.id
    try:
        return synchronize_molecule(
            package_id=package_id,
            inchi_code=package_dict.get("inchi"),
            inchi_key=package_dict.get("inchi_key"),
            smiles=package_dict.get("smiles"),
            mol_formula=package_dict.get("mol_formula"),
            exact_mass=(package_dict.get("exactmass") if
                        package_dict.get("exactmass") is not None else
                        package_dict.get("exact_mass")),
            names=names,
            name_source=name_source,
            session=session,
            dry_run=dry_run,
        )
    except Exception as error:
        log.error(
            "HARVESTER4CHEM molecule_sync package=%s failed: %s",
            package_id, error,
        )
        raise
