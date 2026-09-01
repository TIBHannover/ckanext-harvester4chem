import json

import click
from sqlalchemy import text

import ckan.model as model
import ckan.plugins.toolkit as toolkit

from ckanext.harvester4chem.molecule_sync import (
    MoleculeSyncError, create_validated_rdk_backfill, normalize_chemical_text,
    synchronize_molecule, validate_rdk_backfill_package,
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


def _package_value(package, key):
    value = package.get(key)
    if value is not None:
        return value
    for extra in package.get("extras") or []:
        if extra.get("key") == key:
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
@click.option("--dry-run", is_flag=True, required=True,
              help="Execute all SQL and roll it back.")
@click.option("--write-legacy/--no-write-legacy", default=None,
              help=("Diagnostic override for legacy-table validation; the "
                    "default is ckan.harvester4chem.write_legacy."))
def sync_package_command(package_id, dry_run, write_legacy):
    """Validate and dry-run synchronization of one existing CKAN package."""
    package = toolkit.get_action("package_show")(
        {"ignore_auth": True}, {"id": package_id}
    )
    result = synchronize_molecule(
        package_id=package["id"],
        inchi_code=_package_value(package, "inchi"),
        inchi_key=_package_value(package, "inchi_key"),
        smiles=_package_value(package, "smiles"),
        mol_formula=_package_value(package, "mol_formula"),
        exact_mass=(_package_value(package, "exactmass") or
                    _package_value(package, "exact_mass")),
        names=[package.get("title")],
        name_source="CKAN",
        session=model.Session,
        dry_run=dry_run,
        write_legacy=write_legacy,
    )
    model.Session.rollback()
    click.echo(json.dumps(result, sort_keys=True))


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


def get_commands():
    return [harvester4chem]
