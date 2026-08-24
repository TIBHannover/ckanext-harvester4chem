import json

import click
from sqlalchemy import text

import ckan.model as model
import ckan.plugins.toolkit as toolkit

from ckanext.harvester4chem.molecule_sync import synchronize_molecule


VERIFY_SQL = {
    "packages_missing_molecules": text("""
        SELECT count(*) FROM package p
        JOIN package_extra pi ON pi.package_id = p.id AND pi.key = 'inchi'
        LEFT JOIN rdk.molecules m ON m.inchi_code = btrim(pi.value)
        WHERE p.state = 'active' AND btrim(pi.value) <> ''
          AND m.molecule_id IS NULL
    """),
    "molecules_missing_fingerprints": text("""
        SELECT count(*) FROM rdk.molecules m
        LEFT JOIN rdk.fingerprints f ON f.molecule_id = m.molecule_id
        WHERE f.molecule_id IS NULL
    """),
    "null_fingerprints": text("""
        SELECT count(*) FROM rdk.fingerprints
        WHERE mfp2 IS NULL OR ffp2 IS NULL
    """),
    "relationships_missing_rdk_molecules": text("""
        SELECT count(*) FROM public.molecule_rel_data r
        LEFT JOIN rdk.molecules m ON m.molecule_id = r.molecules_id
        WHERE m.molecule_id IS NULL
    """),
    "duplicate_package_molecule_relationships": text("""
        SELECT count(*) FROM (
            SELECT package_id, molecules_id
            FROM public.molecule_rel_data
            GROUP BY package_id, molecules_id HAVING count(*) > 1
        ) duplicates
    """),
    "package_rdkit_inchi_key_mismatches": text("""
        SELECT count(*) FROM package p
        JOIN package_extra pk
          ON pk.package_id = p.id AND pk.key = 'inchi_key'
        JOIN public.molecule_rel_data r ON r.package_id = p.id
        JOIN rdk.molecules m ON m.molecule_id = r.molecules_id
        WHERE p.state = 'active'
          AND upper(btrim(pk.value)) <> upper(coalesce(m.inchi_key, ''))
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
