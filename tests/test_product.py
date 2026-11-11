import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

from poi_osm.classification import CONFIG
from poi_osm.kml import build as build_kml
from poi_osm.product import execute,remove_owned,recover_swap,publish
from test_collection import collection,run_collection,part
from test_pipeline import project,configure,write_json


@pytest.fixture
def legacy(collection):
    configure(collection,boundaries_reviewed=True,coverage_confirmed=True)
    profile=json.loads((collection/'profile.json').read_text())
    write_json(collection/'profile.json',{**profile,'complete_snapshot_confirmed':True})
    first=run_collection(collection,'--production')
    root=collection/'results'
    build_kml(first['output'],root/'kml',CONFIG/'kml.json')
    return collection,root,first


def test_adopt_cleans_legacy_and_keeps_latest_files_and_registry(legacy,monkeypatch):
    collection,root,first=legacy
    (root/'personal.txt').write_text('keep this')
    original=(collection/'inputs/alpha-261108.osm.pbf').read_bytes()
    # Existing ZIP is reused, not recalculated.
    monkeypatch.setattr('poi_osm.product.build_kml',lambda *a,**k:pytest.fail('should reuse existing KML ZIP'))
    report=execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    assert report['status']=='PRODUCT_SUCCESS'
    assert report['total_objects']==3
    assert report['cleanup']['skipped']==[]
    assert (root/'result/poi_teradata.csv').is_file()
    assert (root/'result/analysis/audit.json').is_file()
    assert (root/'result/.state/registry.sqlite').is_file()
    assert (root/'result/.state/last_success.json').is_file()
    assert (root/'boundaries/regions.geojson').is_file()
    assert (root/'config/settings.json').is_file()
    assert (root/'personal.txt').read_text()=='keep this'
    assert (collection/'inputs/alpha-261108.osm.pbf').read_bytes()==original
    assert not list(root.rglob('*.kml'))
    for name in ['cache','runs','preview_runs','teradata','kml','state','.work','.swap.json']:
        assert not (root/name).exists(),name
    with zipfile.ZipFile(root/'result/poi_layers.zip') as archive:
        assert archive.testzip() is None
        info=json.loads(archive.read('summary.json'))
        assert info['archive']==str(root/'result/poi_layers.zip')
        assert info['source_run']==str(root/'result/.state/registry.sqlite')
    with sqlite3.connect(root/'result/.state/registry.sqlite') as db:
        assert db.execute('SELECT COUNT(*) FROM poi').fetchone()[0]==3
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='overlap_objects'").fetchone()


def test_next_snapshot_compares_saved_registry_and_leaves_no_work(legacy):
    collection,root,first=legacy
    execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    with sqlite3.connect(root/'result/.state/registry.sqlite') as db:
        db.execute("UPDATE poi SET first_seen_date='2020-01-01'")
    report=execute(root,'run')
    with sqlite3.connect(root/'result/.state/registry.sqlite') as db:
        assert db.execute('SELECT DISTINCT first_seen_date FROM poi').fetchall()==[('2020-01-01',)]
    assert report['status']=='PRODUCT_SUCCESS'
    assert report['metrics']['changes']=={'UNCHANGED':3}
    assert not (root/'.work').exists()
    assert not list(root.rglob('*.kml'))
    assert len(list(root.glob('result')))==1
    state=json.loads((root/'result/.state/last_success.json').read_text())
    assert state['registry']==str(root/'result/.state/registry.sqlite')


def test_render_failure_preserves_previous_result_and_deletes_work(legacy,monkeypatch):
    collection,root,first=legacy
    execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    before=(root/'result/poi_teradata.csv').read_bytes()
    registry=(root/'result/.state/registry.sqlite').read_bytes()
    monkeypatch.setattr('poi_osm.product.export_warehouse',lambda *a,**k:(_ for _ in ()).throw(ValueError('test export failure')))
    with pytest.raises(ValueError,match='test export failure'):execute(root,'files')
    assert (root/'result/poi_teradata.csv').read_bytes()==before
    assert (root/'result/.state/registry.sqlite').read_bytes()==registry
    assert not (root/'.work').exists()
    assert json.loads((root/'logs/last_failure.json').read_text())['status']=='FAILED'


def test_files_mode_replaces_previous_without_pbf(legacy,monkeypatch):
    collection,root,first=legacy
    execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    monkeypatch.setattr('poi_osm.product.run_collection',lambda *a:pytest.fail('must not read PBF'))
    report=execute(root,'files')
    assert report['total_objects']==3
    assert not (root/'.work').exists()
    assert not list(root.rglob('*.kml'))
    assert json.loads((root/'result/analysis/summary.json').read_text())['product_run_id']==report['product_run_id']


