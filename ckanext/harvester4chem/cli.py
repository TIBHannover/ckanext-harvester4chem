import csv
import json
import time
from datetime import datetime

import click
from sqlalchemy import text

import ckan.model as model
import ckan.plugins.toolkit as toolkit

from ckanext.harvester4chem.molecule_sync import (
    MoleculeSyncError, TECHNICAL_MOLECULE_NAME,
    create_validated_rdk_backfill, normalize_chemical_text,
    normalize_inchi_structure, normalize_smiles_structure,
    normalize_structure, normalized_inchi_key, synchronize_molecule,
    validate_rdk_backfill_package,
)


VERIFY_SQL = {
    "legacy_relationships_missing_public_molecule": text("""
        SELECT count(*) FROM public.molecule_rel_data r
        LEFT JOIN public.molecules m ON m.id = r.molecules_id
        WHERE m.id IS NULL
    """),
    "duplicate_legacy_package_molecule_relationships": text("""
        SELECT count(*) FROM (
          SELECT package_id,molecules_id FROM public.molecule_rel_data
          GROUP BY package_id,molecules_id HAVING count(*) > 1
        ) duplicate
    """),
    # Historical legacy-table integrity check. It cannot cover post-cutover
    # harvests when ckan.harvester4chem.write_legacy is disabled.
    "legacy_dataset_chemistry_missing_molecule_package": text("""
        WITH dataset_molecule_keys AS (
          SELECT DISTINCT
            rel.package_id AS dataset_id,
            upper(btrim(btrim(btrim(legacy.inchi_key), '"')))
              AS normalized_inchi_key
          FROM public.molecule_rel_data rel
          JOIN public.molecules legacy ON legacy.id=rel.molecules_id
          JOIN public.package dataset ON dataset.id=rel.package_id
            AND dataset.type='dataset' AND dataset.state='active'
          WHERE legacy.inchi_key IS NOT NULL
            AND btrim(legacy.inchi_key)<>''
        )
        SELECT count(*) FROM dataset_molecule_keys dm
        WHERE dm.normalized_inchi_key<>''
          AND NOT EXISTS (
            SELECT 1 FROM public.package molecule_package
            JOIN public.package_extra molecule_key
              ON molecule_key.package_id=molecule_package.id
              AND molecule_key.key='inchi_key'
              AND molecule_key.state='active'
            WHERE molecule_package.type='molecule'
              AND molecule_package.state='active'
              AND molecule_key.value IS NOT NULL
              AND btrim(molecule_key.value)<>''
              AND upper(btrim(btrim(btrim(molecule_key.value), '"')))=
                  dm.normalized_inchi_key
          )
    """),
    # Broader metadata audit: dataset InChIKey extras are not authoritative
    # legacy relationships, but inconsistencies remain useful diagnostics.
    "dataset_extra_inchikey_missing_molecule_package": text("""
        WITH dataset_extra_keys AS (
          SELECT DISTINCT
            dataset.id AS dataset_id,
            upper(btrim(btrim(btrim(dataset_key.value), '"')))
              AS normalized_inchi_key
          FROM public.package dataset
          JOIN public.package_extra dataset_key
            ON dataset_key.package_id=dataset.id
            AND dataset_key.key='inchi_key'
            AND dataset_key.state='active'
          WHERE dataset.type='dataset' AND dataset.state='active'
            AND dataset_key.value IS NOT NULL
            AND btrim(dataset_key.value)<>''
        )
        SELECT count(*) FROM dataset_extra_keys de
        WHERE de.normalized_inchi_key<>''
          AND NOT EXISTS (
            SELECT 1 FROM public.package molecule_package
            JOIN public.package_extra molecule_key
              ON molecule_key.package_id=molecule_package.id
              AND molecule_key.key='inchi_key'
              AND molecule_key.state='active'
            WHERE molecule_package.type='molecule'
              AND molecule_package.state='active'
              AND molecule_key.value IS NOT NULL
              AND btrim(molecule_key.value)<>''
              AND upper(btrim(btrim(btrim(molecule_key.value), '"')))=
                  de.normalized_inchi_key
          )
    """),
    "ambiguous_duplicate_molecule_packages": text("""
        SELECT count(*) FROM (
          SELECT upper(trim(both '"' from btrim(e.value))) FROM package p
          JOIN package_extra e ON e.package_id=p.id AND e.key='inchi_key'
            AND e.state='active'
          WHERE p.type='molecule' AND p.state='active'
            AND e.value IS NOT NULL AND btrim(e.value)<>''
          GROUP BY upper(trim(both '"' from btrim(e.value)))
          HAVING count(DISTINCT p.id)>1
        ) duplicate
    """),
    "molecule_packages_missing_rdk_molecule": text("""
        SELECT count(DISTINCT p.id) FROM package p
        JOIN package_extra i ON i.package_id=p.id AND i.key='inchi'
          AND i.state='active' AND i.value IS NOT NULL AND btrim(i.value)<>''
        LEFT JOIN rdk.molecules m
          ON m.inchi_code=trim(both '"' from btrim(i.value))
        WHERE p.type='molecule' AND p.state='active' AND m.molecule_id IS NULL
    """),
    "molecule_packages_with_rdkit_inchi_key_mismatch": text("""
        SELECT count(*) FROM package p
        JOIN package_extra i ON i.package_id=p.id AND i.key='inchi'
          AND i.state='active' AND i.value IS NOT NULL AND btrim(i.value)<>''
        JOIN package_extra k ON k.package_id=p.id AND k.key='inchi_key'
          AND k.state='active' AND k.value IS NOT NULL AND btrim(k.value)<>''
        JOIN rdk.molecules m
          ON m.inchi_code=trim(both '"' from btrim(i.value))
        WHERE p.type='molecule' AND p.state='active'
          AND upper(trim(both '"' from btrim(k.value)))<>
              upper(coalesce(m.inchi_key,''))
    """),
    "rdk_molecules_missing_fingerprints": text("""
        SELECT count(*) FROM rdk.molecules m LEFT JOIN rdk.fingerprints f
          ON f.molecule_id=m.molecule_id WHERE f.molecule_id IS NULL
    """),
    "null_fingerprints": text("""
        SELECT count(*) FROM rdk.fingerprints WHERE mfp2 IS NULL OR ffp2 IS NULL
    """),
    "dataset_molecule_package_relationships_missing": text("""
        SELECT count(DISTINCT d.id) FROM package d
        JOIN package_extra dk ON dk.package_id=d.id AND dk.key='inchi_key'
          AND dk.state='active' AND dk.value IS NOT NULL AND btrim(dk.value)<>''
        WHERE d.state='active' AND d.type<>'molecule'
          AND EXISTS (
            SELECT 1 FROM package_extra mk
            JOIN package mp ON mp.id=mk.package_id
              AND mp.type='molecule' AND mp.state='active'
            WHERE mk.key='inchi_key' AND mk.state='active'
              AND mk.value IS NOT NULL AND btrim(mk.value)<>''
              AND upper(trim(both '"' from btrim(mk.value)))=
                  upper(trim(both '"' from btrim(dk.value)))
          )
          AND NOT EXISTS (
            SELECT 1 FROM relationship_relationship r
            JOIN package mp ON (mp.id=r.object_id OR mp.name=r.object_id)
              AND mp.type='molecule' AND mp.state='active'
            JOIN package_extra mk ON mk.package_id=mp.id
              AND mk.key='inchi_key' AND mk.state='active'
              AND mk.value IS NOT NULL AND btrim(mk.value)<>''
            WHERE (r.subject_id=d.id OR r.subject_id=d.name)
              AND r.relation_type='related_to'
              AND upper(trim(both '"' from btrim(mk.value)))=
                  upper(trim(both '"' from btrim(dk.value)))
          )
    """),
    "ckan_relationships_referencing_inactive_or_missing_packages": text("""
        SELECT count(*) FROM relationship_relationship r
        LEFT JOIN package s ON s.id=r.subject_id OR s.name=r.subject_id
        LEFT JOIN package o ON o.id=r.object_id OR o.name=r.object_id
        WHERE s.id IS NULL OR o.id IS NULL OR s.state<>'active' OR o.state<>'active'
    """),
}


CHEMICAL_FIELDS = frozenset((
    "inchi", "inchi_key", "smiles", "canonical_smiles",
    "mol_formula", "molecular_formula",
))


def extract_package_chemistry(package):
    """Extract normalized chemistry from CKAN schema fields or extras."""
    top_level = {
        str(key).casefold(): value for key, value in package.items()
        if str(key).casefold() in CHEMICAL_FIELDS
    }
    extra_values, extra_keys = {}, []
    for extra in package.get("extras") or []:
        key = str(extra.get("key") or "").strip().casefold()
        if key not in CHEMICAL_FIELDS:
            continue
        if extra.get("state", "active") != "active":
            continue
        extra_keys.append(key)
        if key not in extra_values:
            extra_values[key] = extra.get("value")
    values, states = {}, {}
    for key in CHEMICAL_FIELDS:
        candidates = []
        if key in top_level:
            candidates.append(top_level[key])
        if key in extra_values:
            candidates.append(extra_values[key])
        normalized = [normalize_chemical_text(value) for value in candidates]
        values[key] = next((value for value in normalized if value is not None),
                           None)
        states[key] = ("absent" if not candidates else
                       "blank" if values[key] is None else "present")
    return {"values": values, "states": states,
            "available_chemistry_extra_keys": sorted(set(extra_keys))}


