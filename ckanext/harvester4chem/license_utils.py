"""Resolve source license metadata exclusively against CKAN's live registry."""

import logging
import re
from urllib.parse import urlsplit, urlunsplit

from ckan.logic import get_action

log = logging.getLogger(__name__)


def extract_license_values(value):
    """Flatten license/rights strings, lists and common JSON-LD objects."""
    if isinstance(value, str):
        if value.strip():
            yield value.strip()
    elif isinstance(value, dict):
        for key in ('@id', 'url', 'id', 'name', 'title', '@value'):
            yield from extract_license_values(value.get(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from extract_license_values(item)


def normalize_license_value(value):
    """Trim text; normalize HTTP URL host/scheme and trailing path slashes.

    Paths, queries and fragments remain case sensitive. Only the known CC
    host's license/public-domain paths permit HTTP/HTTPS equivalence.
    """
    value = value.strip()
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
            return value
        # Do not reinterpret credentials or malformed ports as license URLs.
        if parts.username is not None or parts.password is not None:
            return value
        port = parts.port
    except ValueError:
        return value
    host = parts.hostname.lower()
    netloc = '[{}]'.format(host) if ':' in host else host
    if port is not None:
        netloc += ':{}'.format(port)
    scheme = parts.scheme.lower()
    if (host == 'creativecommons.org' and port is None
            and parts.path.startswith(('/licenses/', '/publicdomain/'))):
        scheme = 'https'
    return urlunsplit((scheme, netloc, parts.path.rstrip('/'),
                       parts.query, parts.fragment))


def _text_key(value):
    return ' '.join(value.split()).casefold()


def _cc_alias(value):
    """Allow CC identifier spacing, never arbitrary name punctuation changes.

    The old OAI matcher accepted arbitrary suffixes after CC IDs. Limit suffix
    support to parenthetical descriptions instead of unrestricted startswith.
    This recognizes notation only: the ID must still exist in license_list.
    """
    match = re.fullmatch(
        r'(CC(?:[ -]+[A-Z]+)+[ -]+\d+\.\d+)(?:\s+\([^()]*\))?',
        value.strip(), re.IGNORECASE)
    return re.sub(r'[ -]+', '-', match.group(1)).casefold() if match else None


def resolve_license_id(source_license, context, dataset_id=None):
    """Return a registered ID or None; unknown metadata never blocks import.

    Fetch per resolution to respect registry changes without global cache state.
    All candidates are checked at each priority before trying weaker matches.
    """
    candidates = list(extract_license_values(source_license))
    if not candidates:
        if source_license:
            log.warning('HARVESTER4CHEM unknown license for dataset %s: '
                        'source_license=%r', dataset_id, source_license)
        return None
    try:
        licenses = get_action('license_list')(context.copy(), {})
    except Exception:
        log.warning('HARVESTER4CHEM could not retrieve license registry for '
                    'dataset %s: source_license=%r', dataset_id,
                    source_license, exc_info=True)
        return None
    licenses = [entry for entry in licenses if entry.get('id')]
    normalized = [normalize_license_value(value) for value in candidates]
    for priority in range(5):
        for value, normal in zip(candidates, normalized):
            matches = []
            for entry in licenses:
                identifier = entry['id']
                if priority == 0:
                    match = value == identifier
                elif priority == 1:
                    url = entry.get('url')
                    match = bool(url) and normal == normalize_license_value(url)
                elif priority == 2:
                    match = value.casefold() == identifier.casefold()
                elif priority == 3:
                    match = _text_key(value) == _text_key(entry.get('title') or '')
                else:
                    alias = _cc_alias(value)
                    match = alias is not None and alias == _cc_alias(identifier)
                if match:
                    matches.append(identifier)
            if len(set(matches)) == 1:
                log.debug('HARVESTER4CHEM license for dataset %s: source=%r '
                          'normalized=%r license_id=%r',
                          dataset_id, value, normal, matches[0])
                return matches[0]
            if matches:
                # Ambiguous registry aliases must not pick an arbitrary ID.
                log.warning('HARVESTER4CHEM ambiguous license for dataset %s: '
                            'source_license=%r', dataset_id, source_license)
                return None
    log.warning('HARVESTER4CHEM unknown license for dataset %s: '
                'source_license=%r', dataset_id, source_license)
    return None


def apply_license(package_dict, source_license, context):
    """Only supply resolved licenses; omission preserves CKAN update metadata."""
    resolved = resolve_license_id(
        source_license, context, package_dict.get('id') or package_dict.get('name'))
    if resolved is not None:
        package_dict['license_id'] = resolved
