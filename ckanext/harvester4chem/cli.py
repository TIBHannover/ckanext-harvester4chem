import json

import click
from sqlalchemy import text

import ckan.model as model
import ckan.plugins.toolkit as toolkit

from ckanext.harvester4chem.molecule_sync import synchronize_molecule


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
    "dataset_chemistry_missing_molecule_package": text("""
        SELECT count(DISTINCT p.id) FROM package p
        JOIN package_extra dk ON dk.package_id=p.id AND dk.key='inchi_key'
        LEFT JOIN package_extra mk ON mk.key='inchi_key'
          AND upper(btrim(mk.value))=upper(btrim(dk.value))
        LEFT JOIN package mp ON mp.id=mk.package_id AND mp.type='molecule'
          AND mp.state='active'
        WHERE p.state='active' AND p.type<>'molecule' AND mp.id IS NULL
    """),
    "ambiguous_duplicate_molecule_packages": text("""
        SELECT count(*) FROM (
          SELECT upper(btrim(e.value)) FROM package p
          JOIN package_extra e ON e.package_id=p.id AND e.key='inchi_key'
          WHERE p.type='molecule' AND p.state='active'
          GROUP BY upper(btrim(e.value)) HAVING count(DISTINCT p.id)>1
        ) duplicate
    """),
    "molecule_packages_missing_rdk_molecule": text("""
        SELECT count(DISTINCT p.id) FROM package p
        JOIN package_extra i ON i.package_id=p.id AND i.key='inchi'
        LEFT JOIN rdk.molecules m ON m.inchi_code=btrim(i.value)
        WHERE p.type='molecule' AND p.state='active' AND m.molecule_id IS NULL
    """),
    "molecule_packages_with_rdkit_inchi_key_mismatch": text("""
        SELECT count(*) FROM package p
        JOIN package_extra i ON i.package_id=p.id AND i.key='inchi'
        JOIN package_extra k ON k.package_id=p.id AND k.key='inchi_key'
        JOIN rdk.molecules m ON m.inchi_code=btrim(i.value)
        WHERE p.type='molecule' AND p.state='active'
          AND upper(btrim(k.value))<>upper(coalesce(m.inchi_key,''))
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
        JOIN package_extra mk ON mk.key='inchi_key'
          AND upper(btrim(mk.value))=upper(btrim(dk.value))
        JOIN package mp ON mp.id=mk.package_id AND mp.type='molecule' AND mp.state='active'
        LEFT JOIN relationship_relationship r ON r.subject_id=d.id
          AND r.object_id=mp.id AND r.relation_type='related_to'
        WHERE d.state='active' AND d.type<>'molecule' AND r.id IS NULL
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
def sync_package_command(package_id, dry_run):
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
    )
    model.Session.rollback()
    click.echo(json.dumps(result, sort_keys=True))


def get_commands():
    return [harvester4chem]