def _package_value(package, key):
    normalized_key = str(key).casefold()
    if normalized_key in CHEMICAL_FIELDS:
        return extract_package_chemistry(package)["values"][normalized_key]
    value = package.get(key)
    if value is not None:
        return value
    for extra in package.get("extras") or []:
        if str(extra.get("key") or "").casefold() == normalized_key:
            return extra.get("value")
    return None


def parse_repair_manifest(filename):
    """Read an explicit package-name manifest and reject duplicates."""
    names, seen = [], set()
    with open(filename, "r") as manifest:
        for line_number, line in enumerate(manifest, 1):
            name = line.strip()
            if not name or name.startswith("#"):
                continue
            if name in seen:
                raise click.ClickException(
                    "duplicate manifest entry {0} on line {1}".format(
                        name, line_number))
            seen.add(name)
            names.append(name)
    return names


def _load_manifest_package(session, package_name):
    row = session.execute(text("""
        SELECT id, name, title, type, state FROM public.package
        WHERE name=:name LIMIT 1
    """), {"name": package_name}).fetchone()
    if not row:
        raise MoleculeSyncError("package does not exist")
    package = {"id": row[0], "name": row[1], "title": row[2],
               "type": row[3], "state": row[4], "extras": []}
    extras = session.execute(text("""
        SELECT key, value, state FROM public.package_extra
        WHERE package_id=:package_id AND state='active' ORDER BY id
    """), {"package_id": row[0]}).fetchall()
    package["extras"] = [
        {"key": item[0], "value": normalize_chemical_text(item[1]),
         "state": item[2]} for item in extras
    ]
    return package


def repair_missing_rdk(names, mode, session=None):
    """Validate a complete explicit batch, then atomically insert rdk.* only."""
    session = session or model.Session
    results, validated = [], []
    try:
        # Prevent another conforming writer from racing between preflight and insert.
        session.execute(text(
            "LOCK TABLE rdk.molecules IN SHARE ROW EXCLUSIVE MODE"))
        for name in names:
            try:
                package = _load_manifest_package(session, name)
                validated.append(validate_rdk_backfill_package(package, session))
                results.append({"package": name, "status": "validated"})
            except Exception as error:
                results.append({"package": name, "status": "failed",
                                "reason": str(error)})
        if len(validated) != len(names):
            raise MoleculeSyncError("manifest preflight failed")
        for package, result in zip(validated, results):
            try:
                result["rdk_molecule_id"] = create_validated_rdk_backfill(
                    package, session)
                result["status"] = "created"
            except Exception as error:
                result["status"] = "failed"
                result["reason"] = str(error)
                raise
        summary = {"mode": mode, "requested": len(names),
                   "validated": len(validated), "created": len(validated),
                   "failed": 0, "rolled_back": mode == "dry-run"}
        if mode == "dry-run":
            session.rollback()
        else:
            session.commit()
        return results, summary
    except Exception as error:
        session.rollback()
        if len(validated) != len(names):
            summary = {"mode": mode, "requested": len(names),
                       "validated": len(validated), "created": 0,
                       "failed": len(names) - len(validated),
                       "rolled_back": True}
            return results, summary
        if validated:
            for result in results:
                if result.get("status") == "created":
                    result["status"] = "rolled_back"
            summary = {"mode": mode, "requested": len(names),
                       "validated": len(validated), "created": 0,
                       "failed": 1, "rolled_back": True}
            return results, summary
        raise error


DUPLICATE_GROUPS_SQL = text("""
    SELECT upper(trim(both '"' from btrim(e.value))) AS inchi_key,
           array_agg(DISTINCT p.id ORDER BY p.id) AS package_ids
    FROM public.package p
    JOIN public.package_extra e ON e.package_id=p.id
      AND e.key='inchi_key' AND e.state='active'
      AND e.value IS NOT NULL AND btrim(e.value)<>''
    WHERE p.type='molecule' AND p.state='active'
    GROUP BY upper(trim(both '"' from btrim(e.value)))
    HAVING count(DISTINCT p.id)=2
    ORDER BY inchi_key
""")


def _load_dedup_package(session, package_id):
    row = session.execute(text("""
        SELECT id,name,title,type,state,metadata_created
        FROM public.package WHERE id=:package_id OR name=:package_id
    """), {"package_id": package_id}).fetchone()
    if not row:
        raise MoleculeSyncError("duplicate package disappeared during audit")
    package = {"id": row[0], "name": row[1], "title": row[2],
               "type": row[3], "state": row[4],
               "metadata_created": row[5], "extras": []}
    package["extras"] = [
        {"key": item[0], "value": normalize_chemical_text(item[1]),
         "state": item[2]}
        for item in session.execute(text("""
            SELECT key,value,state FROM public.package_extra
            WHERE package_id=:package_id AND state='active' ORDER BY id
        """), {"package_id": row[0]}).fetchall()
    ]
    package["active_dataset_relationships"] = session.execute(text("""
        SELECT count(*) FROM public.relationship_relationship
        WHERE subject_id IN (:package_id,:package_name)
           OR object_id IN (:package_id,:package_name)
    """), {"package_id": row[0], "package_name": row[1]}).scalar()
    return package


def _package_structure_error(package, field, state, stage, error=None):
    chemistry = extract_package_chemistry(package)
    message = ("package {0}: {1} is {2}; validation_stage={3}; "
               "available_chemistry_extra_keys={4}".format(
                   package.get("name") or package.get("id"), field, state,
                   stage,
                   json.dumps(chemistry["available_chemistry_extra_keys"])))
    if error is not None:
        message += "; parser_error={0}".format(error)
    return MoleculeSyncError(message)


def _is_meaningful_title(package, inchi_key):
    title = normalize_chemical_text(package.get("title"))
    return bool(title and title.upper() != inchi_key and
                not TECHNICAL_MOLECULE_NAME.match(title))


def _is_valid(package, key, inchi_key):
    try:
        if key == "inchi":
            normalize_inchi_structure(_package_value(package, key), inchi_key)
        else:
            normalize_smiles_structure(
                _package_value(package, "canonical_smiles") or
                _package_value(package, "smiles"), inchi_key)
        return True
    except MoleculeSyncError:
        return False


def _canonical_rank(package, inchi_key):
    completeness = sum(
        1 for extra in package.get("extras", [])
        if normalize_chemical_text(extra.get("value")) is not None)
    formula = (_package_value(package, "mol_formula") or
               _package_value(package, "molecular_formula"))
    created = package.get("metadata_created")
    return (-int(package.get("active_dataset_relationships") or 0),
            -int(_is_meaningful_title(package, inchi_key)),
            -int(_is_valid(package, "inchi", inchi_key)),
            -int(_is_valid(package, "smiles", inchi_key)),
            -int(bool(normalize_chemical_text(formula))), -completeness,
            created is None, created or "", package["name"])


def _package_references(session, package):
    params = {"package_id": package["id"],
              "package_name": package["name"]}
    incoming = session.execute(text("""
        SELECT count(*) FROM public.relationship_relationship
        WHERE object_id IN (:package_id,:package_name)
    """), params).scalar()
    outgoing = session.execute(text("""
        SELECT count(*) FROM public.relationship_relationship
        WHERE subject_id IN (:package_id,:package_name)
    """), params).scalar()
    legacy = session.execute(text("""
        SELECT count(*) FROM public.molecule_rel_data
        WHERE package_id=:package_id
    """), params).scalar()
    return {"incoming": int(incoming or 0), "outgoing": int(outgoing or 0),
            "legacy": int(legacy or 0)}


def _metadata_plan(keep, remove, expected_key):
    protected = {"inchi", "inchi_key", "smiles", "canonical_smiles"}
    keep_values = {item["key"]: normalize_chemical_text(item.get("value"))
                   for item in keep.get("extras", [])}
    remove_values = {item["key"]: normalize_chemical_text(item.get("value"))
                     for item in remove.get("extras", [])}
    plan = {"retained": [], "copied": [], "identical": [], "conflicts": []}
    for key in sorted(set(keep_values) | set(remove_values)):
        left, right = keep_values.get(key), remove_values.get(key)
        if left and right and left == right:
            plan["identical"].append(key)
        elif left and not right:
            plan["retained"].append(key)
        elif right and not left:
            plan["copied"].append(key)
        elif left != right:
            plan["conflicts"].append(
                {"field": key, "keep": left, "remove": right,
                 "protected_structure": key in protected})
    remove_title = normalize_chemical_text(remove.get("title"))
    keep_title = normalize_chemical_text(keep.get("title"))
    synonym = (remove_title if remove_title and
               remove_title.casefold() != (keep_title or "").casefold() and
               remove_title.upper() != expected_key and
               not TECHNICAL_MOLECULE_NAME.match(remove_title) else None)
    plan["planned_synonym"] = synonym
    return plan