def test_cleanup_refuses_external_paths_and_inputs(tmp_path):
    root=tmp_path/'output';root.mkdir()
    other=tmp_path/'input';other.mkdir();(other/'russia.pbf').write_bytes(b'source')
    with pytest.raises(ValueError): remove_owned(other,root)
    nested=root/'files';nested.mkdir();source=nested/'source.pbf';source.write_bytes(b'original')
    with pytest.raises(ValueError): remove_owned(nested,root,[source])
    assert source.read_bytes()==b'original'
    if hasattr(Path,'symlink_to'):
        try:(root/'link').symlink_to(other,target_is_directory=True)
        except OSError:return
        with pytest.raises(ValueError): remove_owned(root/'link',root)
    assert (other/'russia.pbf').read_bytes()==b'source'


def test_publication_recovery_restores_previous_registry(tmp_path):
    root=tmp_path/'output';backup=root/'.work/job/previous-result';backup.mkdir(parents=True)
    (backup/'registry.sqlite').write_bytes(b'preserve')
    write_json(root/'.swap.json',{'run_id':'new','backup':str(backup)})
    recover_swap(root)
    assert (root/'result/registry.sqlite').read_bytes()==b'preserve'
    assert not (root/'.swap.json').exists()


def test_deletions_keep_tombstones_after_all_caches_are_cleaned(legacy):
    collection,root,first=legacy
    execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    part(collection/'inputs/alpha-261108.osm.pbf','RU-AA',0,10,2,include_extra=False)
    report=execute(root,'run')
    assert report['total_objects']==2
    assert report['metrics']['changes']=={'DELETED':1,'UNCHANGED':2}
    with sqlite3.connect(root/'result/.state/registry.sqlite') as db:
        assert db.execute("SELECT record_status FROM poi WHERE osm_id='2'").fetchone()[0]=='DELETED'
    assert not (root/'.work').exists()


def test_unknown_files_inside_legacy_folders_are_preserved(legacy):
    collection,root,first=legacy
    (root/'checks').mkdir()
    (root/'checks/private.json').write_text('important unrelated file')
    report=execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    assert not report['cleanup_complete']
    assert (root/'checks/private.json').read_text()=='important unrelated file'
    assert any('private.json' in row['path'] for row in report['cleanup']['skipped'])


def test_migration_cleans_signed_demo_and_old_preview_in_project(legacy):
    collection,root,first=legacy
    (collection/'demo').mkdir()
    write_json(collection/'demo/settings.json',{'coverage_id':'demo','input':{'path':'example.osm.pbf'}})
    (collection/'demo/example.osm.pbf').write_bytes(b'synthetic fixture')
    preview=collection/'preview/example';preview.mkdir(parents=True)
    write_json(preview/'summary.json',{'status':'PREVIEW_SUCCESS','preview_only':True})
    (preview/'temporary.idx').write_bytes(b'temporary')
    execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    assert not (collection/'demo').exists()
    assert not (collection/'preview').exists()
    assert (collection/'inputs/alpha-261108.osm.pbf').is_file()


def test_migration_refuses_to_run_while_legacy_job_is_locked(legacy):
    from poi_osm.runtime import process_lock
    collection,root,first=legacy
    with process_lock(root/'state/process.lock'):
        with pytest.raises(RuntimeError,match='already running'):
            execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    assert Path(first['registry']).is_file()
    assert not (root/'result').exists()
    assert not (root/'.work').exists()


def test_failed_new_result_rename_rolls_back_previous_registry(tmp_path,monkeypatch):
    root=tmp_path/'output';current=root/'result';current.mkdir(parents=True)
    (current/'registry.sqlite').write_bytes(b'last valid state')
    work=root/'.work/job';bundle=work/'bundle';bundle.mkdir(parents=True)
    (bundle/'registry.sqlite').write_bytes(b'new state')
    original=Path.replace
    def replace(self,target):
        if self==bundle: raise OSError('simulated Windows sharing violation')
        return original(self,target)
    monkeypatch.setattr(Path,'replace',replace)
    with pytest.raises(OSError,match='sharing violation'):
        publish(bundle,root,work,'job')
    assert (current/'registry.sqlite').read_bytes()==b'last valid state'
    assert not (root/'.swap.json').exists()
    assert not (work/'previous-result').exists()


def test_corrupt_old_zip_is_rebuilt_before_legacy_cleanup(legacy):
    collection,root,first=legacy
    archive=Path(json.loads((root/'kml/last_success.json').read_text())['archive'])
    archive.write_bytes(b'corrupt archive')
    report=execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    assert report['status']=='PRODUCT_SUCCESS'
    with zipfile.ZipFile(root/'result/poi_layers.zip') as saved:
        assert saved.testzip() is None
    assert not (root/'kml').exists()
    assert not (root/'.work').exists()
