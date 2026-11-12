"""Download the agreed eight Russian extracts into an owned temporary workspace."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .classification import CONFIG
from .collection import inspect_sources
from .sources import obtain_source

LOG = logging.getLogger(__name__)
BASE_URL = 'https://download.geofabrik.de/russia/'


def download_collection(profile, folder, *, windows_proxy=False):
    approved = json.loads((CONFIG/'collection.json').read_text(encoding='utf-8'))
    if (profile.get('coverage_id') != approved['coverage_id']
            or set(profile['expected_extracts']) != set(approved['expected_extracts'])
            or set(profile.get('excluded_extracts', [])) != set(approved['excluded_extracts'])
            or profile.get('complete_snapshot_confirmed') is not True):
        raise ValueError('Geofabrik download requires the agreed complete eight-extract profile and coverage')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    session_factory = None
    if windows_proxy:
        from .windows_proxy import WindowsProxySession
        session_factory = lambda: WindowsProxySession(folder)
        LOG.info('DOWNLOAD transport: Windows curl, SSPI Negotiate, Schannel revocation best-effort')
    downloaded = []
    for number, extract in enumerate(profile['expected_extracts'], 1):
        filename = extract+'-latest.osm.pbf'
        url = BASE_URL+filename
        LOG.info('DOWNLOAD %d/%d: %s', number, len(profile['expected_extracts']), filename)
        source = obtain_source({'coverage_id': profile['coverage_id'], 'sources': [{
            'url': url, 'checksum_url': url+'.md5', 'checksum_algorithm': 'md5',
            'coverage_id': profile['coverage_id'], 'complete': False,
            'max_age_days': profile.get('max_age_days', 45), 'retries': 3,
        }]}, folder, force=True, session_factory=session_factory)
        target = folder/filename
        Path(source['path']).replace(target)
        (folder/(filename+'.md5')).write_text(source['publisher_checksum']+'  '+filename+'\n', encoding='ascii')
        downloaded.append({key: source[key] for key in ('source_url', 'resolved_url', 'source_date', 'sha256',
                                                       'size_bytes', 'checksum_algorithm', 'publisher_checksum')})
        downloaded[-1]['extract'] = extract
    # Header timestamps must agree for the entire set before any POI extraction.
    checked, _ = inspect_sources(folder, profile)
    return {'provider': 'Geofabrik', 'storage': 'temporary', 'files': downloaded,
            'transport': 'windows-sspi-negotiate' if windows_proxy else 'requests',
            'tls_revocation': 'best-effort' if windows_proxy else 'default',
            'total_bytes': sum(item['size_bytes'] for item in downloaded),
            'source_date': checked[0]['source_date'], 'replication_timestamp': checked[0]['replication_timestamp'],
            'checksums_verified': True, 'complete_set_verified': True}


def attach_provenance(manifest, download):
    remote = {item['extract']: item for item in download['files']}
    if {item['extract'] for item in manifest['sources']} != set(remote):
        raise ValueError('Processed extract set differs from downloaded set')
    for source in manifest['sources']:
        original = remote[source['extract']]
        if source['sha256'] != original['sha256'] or source['source_date'] != original['source_date']:
            raise ValueError('Processed source differs from verified download')
        source.update({key: original[key] for key in ('source_url', 'resolved_url', 'checksum_algorithm', 'publisher_checksum')})
    manifest['download'] = download