def validate_duplicate_pair(session, inchi_key, packages):
    """Return a complete read-only plan or a blocked-pair reason."""
    expected = normalized_inchi_key(inchi_key)
    if len(packages) != 2:
        raise MoleculeSyncError("duplicate group does not contain exactly two packages")
    if any(item["name"] == "nfdi4chem-mol9372" for item in packages):
        raise MoleculeSyncError("nfdi4chem-mol9372 is explicitly excluded")
    ordered = sorted(packages, key=lambda item: _canonical_rank(item, expected))
    keep, remove = ordered[0], ordered[1]
    references = {item["name"]: _package_references(session, item)
                  for item in packages}
    if any(sum(item.values()) for item in references.values()):
        raise MoleculeSyncError("package reference blocks cleanup: {0}".format(
            json.dumps(references, sort_keys=True)))
    chemistry = [extract_package_chemistry(package) for package in packages]
    inchi_values = []
    for package, extracted in zip(packages, chemistry):
        raw_inchi = extracted["values"]["inchi"]
        if raw_inchi is None:
            raise _package_structure_error(
                package, "InChI", extracted["states"]["inchi"],
                "package_inchi_extraction")
        try:
            inchi_values.append(normalize_inchi_structure(raw_inchi, expected))
        except MoleculeSyncError as error:
            raise _package_structure_error(
                package, "InChI", "unparsable", "package_inchi_parsing",
                error)
    normalized_inchis = [item["inchi_code"] for item in inchi_values]
    if normalized_inchis[0] != normalized_inchis[1]:
        raise MoleculeSyncError(
            "package normalized InChIs differ: {0}".format(json.dumps({
                package["name"]: values["inchi_code"]
                for package, values in zip(packages, inchi_values)
            }, sort_keys=True)))
    calculated_formula = inchi_values[0]["calculated_formula"]
    formulas, missing = [], []
    for package, values in zip(packages, inchi_values):
        formula = normalize_chemical_text(
            _package_value(package, "mol_formula") or
            _package_value(package, "molecular_formula"))
        if formula is None:
            missing.append(package["name"])
        elif formula != values["calculated_formula"]:
            raise MoleculeSyncError(
                "conflicting molecular formula for {0}: supplied {1}, calculated {2}"
                .format(package["name"], formula, values["calculated_formula"]))
        formulas.append(formula)
    rdk_rows = session.execute(text("""
        SELECT m.molecule_id, m.inchi_code, f.molecule_id IS NOT NULL,
               f.mfp2 IS NOT NULL, f.ffp2 IS NOT NULL
        FROM rdk.molecules m LEFT JOIN rdk.fingerprints f
          ON f.molecule_id=m.molecule_id
        WHERE upper(btrim(m.inchi_key))=:inchi_key
    """), {"inchi_key": expected}).fetchall()
    if len(rdk_rows) != 1:
        raise MoleculeSyncError(
            "expected exactly one matching rdk.molecules row; found {0}"
            .format(len(rdk_rows)))
    if not all(rdk_rows[0][2:]):
        raise MoleculeSyncError("matching RDKit fingerprint is missing or null")
    rdk_inchi = normalize_inchi_structure(rdk_rows[0][1], expected)["inchi_code"]
    if rdk_inchi != normalized_inchis[0]:
        raise MoleculeSyncError(
            "RDKit normalized InChI differs from package InChI: RDKit {0}, "
            "package {1}".format(rdk_inchi, normalized_inchis[0]))
    raw_smiles = [item["values"]["canonical_smiles"] or
                  item["values"]["smiles"] for item in chemistry]
    smiles_values = []
    for package, extracted, raw_smiles_value in zip(
            packages, chemistry, raw_smiles):
        if raw_smiles_value is None:
            state = (extracted["states"]["canonical_smiles"]
                     if extracted["states"]["canonical_smiles"] != "absent"
                     else extracted["states"]["smiles"])
            raise _package_structure_error(
                package, "SMILES", state, "package_smiles_extraction")
        try:
            smiles_values.append(normalize_smiles_structure(raw_smiles_value))
        except MoleculeSyncError as error:
            raise _package_structure_error(
                package, "SMILES", "unparsable", "package_smiles_parsing",
                error)
    generated_smiles_keys = {
        package["name"]: values["inchi_key"]
        for package, values in zip(packages, smiles_values)
    }
    expected_connectivity = expected[:14]
    if any(key[:14] != expected_connectivity
           for key in generated_smiles_keys.values()):
        raise MoleculeSyncError(
            "SMILES connectivity block mismatch: expected {0}; generated {1}"
            .format(expected_connectivity,
                    json.dumps(generated_smiles_keys, sort_keys=True)))
    stereochemistry_mismatches = [
        {"package": package["name"], "smiles": smiles,
         "generated_inchi_key": values["inchi_key"],
         "classification": "smiles_stereochemistry_mismatch"}
        for package, smiles, values in zip(packages, raw_smiles, smiles_values)
        if values["inchi_key"] != expected
    ]
    titles = [normalize_chemical_text(item.get("title")) for item in packages]
    plan = {"status": "validated", "inchi_key": expected,
            "keep_package": keep["name"],
            "remove_package": remove["name"], "references": references,
            "metadata_plan": _metadata_plan(keep, remove, expected),
            "differing_titles": titles[0] != titles[1],
            "equivalent_differing_smiles": raw_smiles[0] != raw_smiles[1],
            "package_smiles_inchi_keys": generated_smiles_keys,
            "missing_formulas": missing, "calculated_formula": calculated_formula,
            "relationships_requiring_migration": 0}
    if stereochemistry_mismatches:
        corrected_smiles = inchi_values[0]["canonical_smiles"]
        corrected_key = normalize_smiles_structure(
            corrected_smiles)["inchi_key"]
        if corrected_key != expected:
            raise MoleculeSyncError(
                "canonical isomeric SMILES generated from authoritative InChI "
                "does not regenerate expected InChIKey: expected {0}, generated {1}"
                .format(expected, corrected_key))
        plan.update({
            "status": "validated_with_warning",
            "warning": "smiles_stereochemistry_mismatch",
            "expected_inchi_key": expected,
            "smiles_stereochemistry_mismatch_details":
                stereochemistry_mismatches,
            "corrected_canonical_isomeric_smiles": corrected_smiles,
            "corrected_smiles_inchi_key": corrected_key,
            "planned_smiles_replacement": True,
        })
    return plan


def _write_dedup_manifest(filename, plans):
    with open(filename, "w", newline="") as manifest:
        writer = csv.DictWriter(
            manifest,
            fieldnames=["inchi_key", "keep_package", "remove_package"])
        writer.writeheader()
        for plan in plans:
            writer.writerow({key: plan[key] for key in writer.fieldnames})


def parse_dedup_manifest(filename):
    """Read and strictly validate an explicit deduplication manifest."""
    required = ["inchi_key", "keep_package", "remove_package"]
    entries, keys, packages = [], set(), set()
    with open(filename, "r", newline="") as manifest:
        reader = csv.DictReader(manifest)
        if reader.fieldnames != required:
            raise MoleculeSyncError(
                "manifest columns must be exactly: {0}".format(
                    ",".join(required)))
        for number, row in enumerate(reader, 2):
            if None in row or any(normalize_chemical_text(row.get(key)) is None
                                  for key in required):
                raise MoleculeSyncError(
                    "malformed manifest row {0}".format(number))
            entry = {
                "inchi_key": normalized_inchi_key(row["inchi_key"]),
                "keep_package": row["keep_package"].strip(),
                "remove_package": row["remove_package"].strip(),
            }
            key_parts = entry["inchi_key"].split("-")
            if (len(key_parts) != 3 or len(key_parts[0]) != 14 or
                    len(key_parts[1]) != 10 or len(key_parts[2]) != 1 or
                    not all(part.isalpha() for part in key_parts)):
                raise MoleculeSyncError(
                    "malformed manifest InChIKey on row {0}".format(number))
            if entry["keep_package"] == entry["remove_package"]:
                raise MoleculeSyncError(
                    "manifest row {0} repeats one package".format(number))
            if entry["inchi_key"] in keys:
                raise MoleculeSyncError("duplicate manifest InChIKey: {0}".format(
                    entry["inchi_key"]))
            repeated = packages.intersection(
                [entry["keep_package"], entry["remove_package"]])
            if repeated:
                raise MoleculeSyncError("duplicate manifest package: {0}".format(
                    sorted(repeated)[0]))
            keys.add(entry["inchi_key"])
            packages.update([entry["keep_package"], entry["remove_package"]])
            entries.append(entry)
    return entries


def _dedup_audit_record(entry, status, validation_result=None, error=None,
                        metadata_changes=None, synonym_changes=None,
                        deletion_result=None, solr_result=None):
    return {
        "inchi_key": entry["inchi_key"],
        "retained_package": entry["keep_package"],
        "removed_package": entry["remove_package"],
        "validation_result": validation_result,
        "metadata_changes": metadata_changes or [],
        "synonym_changes": synonym_changes or [],
        "ckan_deletion_result": deletion_result,
        "solr_result": solr_result,
        "status": status,
        "error": error,
    }


def _append_dedup_audit(filename, record):
    with open(filename, "a") as audit:
        audit.write(json.dumps(record, sort_keys=True) + "\n")
        audit.flush()


def _dedup_synonym_exists(session, molecule_id, name):
    return bool(session.execute(text("""
        SELECT name_id FROM rdk.molecule_names
        WHERE molecule_id=:molecule_id AND lower(name)=lower(:name) LIMIT 1
    """), {"molecule_id": molecule_id, "name": name}).fetchone())


