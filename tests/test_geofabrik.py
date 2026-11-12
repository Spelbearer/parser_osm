import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import osmium
import pytest
import requests
from helpers import confirm_collection, run_collection, write_json
from test_sources import Response, today

from poi_osm.classification import CONFIG
from poi_osm.geofabrik import attach_provenance, download_collection
from poi_osm.product import execute
from poi_osm.sources import SourceError


def routes(monkeypatch, payloads, *, broken=None):
    calls=[]
    def request(self,method,url,**kwargs):
        calls.append(url)
        filename=Path(urlsplit(url).path).name
        checksum=filename.endswith('.md5')
        extract=filename.removesuffix('.md5').removesuffix('-latest.osm.pbf')
        if extract==broken:
            return Response(url,status=503)
        data=payloads[extract]
        return Response(url,(hashlib.md5(data).hexdigest()+'  '+filename.removesuffix('.md5')).encode() if checksum else data)
    monkeypatch.setattr(requests.Session,'request',request)
    monkeypatch.setattr('poi_osm.sources.time.sleep',lambda _:None)
    return calls


def test_eight_verified_downloads_and_consistent_dates(tmp_path,monkeypatch):
    profile=json.loads((CONFIG/'collection.json').read_text());profile['complete_snapshot_confirmed']=True
    payloads={}
    for number,extract in enumerate(profile['expected_extracts'],1):
        header=osmium.io.Header();header.set('osmosis_replication_timestamp',today()+'T00:00:00Z')
        path=tmp_path/(extract+'.pbf')
        with osmium.SimpleWriter(str(path),header=header) as writer:
            writer.add_node(osmium.osm.mutable.Node(id=number,location=(37,55),tags={'name':extract}))
        payloads[extract]=path.read_bytes()
    calls=routes(monkeypatch,payloads)
    folder=tmp_path/'download'
    report=download_collection(profile,folder)
    assert len(report['files'])==8 and len(calls)==16
    assert report['source_date']==today() and report['checksums_verified'] and report['complete_set_verified']
    assert not any('crimean' in url for url in calls)
    assert len(list(folder.glob('*-latest.osm.pbf')))==8
    assert not list(folder.glob('*.part'))
    assert report['total_bytes']==sum(len(data) for data in payloads.values())


def test_wrong_scope_is_rejected_before_network(tmp_path,monkeypatch):
    monkeypatch.setattr(requests.Session,'request',lambda *a,**k:pytest.fail('unexpected network'))
    with pytest.raises(ValueError,match='agreed complete eight-extract'):
        download_collection({'coverage_id':'wrong','expected_extracts':['crimean-fed-district'],
                             'complete_snapshot_confirmed':True},tmp_path/'download')


def test_download_mode_and_files_mode_preserve_remote_provenance_and_remove_pbf(collection,tmp_path,monkeypatch):
    confirm_collection(collection)
    run_collection(collection,'--production')
    root=collection/'results'
    execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    approved=tmp_path/'approved';approved.mkdir()
    profile=json.loads((collection/'profile.json').read_text());write_json(approved/'collection.json',profile)
    monkeypatch.setattr('poi_osm.geofabrik.CONFIG',approved)
    payloads={name:(collection/'inputs'/(name+'-261108.osm.pbf')).read_bytes() for name in ['alpha','beta']}
    saved=dict(payloads)
    calls=routes(monkeypatch,payloads)
    report=execute(root,'download')
    assert report['total_objects']==3
    assert not (root/'.work').exists() and not list(root.rglob('*.osm.pbf'))
    assert all((collection/'inputs'/(name+'-261108.osm.pbf')).read_bytes()==data for name,data in saved.items())
    state=json.loads((root/'result/.state/last_success.json').read_text())
    assert all(source['source_url'].startswith('https://download.geofabrik.de/russia/') for source in state['sources'])
    assert all(source['publisher_md5_checked'] for source in state['sources'])
    assert len(json.loads((root/'result/analysis/downloads.json').read_text())['files'])==2
    before=len(calls)
    execute(root,'files')
    assert len(calls)==before and (root/'result/analysis/downloads.json').is_file()


def test_network_failure_preserves_previous_result_and_cleans_partial_downloads(collection,tmp_path,monkeypatch):
    confirm_collection(collection);run_collection(collection,'--production')
    root=collection/'results';execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    before=(root/'result/.state/registry.sqlite').read_bytes()
    approved=tmp_path/'approved';approved.mkdir();write_json(approved/'collection.json',json.loads((collection/'profile.json').read_text()))
    monkeypatch.setattr('poi_osm.geofabrik.CONFIG',approved)
    payloads={name:(collection/'inputs'/(name+'-261108.osm.pbf')).read_bytes() for name in ['alpha','beta']}
    routes(monkeypatch,payloads,broken='beta')
    with pytest.raises(SourceError,match='No usable source'):
        execute(root,'download')
    assert (root/'result/.state/registry.sqlite').read_bytes()==before
    assert not (root/'.work').exists()


def test_remote_manifest_rejects_processed_checksum_mismatch():
    source={'extract':'central-fed-district','sha256':'other','source_date':today()}
    download={'files':[{'extract':'central-fed-district','sha256':'expected','source_date':today()}]}
    with pytest.raises(ValueError,match='differs from verified download'):
        attach_provenance({'sources':[source]},download)