class DedupPreflightError(MoleculeSyncError):
    def __init__(self, message, validation_result):
        super(DedupPreflightError, self).__init__(message)
        self.validation_result = validation_result


def _dedup_preflight_entry(session, entry):
    progress = {"validation_stage": "load_packages", "checks": {
        "manifest_parsed": True, "expected_inchi_key": entry["inchi_key"]}}
    try:
        keep = _load_dedup_package(session, entry["keep_package"])
        remove = _load_dedup_package(session, entry["remove_package"])
        progress["checks"]["packages"] = {}
        for package in (keep, remove):
            extracted = extract_package_chemistry(package)
            progress["checks"]["packages"][package["name"]] = {
                "exists": True, "type": package["type"],
                "state": package["state"],
                "chemistry_field_states": extracted["states"],
                "available_chemistry_extra_keys":
                    extracted["available_chemistry_extra_keys"],
            }
        progress["validation_stage"] = "package_type_and_state"
        if keep["type"] != "molecule" or remove["type"] != "molecule":
            raise MoleculeSyncError("manifest package type is not molecule")
        active = keep["state"] == "active" and remove["state"] == "active"
        already = keep["state"] == "active" and remove["state"] == "deleted"
        if not active and not already:
            raise MoleculeSyncError(
                "manifest packages must be active or an already-applied pair")
        progress["checks"]["package_states_valid"] = True
        progress["validation_stage"] = "chemical_and_reference_validation"
        plan = validate_duplicate_pair(
            session, entry["inchi_key"], [keep, remove])
        progress["checks"]["chemical_and_reference_validation"] = plan
        progress["validation_stage"] = "manifest_selection"
        if active and (plan["keep_package"] != entry["keep_package"] or
                       plan["remove_package"] != entry["remove_package"]):
            raise MoleculeSyncError(
                "manifest retained/removed selection was tampered")
    except Exception as error:
        if isinstance(error, DedupPreflightError):
            raise
        progress["error"] = str(error)
        raise DedupPreflightError(str(error), progress)
    plan["already_applied_candidate"] = already
    return keep, remove, plan


def _dedup_metadata_changes(keep, plan):
    changes = {}
    if plan.get("planned_smiles_replacement"):
        corrected = plan["corrected_canonical_isomeric_smiles"]
        for field in ("smiles", "canonical_smiles"):
            if normalize_chemical_text(_package_value(keep, field)) != corrected:
                changes[field] = corrected
    formula = normalize_chemical_text(
        _package_value(keep, "mol_formula") or
        _package_value(keep, "molecular_formula"))
    if formula is None:
        changes["mol_formula"] = plan["calculated_formula"]
    return changes


def _dedup_synonym(keep, remove, expected_key):
    keep_title = normalize_chemical_text(keep.get("title"))
    title = normalize_chemical_text(remove.get("title"))
    if (not title or title.casefold() == (keep_title or "").casefold() or
            title.upper() == expected_key or
            TECHNICAL_MOLECULE_NAME.match(title)):
        return None
    return title


def apply_dedup_manifest(session, manifest, expected_pairs, audit_log,
                         action_getter=None):
    """Preflight every explicit pair, then apply resumable CKAN mutations."""
    entries = parse_dedup_manifest(manifest)
    if expected_pairs != len(entries):
        raise MoleculeSyncError(
            "expected-pairs {0} does not match manifest entry count {1}".format(
                expected_pairs, len(entries)))
    action_getter = action_getter or toolkit.get_action
    validated, failures = [], []
    for entry in entries:
        try:
            validated.append((entry,) + _dedup_preflight_entry(session, entry))
        except Exception as error:
            failures.append((entry, error))
    if failures:
        for entry, keep, remove, plan in validated:
            _append_dedup_audit(audit_log, _dedup_audit_record(
                entry, "preflight_validated", validation_result=plan))
        for entry, error in failures:
            _append_dedup_audit(audit_log, _dedup_audit_record(
                entry, "preflight_failed",
                validation_result=getattr(error, "validation_result", {
                    "validation_stage": "preflight",
                    "error": str(error)}), error=str(error)))
        session.rollback()
        raise MoleculeSyncError(
            "manifest preflight failed; no mutations performed")

    results = []
    for entry, keep, remove, plan in validated:
        changes = _dedup_metadata_changes(keep, plan)
        synonym = _dedup_synonym(keep, remove, entry["inchi_key"])
        molecule_id = session.execute(text("""
            SELECT molecule_id FROM rdk.molecules
            WHERE upper(btrim(inchi_key))=:inchi_key
        """), {"inchi_key": entry["inchi_key"]}).scalar()
        synonym_exists = synonym and _dedup_synonym_exists(
            session, molecule_id, synonym)
        if plan["already_applied_candidate"]:
            if changes or (synonym and not synonym_exists):
                error = "already-applied pair has incomplete retained metadata"
                record = _dedup_audit_record(
                    entry, "failed", validation_result=plan, error=error)
                _append_dedup_audit(audit_log, record)
                raise MoleculeSyncError(error)
            record = _dedup_audit_record(
                entry, "already_applied", validation_result=plan,
                deletion_result="already_deleted",
                solr_result={"retained": "no_action_metadata_valid",
                             "removed": "already_removed"})
            _append_dedup_audit(audit_log, record)
            results.append(record)
            continue
        metadata_changes, synonym_changes = [], []
        try:
            payload = {"id": keep["id"]}
            payload.update(changes)
            action_getter("package_patch")(
                {"model": model, "session": session,
                 "ignore_auth": True, "user": "harvest"}, payload)
            metadata_changes = sorted(changes)
            if synonym and not synonym_exists:
                session.execute(text("""
                    INSERT INTO rdk.molecule_names
                      (molecule_id,name,type,source)
                    VALUES (:molecule_id,:name,'harvested_name','CKAN deduplication')
                """), {"molecule_id": molecule_id, "name": synonym})
                session.commit()
                synonym_changes = [synonym]
            deletion = action_getter("package_delete")(
                {"model": model, "session": session,
                 "ignore_auth": True, "user": "harvest"},
                {"id": remove["id"]})
            record = _dedup_audit_record(
                entry, "applied", validation_result=plan,
                metadata_changes=metadata_changes,
                synonym_changes=synonym_changes,
                deletion_result=deletion or "soft_deleted",
                solr_result={"retained": "reindexed_by_package_patch",
                             "removed": "removed_by_package_delete"})
            _append_dedup_audit(audit_log, record)
            results.append(record)
        except Exception as error:
            session.rollback()
            record = _dedup_audit_record(
                entry, "failed", validation_result=plan,
                metadata_changes=metadata_changes,
                synonym_changes=synonym_changes, error=str(error),
                solr_result="unknown_after_ckan_action_failure")
            _append_dedup_audit(audit_log, record)
            raise
    return results


def deduplicate_molecule_packages_dry_run(session, manifest_out):
    plans, blocked = [], []
    try:
        groups = session.execute(DUPLICATE_GROUPS_SQL).fetchall()
        for inchi_key, package_ids in groups:
            packages = [_load_dedup_package(session, item)
                        for item in package_ids]
            try:
                plans.append(validate_duplicate_pair(
                    session, inchi_key, packages))
            except Exception as error:
                references = {item["name"]: _package_references(session, item)
                              for item in packages}
                blocked.append({"inchi_key": normalized_inchi_key(inchi_key),
                                "packages": [item["name"] for item in packages],
                                "references": references,
                                "reason": str(error)})
        active_count = session.execute(text("""
            SELECT count(*) FROM public.package
            WHERE type='molecule' AND state='active'
        """)).scalar()
        rdk_count = session.execute(text(
            "SELECT count(*) FROM rdk.molecules")).scalar()
        fingerprint_count = session.execute(text(
            "SELECT count(*) FROM rdk.fingerprints")).scalar()
        _write_dedup_manifest(manifest_out, plans)
        summary = {
            "duplicate_groups_found": len(groups),
            "validated_pairs": len(plans), "blocked_pairs": len(blocked),
            "differing_titles": sum(int(x["differing_titles"]) for x in plans),
            "equivalent_differing_smiles": sum(
                int(x["equivalent_differing_smiles"]) for x in plans),
            "smiles_stereochemistry_mismatches": sum(
                int(x.get("warning") == "smiles_stereochemistry_mismatch")
                for x in plans),
            "planned_smiles_replacements": sum(
                int(x.get("planned_smiles_replacement", False))
                for x in plans),
            "missing_formulas": sum(len(x["missing_formulas"]) for x in plans),
            "conflicting_formulas": sum(
                int("conflicting molecular formula" in x["reason"])
                for x in blocked),
            "packages_to_retain": len(plans),
            "packages_to_soft_delete": len(plans),
            "relationships_requiring_migration": sum(
                x["relationships_requiring_migration"] for x in plans) + sum(
                    sum(sum(counts.values())
                        for counts in item["references"].values())
                    for item in blocked),
            "expected_active_molecule_packages": int(active_count) - len(plans),
            "expected_rdk_molecules": int(rdk_count),
            "expected_rdk_fingerprints": int(fingerprint_count),
            "database_changed": False,
        }
        return plans, blocked, summary
    finally:
        session.rollback()


RECOVERY_MANIFEST_FIELDS = [
    "inchi_key", "dataset_package", "keep_package", "remove_package",
    "relation_type",
]


def parse_relationship_recovery_manifest(filename):
    entries, rows, logical, removal_map = [], set(), set(), {}
    with open(filename, "r", newline="") as manifest:
        reader = csv.DictReader(manifest)
        if reader.fieldnames != RECOVERY_MANIFEST_FIELDS:
            raise MoleculeSyncError(
                "recovery manifest columns must be exactly: {0}".format(
                    ",".join(RECOVERY_MANIFEST_FIELDS)))
        for number, raw in enumerate(reader, 2):
            if None in raw or any(normalize_chemical_text(raw.get(field)) is None
                                  for field in RECOVERY_MANIFEST_FIELDS):
                raise MoleculeSyncError(
                    "malformed recovery manifest row {0}".format(number))
            entry = {field: raw[field].strip()
                     for field in RECOVERY_MANIFEST_FIELDS}
            entry["inchi_key"] = normalized_inchi_key(entry["inchi_key"])
            key_parts = entry["inchi_key"].split("-")
            if (len(key_parts) != 3 or len(key_parts[0]) != 14 or
                    len(key_parts[1]) != 10 or len(key_parts[2]) != 1 or
                    not all(part.isalpha() for part in key_parts)):
                raise MoleculeSyncError(
                    "malformed recovery InChIKey on row {0}".format(number))
            if entry["relation_type"] != "related_to":
                raise MoleculeSyncError(
                    "unexpected recovery relation type on row {0}".format(number))
            complete = tuple(entry[field] for field in RECOVERY_MANIFEST_FIELDS)
            mapping = (entry["dataset_package"], entry["keep_package"],
                       entry["relation_type"])
            removal = (entry["inchi_key"], entry["keep_package"])
            if complete in rows:
                raise MoleculeSyncError(
                    "duplicate complete recovery row {0}".format(number))
            if mapping in logical:
                raise MoleculeSyncError(
                    "duplicate logical recovery mapping on row {0}".format(number))
            previous = removal_map.get(entry["remove_package"])
            if previous is not None and previous != removal:
                raise MoleculeSyncError(
                    "removal package has inconsistent deduplication mapping: {0}"
                    .format(entry["remove_package"]))
            rows.add(complete)
            logical.add(mapping)
            removal_map[entry["remove_package"]] = removal
            entries.append(entry)
    return entries


def _relationship_count(session, subject, object_, relation_type):
    return int(session.execute(text("""
        SELECT count(*) FROM public.relationship_relationship
        WHERE subject_id IN (:subject_id,:subject_name)
          AND object_id IN (:object_id,:object_name)
          AND relation_type=:relation_type
    """), {"subject_id": subject["id"], "subject_name": subject["name"],
             "object_id": object_["id"], "object_name": object_["name"],
             "relation_type": relation_type}).scalar() or 0)


def _active_extra_values(package, key):
    matched = []
    for extra in package.get("extras") or []:
        if (str(extra.get("key") or "").strip().casefold() == key.casefold() and
                extra.get("state", "active") == "active"):
            matched.append(normalize_chemical_text(extra.get("value")))
    return matched


def _legacy_dataset_identities(session, dataset_id):
    rows = session.execute(text("""
        SELECT legacy.id, legacy.inchi_key
        FROM public.molecule_rel_data relationship
        JOIN public.molecules legacy ON legacy.id=relationship.molecules_id
        WHERE relationship.package_id=:dataset_id
        ORDER BY legacy.id
    """), {"dataset_id": dataset_id}).fetchall()
    return [(row[0], normalized_inchi_key(row[1])) for row in rows]


def _recovery_checks(entry):
    return {
        "manifest_inchi_key": entry["inchi_key"],
        "normalized_nonblank_dataset_inchi_keys": [],
        "dataset_inchi_key_extra_state": None,
        "matching_legacy_molecule_ids": [],
        "normalized_legacy_inchi_keys": [],
        "dataset_identity_source": None,
        "retained_molecule_inchi_key": None,
        "removed_molecule_inchi_key": None,
        "rdk_molecule_id": None,
        "stages": {
            "package_resolution": None,
            "package_states_and_types": None,
            "dataset_identity": None,
            "retained_molecule_identity": None,
            "removed_molecule_identity": None,
            "rdk_and_fingerprint": None,
            "relationship": None,
        },
    }


def _recovery_preflight_entry(session, entry):
    checks = _recovery_checks(entry)

    def fail(message):
        raise DedupPreflightError(message, checks)

    try:
        dataset = _load_dedup_package(session, entry["dataset_package"])
        keep = _load_dedup_package(session, entry["keep_package"])
        remove = _load_dedup_package(session, entry["remove_package"])
    except Exception as error:
        fail("package resolution failed: {0}".format(error))
    checks["stages"]["package_resolution"] = "validated"
    if (dataset["state"] != "active" or dataset["type"] == "molecule"):
        fail("dataset package must be active and not type=molecule")
    if keep["state"] != "active" or keep["type"] != "molecule":
        fail("retained package must be an active molecule")
    if remove["state"] != "deleted" or remove["type"] != "molecule":
        fail("removed package must be a deleted molecule")
    checks["stages"]["package_states_and_types"] = "validated"
    expected = entry["inchi_key"]

    dataset_extra_values = _active_extra_values(dataset, "inchi_key")
    dataset_keys = sorted(set(value.upper() for value in dataset_extra_values
                              if value is not None))
    checks["normalized_nonblank_dataset_inchi_keys"] = dataset_keys
    checks["dataset_inchi_key_extra_state"] = (
        "absent" if not dataset_extra_values else
        "blank" if not dataset_keys else "present")
    if dataset_keys:
        if dataset_keys != [expected]:
            fail("nonblank dataset InChIKey conflicts with recovery manifest")
        checks["dataset_identity_source"] = "dataset_extra"
    else:
        legacy = _legacy_dataset_identities(session, dataset["id"])
        checks["normalized_legacy_inchi_keys"] = [key for molecule_id, key
                                                   in legacy]
        checks["matching_legacy_molecule_ids"] = [
            molecule_id for molecule_id, key in legacy if key == expected]
        if not checks["matching_legacy_molecule_ids"]:
            fail("dataset has no matching legacy molecule relationship")
        checks["dataset_identity_source"] = "legacy_relationship"
    checks["stages"]["dataset_identity"] = "validated"

    keep_key = normalized_inchi_key(_package_value(keep, "inchi_key"))
    checks["retained_molecule_inchi_key"] = keep_key
    if keep_key != expected:
        fail("retained molecule InChIKey does not match recovery manifest")
    checks["stages"]["retained_molecule_identity"] = "validated"
    remove_key = normalized_inchi_key(_package_value(remove, "inchi_key"))
    checks["removed_molecule_inchi_key"] = remove_key
    if remove_key != expected:
        fail("removed molecule InChIKey does not match recovery manifest")
    checks["stages"]["removed_molecule_identity"] = "validated"

    rows = session.execute(text("""
        SELECT m.molecule_id, f.molecule_id IS NOT NULL,
               f.mfp2 IS NOT NULL, f.ffp2 IS NOT NULL
        FROM rdk.molecules m LEFT JOIN rdk.fingerprints f
          ON f.molecule_id=m.molecule_id
        WHERE upper(btrim(m.inchi_key))=:inchi_key
    """), {"inchi_key": expected}).fetchall()
    if len(rows) != 1 or not all(rows[0][1:]):
        fail("expected exactly one RDKit molecule with non-null fingerprint")
    checks["rdk_molecule_id"] = rows[0][0]
    checks["stages"]["rdk_and_fingerprint"] = "validated"
    forward = _relationship_count(
        session, dataset, keep, entry["relation_type"])
    reverse = _relationship_count(
        session, keep, dataset, entry["relation_type"])
    checks["stages"]["relationship"] = {
        "forward_rows": forward, "reverse_rows": reverse,
        "already_present": bool(forward and reverse)}
    if bool(forward) != bool(reverse):
        raise DedupPreflightError(
            "existing relationship is missing its reciprocal row", checks)
    already = bool(forward and reverse)
    return dataset, keep, remove, checks, already


def _relationship_audit(entry, status, preflight=None, creation=None,
                        reciprocal=None, solr=None, error=None):
    evidence = preflight or {}
    reindex_results = list(solr.values()) if isinstance(solr, dict) else []
    reindex_errors = [
        {"package_id": item.get("package_id"),
         "package_name": item.get("package_name"),
         "package_type": item.get("package_type"),
         "error": item.get("error")}
        for item in reindex_results if item.get("error")]
    relationship_status = ("already_present" if status.startswith(
        "already_present") else "created" if status.startswith("created")
        else status)
    return {"dataset_package": entry["dataset_package"],
            "retained_molecule": entry["keep_package"],
            "removed_molecule": entry["remove_package"],
            "inchi_key": entry["inchi_key"],
            "manifest_inchi_key": entry["inchi_key"],
            "relation_type": entry["relation_type"],
            "relationship_status": relationship_status,
            "forward_row_count": (reciprocal or {}).get("forward_rows"),
            "reverse_row_count": (reciprocal or {}).get("reverse_rows"),
            "normalized_nonblank_dataset_inchi_keys": evidence.get(
                "normalized_nonblank_dataset_inchi_keys"),
            "dataset_inchi_key_extra_state": evidence.get(
                "dataset_inchi_key_extra_state"),
            "matching_legacy_molecule_ids": evidence.get(
                "matching_legacy_molecule_ids"),
            "normalized_legacy_inchi_keys": evidence.get(
                "normalized_legacy_inchi_keys"),
            "identity_source": evidence.get("dataset_identity_source"),
            "retained_molecule_inchi_key": evidence.get(
                "retained_molecule_inchi_key"),
            "removed_molecule_inchi_key": evidence.get(
                "removed_molecule_inchi_key"),
            "rdk_molecule_id": evidence.get("rdk_molecule_id"),
            "validation_stages": evidence.get("stages"),
            "preflight_result": preflight,
            "creation_result": creation,
            "reciprocal_row_verification": reciprocal,
            "dataset_reindex_result": (solr or {}).get("dataset")
                if isinstance(solr, dict) else None,
            "retained_molecule_reindex_result": (solr or {}).get(
                "retained_molecule") if isinstance(solr, dict) else None,
            "solr_reindex_result": solr,
            "reindex_attempt_count": sum(
                item.get("attempts", 0) for item in reindex_results),
            "errors": ([{"scope": "relationship", "error": error}]
                       if error else []) + reindex_errors,
            "processing_continued_after_warning": False,
            "status": status, "error": error}


def _append_relationship_audit(filename, record):
    with open(filename, "a") as audit:
        audit.write(json.dumps(record, sort_keys=True) + "\n")
        audit.flush()


def _normalize_package_for_index(package):
    normalized = dict(package)
    for field in ("extras", "resources", "tags", "groups"):
        if normalized.get(field) is None:
            normalized[field] = []
    if normalized.get("organization") is None:
        normalized["organization"] = {}
    return normalized


def _reindex_recovery_package(package, action_getter, index_factory=None,
                              sleep=None, attempts=3, retry_delay=0.1):
    from ckan.lib.search.index import PackageSearchIndex
    index_factory = index_factory or PackageSearchIndex
    sleep = sleep or time.sleep
    result = {"package_id": package["id"], "package_name": package["name"],
              "package_type": package.get("type"), "status": "failed",
              "attempts": 0, "error": None,
              "cached_reindex_reused": False}
    for attempt in range(1, attempts + 1):
        result["attempts"] = attempt
        try:
            fresh = action_getter("package_show")(
                {"ignore_auth": True}, {"id": package["id"]})
            fresh = _normalize_package_for_index(fresh)
            result["package_name"] = fresh.get("name", result["package_name"])
            result["package_type"] = fresh.get("type", result["package_type"])
            index_factory().index_package(fresh)
            result["status"] = "reindexed"
            result["error"] = None
            return result
        except Exception as error:
            result["error"] = str(error)
            if attempt < attempts:
                sleep(min(max(retry_delay, 0), 1))
    return result


def _cached_reindex_result(package):
    return {"package_id": package["id"], "package_name": package["name"],
            "package_type": package.get("type"), "status": "cached",
            "attempts": 0, "error": None, "cached_reindex_reused": True}


def _failed_reindex_result(package, error):
    return {"package_id": package["id"], "package_name": package["name"],
            "package_type": package.get("type"), "status": "failed",
            "attempts": 0, "error": str(error),
            "cached_reindex_reused": False}


def _reindex_recovery_pair(dataset, keep, action_getter, reindexer, cache):
    try:
        dataset_result = reindexer(dataset, action_getter)
    except Exception as error:
        dataset_result = _failed_reindex_result(dataset, error)
    if keep["id"] in cache:
        keep_result = _cached_reindex_result(keep)
    else:
        try:
            keep_result = reindexer(keep, action_getter)
        except Exception as error:
            keep_result = _failed_reindex_result(keep, error)
        if keep_result["status"] == "reindexed":
            cache.add(keep["id"])
    return {"dataset": dataset_result, "retained_molecule": keep_result}


def recover_dedup_relationships(session, manifest, expected_relationships,
                                audit_log, apply_mode=False,
                                action_getter=None, reindexer=None):
    entries = parse_relationship_recovery_manifest(manifest)
    if len(entries) != expected_relationships:
        raise MoleculeSyncError(
            "expected-relationships {0} does not match manifest entry count {1}"
            .format(expected_relationships, len(entries)))
    action_getter = action_getter or toolkit.get_action
    reindexer = reindexer or _reindex_recovery_package
    validated, failures = [], []
    for entry in entries:
        try:
            validated.append((entry,) +
                             _recovery_preflight_entry(session, entry))
        except Exception as error:
            failures.append((entry, error))
    if failures:
        for item in validated:
            _append_relationship_audit(audit_log, _relationship_audit(
                item[0], "preflight_validated", preflight=item[4]))
        for entry, error in failures:
            _append_relationship_audit(audit_log, _relationship_audit(
                entry, "preflight_failed",
                preflight=getattr(error, "validation_result", None),
                error=str(error)))
        session.rollback()
        raise MoleculeSyncError(
            "relationship recovery preflight failed; no mutations performed")
    if not apply_mode:
        results = []
        for entry, dataset, keep, remove, checks, already in validated:
            status = "already_present" if already else "validated"
            record = _relationship_audit(entry, status, preflight=checks)
            _append_relationship_audit(audit_log, record)
            results.append(record)
        session.rollback()
        summary = {"requested": len(entries), "created": 0,
                   "already_present": sum(int(item[5]) for item in validated),
                   "relationship_failures": 0, "reindex_warnings": 0,
                   "completed": len(results)}
        return results, summary

    results, reindex_cache = [], set()
    for entry, dataset, keep, remove, checks, already in validated:
        if already:
            relationship_check = (checks.get("relationship") or
                                  checks.get("stages", {}).get("relationship"))
            solr = _reindex_recovery_pair(
                dataset, keep, action_getter, reindexer, reindex_cache)
            warning = any(item["status"] == "failed"
                          for item in solr.values())
            record = _relationship_audit(
                entry, ("already_present_with_reindex_warning" if warning
                        else "already_present"), preflight=checks,
                reciprocal=relationship_check, solr=solr)
            record["processing_continued_after_warning"] = warning
            _append_relationship_audit(audit_log, record)
            results.append(record)
            continue
        try:
            created, reciprocal = None, None
            created = action_getter("relationship_relation_create")(
                {"model": model, "session": session,
                 "ignore_auth": True, "user": "harvest"},
                {"subject_id": dataset["id"], "object_id": keep["id"],
                 "relation_type": entry["relation_type"]})
            forward = _relationship_count(
                session, dataset, keep, entry["relation_type"])
            reverse = _relationship_count(
                session, keep, dataset, entry["relation_type"])
            reciprocal = {"forward_rows": forward, "reverse_rows": reverse}
            if not forward or not reverse:
                raise MoleculeSyncError(
                    "relationship action did not create reciprocal rows")
        except Exception as error:
            session.rollback()
            record = _relationship_audit(
                entry, "failed", preflight=checks, creation=created,
                reciprocal=reciprocal, error=str(error),
                solr=None)
            record["processing_continued_after_warning"] = False
            _append_relationship_audit(audit_log, record)
            raise
        solr = _reindex_recovery_pair(
            dataset, keep, action_getter, reindexer, reindex_cache)
        warning = any(item["status"] == "failed" for item in solr.values())
        record = _relationship_audit(
            entry, "created_with_reindex_warning" if warning else "created",
            preflight=checks, creation=created, reciprocal=reciprocal,
            solr=solr)
        record["processing_continued_after_warning"] = warning
        _append_relationship_audit(audit_log, record)
        results.append(record)
    summary = {
        "requested": len(entries),
        "created": sum(int(item["status"].startswith("created"))
                       for item in results),
        "already_present": sum(
            int(item["status"].startswith("already_present"))
            for item in results),
        "relationship_failures": 0,
        "reindex_warnings": sum(
            int(item["status"].endswith("reindex_warning"))
            for item in results),
        "completed": len(results),
    }
    return results, summary


CLEANUP_RELATIONSHIP_MANIFEST_FIELDS = [
    "dataset_id", "dataset_name", "molecule_id", "molecule_name",
    "relation_type",
]


def parse_inactive_relationship_manifest(filename):
    entries, logical = [], set()
    with open(filename, "r", newline="") as manifest:
        reader = csv.DictReader(manifest)
        if reader.fieldnames != CLEANUP_RELATIONSHIP_MANIFEST_FIELDS:
            raise MoleculeSyncError(
                "cleanup manifest columns must be exactly: {0}".format(
                    ",".join(CLEANUP_RELATIONSHIP_MANIFEST_FIELDS)))
        for number, raw in enumerate(reader, 2):
            if None in raw or any(normalize_chemical_text(raw.get(field)) is None
                                  for field in
                                  CLEANUP_RELATIONSHIP_MANIFEST_FIELDS):
                raise MoleculeSyncError(
                    "malformed cleanup manifest row {0}".format(number))
            entry = {field: raw[field].strip()
                     for field in CLEANUP_RELATIONSHIP_MANIFEST_FIELDS}
            if entry["relation_type"] != "related_to":
                raise MoleculeSyncError(
                    "unexpected cleanup relation type on row {0}".format(
                        number))
            pair = (entry["dataset_id"], entry["molecule_id"],
                    entry["relation_type"])
            if pair in logical:
                raise MoleculeSyncError(
                    "duplicate logical cleanup pair on row {0}".format(number))
            logical.add(pair)
            entries.append(entry)
    return entries


def _cleanup_preflight_entry(session, entry, package_loader=None):
    package_loader = package_loader or _load_dedup_package
    result = {"dataset": None, "molecule": None,
              "forward_rows_before": None, "reverse_rows_before": None,
              "already_deleted": False}

    def fail(message):
        raise DedupPreflightError(message, result)

    try:
        dataset_by_id = package_loader(session, entry["dataset_id"])
        dataset_by_name = package_loader(session, entry["dataset_name"])
        if (dataset_by_id["id"] != dataset_by_name["id"] or
                dataset_by_id["id"] != entry["dataset_id"] or
                dataset_by_id["name"] != entry["dataset_name"]):
            fail("dataset ID and name do not resolve to the same package")
        result["dataset"] = {
            "id": dataset_by_id["id"], "name": dataset_by_id["name"],
            "type": dataset_by_id["type"], "state": dataset_by_id["state"]}
        if dataset_by_id["type"] == "molecule":
            fail("cleanup dataset must not have type=molecule")
        if dataset_by_id["state"] != "deleted":
            fail("cleanup dataset must be deleted")

        molecule_by_id = package_loader(session, entry["molecule_id"])
        molecule_by_name = package_loader(session, entry["molecule_name"])
        if (molecule_by_id["id"] != molecule_by_name["id"] or
                molecule_by_id["id"] != entry["molecule_id"] or
                molecule_by_id["name"] != entry["molecule_name"]):
            fail("molecule ID and name do not resolve to the same package")
        result["molecule"] = {
            "id": molecule_by_id["id"], "name": molecule_by_id["name"],
            "type": molecule_by_id["type"], "state": molecule_by_id["state"]}
        if molecule_by_id["type"] != "molecule":
            fail("cleanup molecule must have type=molecule")
        if molecule_by_id["state"] != "active":
            fail("cleanup molecule must be active")

        forward = _relationship_count(
            session, dataset_by_id, molecule_by_id, entry["relation_type"])
        reverse = _relationship_count(
            session, molecule_by_id, dataset_by_id, entry["relation_type"])
        result["forward_rows_before"] = forward
        result["reverse_rows_before"] = reverse
        if forward == 0 and reverse == 0:
            result["already_deleted"] = True
        elif forward != 1 or reverse != 1:
            fail("cleanup relationship must have exactly one reciprocal row "
                 "in each direction")
        return dataset_by_id, molecule_by_id, result
    except DedupPreflightError:
        raise
    except Exception as error:
        fail(str(error))


def _cleanup_relationship_audit(entry, status, preflight=None,
                                deletion=None, after=None, reindex=None,
                                error=None):
    before = preflight or {}
    after = after or {}
    return {
        "dataset_id": entry["dataset_id"],
        "dataset_name": entry["dataset_name"],
        "molecule_id": entry["molecule_id"],
        "molecule_name": entry["molecule_name"],
        "relation_type": entry["relation_type"],
        "preflight_result": preflight,
        "deletion_action_result": deletion,
        "forward_rows_before": before.get("forward_rows_before"),
        "reverse_rows_before": before.get("reverse_rows_before"),
        "forward_rows_after": after.get("forward_rows_after"),
        "reverse_rows_after": after.get("reverse_rows_after"),
        "molecule_reindex_result": reindex,
        "status": status,
        "error": error,
    }


def cleanup_inactive_relationships(session, manifest, expected_relationships,
                                   audit_log, apply_mode=False,
                                   action_getter=None, reindexer=None,
                                   package_loader=None):
    entries = parse_inactive_relationship_manifest(manifest)
    if len(entries) != expected_relationships:
        raise MoleculeSyncError(
            "expected-relationships {0} does not match manifest entry count {1}"
            .format(expected_relationships, len(entries)))
    action_getter = action_getter or toolkit.get_action
    reindexer = reindexer or _reindex_recovery_package
    validated, failures = [], []
    for entry in entries:
        try:
            validated.append((entry,) + _cleanup_preflight_entry(
                session, entry, package_loader=package_loader))
        except Exception as error:
            failures.append((entry, error))
    if failures:
        for entry, dataset, molecule, preflight in validated:
            _append_relationship_audit(audit_log, _cleanup_relationship_audit(
                entry, "validated", preflight=preflight))
        for entry, error in failures:
            _append_relationship_audit(audit_log, _cleanup_relationship_audit(
                entry, "failed",
                preflight=getattr(error, "validation_result", None),
                error=str(error)))
        session.rollback()
        raise MoleculeSyncError(
            "inactive relationship cleanup preflight failed; "
            "no mutations performed")

    if not apply_mode:
        results = []
        for entry, dataset, molecule, preflight in validated:
            record = _cleanup_relationship_audit(
                entry, "validated", preflight=preflight)
            _append_relationship_audit(audit_log, record)
            results.append(record)
        session.rollback()
        return results

    results = []
    for entry, dataset, molecule, preflight in validated:
        if preflight["already_deleted"]:
            record = _cleanup_relationship_audit(
                entry, "already_deleted", preflight=preflight,
                after={"forward_rows_after": 0, "reverse_rows_after": 0})
            _append_relationship_audit(audit_log, record)
            results.append(record)
            continue
        deletion, after = None, None
        try:
            deletion = action_getter("relationship_relation_delete")(
                {"model": model, "session": session,
                 "ignore_auth": True, "user": "harvest"},
                {"subject_id": dataset["id"],
                 "object_id": molecule["id"],
                 "relation_type": entry["relation_type"]})
            forward = _relationship_count(
                session, dataset, molecule, entry["relation_type"])
            reverse = _relationship_count(
                session, molecule, dataset, entry["relation_type"])
            after = {"forward_rows_after": forward,
                     "reverse_rows_after": reverse}
            if forward != 0 or reverse != 0:
                raise MoleculeSyncError(
                    "relationship action did not delete both reciprocal rows")
        except Exception as error:
            session.rollback()
            record = _cleanup_relationship_audit(
                entry, "failed", preflight=preflight, deletion=deletion,
                after=after, error=str(error))
            _append_relationship_audit(audit_log, record)
            raise

        try:
            reindex = reindexer(molecule, action_getter)
        except Exception as error:
            reindex = _failed_reindex_result(molecule, error)
        warning = reindex["status"] == "failed"
        record = _cleanup_relationship_audit(
            entry, "deleted_with_reindex_warning" if warning else "deleted",
            preflight=preflight, deletion=deletion, after=after,
            reindex=reindex, error=reindex.get("error") if warning else None)
        _append_relationship_audit(audit_log, record)
        results.append(record)
    return results


@click.group(name="harvester4chem")
def harvester4chem():
    """Safely synchronize harvested chemistry with PostgreSQL RDKit."""


@harvester4chem.command(name="verify")
def verify_command():
    """Report consistency counts without writing to the database."""
    try:
        for label, query in VERIFY_SQL.items():
            click.echo("{0}={1}".format(
                label, model.Session.execute(query).scalar()
            ))
    finally:
        model.Session.rollback()


@harvester4chem.command(name="sync-package")
@click.argument("package_id")
@click.option("--dry-run", is_flag=True,
              help="Validate and prepare the plan without persistent writes.")
@click.option("--apply", "apply_mode", is_flag=True,
              help="Apply the validated, resumable plan.")
@click.option("--audit-log", type=click.Path(dir_okay=False))
@click.option("--confirm")
@click.option("--expected-inchi-key")
@click.option("--write-legacy/--no-write-legacy", default=None,
              help=("Diagnostic override for legacy-table validation; the "
                    "default is ckan.harvester4chem.write_legacy."))
def sync_package_command(package_id, dry_run, apply_mode, audit_log, confirm,
                         expected_inchi_key, write_legacy):
    """Validate and synchronize one active dataset; safe to rerun by design."""
    if dry_run == apply_mode:
        raise click.UsageError("exactly one of --dry-run or --apply is required")
    if apply_mode and not audit_log:
        raise click.UsageError("--apply requires --audit-log PATH")
    if apply_mode and confirm != "SYNCHRONIZE_VALIDATED_MOLECULE":
        raise click.UsageError(
            "--apply requires --confirm SYNCHRONIZE_VALIDATED_MOLECULE")
    mode = "apply" if apply_mode else "dry-run"
    record = {"timestamp": datetime.utcnow().isoformat() + "Z", "mode": mode,
              "dataset_id": package_id, "dataset_name": None,
              "normalized_inchi_key": None, "validation_result": "failed",
              "molecule_package_id": None, "molecule_package_name": None,
              "molecule_package_status": "not_run",
              "rdkit_molecule_id": None, "rdkit_molecule_status": "not_run",
              "fingerprint_status": "not_run", "relationship_status": "not_run",
              "dataset_solr_status": "not_run", "molecule_solr_status": "not_run",
              "overall_status": "failed", "error": None, "warning": None}
    try:
        package = toolkit.get_action("package_show")(
            {"ignore_auth": True}, {"id": package_id})
        if package.get("state", "active") != "active" or \
                package.get("type", "dataset") == "molecule":
            raise MoleculeSyncError("source must be an active dataset")
        record.update(dataset_id=package["id"], dataset_name=package.get("name"))
        chemistry = normalize_structure(
            _package_value(package, "inchi"), _package_value(package, "inchi_key"),
            _package_value(package, "smiles"), _package_value(package, "mol_formula"),
            _package_value(package, "exactmass") or _package_value(package, "exact_mass"))
        record["normalized_inchi_key"] = chemistry["inchi_key"]
        expected = normalized_inchi_key(expected_inchi_key)
        if expected and expected != chemistry["inchi_key"]:
            raise MoleculeSyncError(
                "expected InChIKey {0}, calculated {1}".format(
                    expected, chemistry["inchi_key"]))
        record["validation_result"] = "valid"
        result = synchronize_molecule(
            package_id=package["id"], inchi_code=chemistry["inchi_code"],
            inchi_key=chemistry["inchi_key"], smiles=chemistry["canonical_smiles"],
            mol_formula=chemistry["mol_formula"], exact_mass=chemistry["exact_mass"],
            names=[package.get("title")], name_source="CKAN", session=model.Session,
            dry_run=dry_run, write_legacy=write_legacy)
        record.update(
            molecule_package_id=result.get("molecule_package_id"),
            molecule_package_status=result.get("molecule_package", "planned"),
            rdkit_molecule_id=result.get("rdk_molecule_id"),
            rdkit_molecule_status=result.get("rdkit_molecule_status", "ensured"),
            fingerprint_status=result.get("fingerprint_status", "ensured"),
            relationship_status=result.get("ckan_relationship", "planned"))
        molecule_id = result.get("molecule_package_id")
        molecule = toolkit.get_action("package_show")(
            {"ignore_auth": True}, {"id": molecule_id}) \
            if molecule_id and (not dry_run or
                                result.get("molecule_package") == "existing") else None
        record["molecule_package_name"] = (molecule or {}).get("name")
        if dry_run:
            if result.get("molecule_package") == "allocated_on_apply":
                record["molecule_package_name"] = "allocated_on_apply"
            record["warning"] = (
                "RDKit rows are rolled back, but PostgreSQL sequence values "
                "used for surrogate RDKit IDs may advance during dry-run")
            model.Session.rollback()
            record["overall_status"] = "dry_run_validated"
        else:
            # Database/action stages may already have committed independently.
            # Indexing is deliberately reported and retried as a separate stage.
            model.Session.commit()
            warnings = []
            for key, item in (("dataset_solr_status", package),
                              ("molecule_solr_status", molecule)):
                indexed = _reindex_recovery_package(item, toolkit.get_action)
                record[key] = indexed["status"]
                if indexed["status"] == "failed":
                    warnings.append(item["name"])
            if warnings:
                record["overall_status"] = "completed_with_index_warning"
                record["warning"] = "individual reindex required: " + ", ".join(warnings)
            else:
                record["overall_status"] = "completed"
    except Exception as error:
        model.Session.rollback()
        record["error"] = str(error)
        if apply_mode and record["validation_result"] == "valid":
            record["overall_status"] = "incomplete_resumable"
        raise
    finally:
        line = json.dumps(record, sort_keys=True)
        if audit_log:
            with open(audit_log, "a") as stream:
                stream.write(line + "\n")
        click.echo(line)


@harvester4chem.command(name="repair-missing-rdk")
@click.option("--manifest", type=click.Path(exists=True, dir_okay=False),
              required=True)
@click.option("--dry-run", is_flag=True)
@click.option("--apply", "apply_mode", is_flag=True)
def repair_missing_rdk_command(manifest, dry_run, apply_mode):
    """Backfill explicitly listed molecule packages into rdk.* only."""
    if dry_run == apply_mode:
        raise click.UsageError("exactly one of --dry-run or --apply is required")
    mode = "dry-run" if dry_run else "apply"
    names = parse_repair_manifest(manifest)
    if not names:
        raise click.ClickException("manifest contains no package names")
    results, summary = repair_missing_rdk(names, mode)
    for result in results:
        click.echo(json.dumps(result, sort_keys=True))
    click.echo(json.dumps(summary, sort_keys=True))
    if summary["failed"]:
        raise click.ClickException("manifest preflight failed; no rows written")


@harvester4chem.command(name="deduplicate-molecule-packages")
@click.option("--dry-run", is_flag=True,
              help="Audit and write only the CSV plan; never change CKAN.")
@click.option("--apply", "apply_mode", is_flag=True,
              help="Apply only pairs from an explicit validated manifest.")
@click.option("--manifest-out", type=click.Path(dir_okay=False))
@click.option("--manifest", type=click.Path(exists=True, dir_okay=False))
@click.option("--expected-pairs", type=click.IntRange(min=0))
@click.option("--audit-log", type=click.Path(dir_okay=False))
@click.option("--confirm")
def deduplicate_molecule_packages_command(dry_run, apply_mode, manifest_out,
                                          manifest, expected_pairs, audit_log,
                                          confirm):
    """Audit duplicates or apply a previously generated explicit manifest."""
    if dry_run == apply_mode:
        raise click.UsageError(
            "exactly one of --dry-run or --apply is required")
    if apply_mode:
        if not manifest or expected_pairs is None or not audit_log:
            raise click.UsageError(
                "--apply requires --manifest, --expected-pairs and --audit-log")
        if confirm != "SOFT_DELETE_VALIDATED_DUPLICATES":
            raise click.UsageError(
                "--apply requires --confirm SOFT_DELETE_VALIDATED_DUPLICATES")
        try:
            results = apply_dedup_manifest(
                model.Session, manifest, expected_pairs, audit_log)
        except Exception as error:
            raise click.ClickException(str(error))
        for result in results:
            click.echo(json.dumps(result, sort_keys=True))
        return
    if not manifest_out:
        raise click.UsageError("--dry-run requires --manifest-out")
    plans, blocked, summary = deduplicate_molecule_packages_dry_run(
        model.Session, manifest_out)
    for plan in plans:
        click.echo(json.dumps(plan, sort_keys=True))
    for item in blocked:
        click.echo(json.dumps({"status": "blocked", **item},
                              sort_keys=True))
    for key in (
            "duplicate_groups_found", "validated_pairs", "blocked_pairs",
            "differing_titles", "equivalent_differing_smiles",
            "smiles_stereochemistry_mismatches",
            "planned_smiles_replacements",
            "missing_formulas", "conflicting_formulas",
            "packages_to_retain", "packages_to_soft_delete",
            "relationships_requiring_migration",
            "expected_active_molecule_packages", "expected_rdk_molecules",
            "expected_rdk_fingerprints", "database_changed"):
        click.echo("{0}={1}".format(key, str(summary[key]).lower()
                                   if isinstance(summary[key], bool)
                                   else summary[key]))


@harvester4chem.command(name="recover-dedup-relationships")
@click.option("--dry-run", is_flag=True)
@click.option("--apply", "apply_mode", is_flag=True)
@click.option("--manifest", type=click.Path(exists=True, dir_okay=False),
              required=True)
@click.option("--expected-relationships", type=click.IntRange(min=0),
              required=True)
@click.option("--audit-log", type=click.Path(dir_okay=False), required=True)
@click.option("--confirm")
def recover_dedup_relationships_command(dry_run, apply_mode, manifest,
                                        expected_relationships, audit_log,
                                        confirm):
    """Recover validated dataset-to-retained-molecule relationships."""
    if dry_run == apply_mode:
        raise click.UsageError(
            "exactly one of --dry-run or --apply is required")
    if apply_mode and confirm != "RECOVER_VALIDATED_RELATIONSHIPS":
        raise click.UsageError(
            "--apply requires --confirm RECOVER_VALIDATED_RELATIONSHIPS")
    try:
        results, summary = recover_dedup_relationships(
            model.Session, manifest, expected_relationships, audit_log,
            apply_mode=apply_mode)
    except Exception as error:
        raise click.ClickException(str(error))
    for result in results:
        click.echo(json.dumps(result, sort_keys=True)
                   if isinstance(result, dict) else result)
    click.echo(json.dumps(summary, sort_keys=True))


@harvester4chem.command(name="cleanup-inactive-relationships")
@click.option("--dry-run", is_flag=True)
@click.option("--apply", "apply_mode", is_flag=True)
@click.option("--manifest", type=click.Path(exists=True, dir_okay=False),
              required=True)
@click.option("--expected-relationships", type=click.IntRange(min=0),
              required=True)
@click.option("--audit-log", type=click.Path(dir_okay=False), required=True)
@click.option("--confirm")
def cleanup_inactive_relationships_command(dry_run, apply_mode, manifest,
                                           expected_relationships, audit_log,
                                           confirm):
    """Delete manifested relationships belonging to deleted datasets."""
    if dry_run == apply_mode:
        raise click.UsageError(
            "exactly one of --dry-run or --apply is required")
    if apply_mode and confirm != "DELETE_STALE_RELATIONSHIPS":
        raise click.UsageError(
            "--apply requires --confirm DELETE_STALE_RELATIONSHIPS")
    try:
        results = cleanup_inactive_relationships(
            model.Session, manifest, expected_relationships, audit_log,
            apply_mode=apply_mode)
    except Exception as error:
        raise click.ClickException(str(error))
    for result in results:
        click.echo(json.dumps(result, sort_keys=True))


def get_commands():
    return [harvester4chem]
