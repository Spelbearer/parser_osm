import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pyarrow.parquet as pq
import pytest

from poi_osm.objects import collapse, export_objects
from poi_osm.storage import FIELDS, Registry
from poi_osm.classification import default_classifier
from test_pipeline import project, invoke, manifest, csv_rows
from test_storage import row


def full_row(tag='bakery', **updates):
    value = {k:None for k in FIELDS}
    value.update(row(tag=tag))
    value.update(default_classifier().classify({'name':value['point_name']},tag))
    value.update(update_date='2026-11-11',first_seen_date='2026-11-11',last_seen_date='2026-11-11',record_status='NEW')
    value.update(updates)
    return value


def test_object_collapse_preserves_assignments_dates_and_flags():
    a = full_row(is_telecom=False, record_status='UNCHANGED', first_seen_date='2026-01-01')
    b = full_row('hotel', is_telecom=True, telecom_type='Связь', category='Другая категория')
    merged = collapse([a,b])
    assert merged['osm_uid'] == 'node/1'
    assert merged['tag_count'] == 2
    assert set(merged['tag'].split(' | ')) == {'bakery','hotel'}
    assert merged['is_telecom'] is True
    assert merged['first_seen_date'] == '2026-01-01'
    assert merged['record_status'] == 'UPDATED'
    assignments = json.loads(merged['classifications'])
    assert {r['tag']:r['category'] for r in assignments} == {'bakery':a['category'],'hotel':'Другая категория'}


def test_conflicting_chain_is_not_arbitrarily_selected():
    a = full_row(is_chain=True, chain_id='magnit',chain_name='Магнит',chain_status='identified')
    b = full_row('hotel',is_chain=True,chain_id='ozon',chain_name='Ozon',chain_status='identified')
    merged = collapse([a,b])
    assert merged['chain_status'] == 'conflict'
    assert merged['is_chain'] is None
    assert merged['chain_name'] is None
    assert {r['chain_id'] for r in json.loads(merged['classifications'])} == {'magnit','ozon'}


def test_conflicting_geometry_aborts_instead_of_dropping_data():
    with pytest.raises(ValueError, match='Conflicting object field'):
        collapse([full_row(), full_row('hotel',lon=5)])


def test_numeric_id_collisions_across_osm_types_are_retained(tmp_path):
    store = Registry(tmp_path/'db.sqlite', {'bakery':['shop'],'hotel':['tourism']},'2026-11-11',
                     {'source_date':'2026-11-11','path':'fixture.osm.pbf'},batch_size=1)
    for value in [row(),row(tag='hotel'),row(osm_type='way')]:
        store.insert(value)
    store.compare(None)
    metrics = store.metrics()
    assert metrics['category_rows'] == 3
    assert metrics['unique_objects'] == 2
    output = tmp_path/'objects'
    counts = export_objects(store.db, output,batch_size=1)
    rows = csv_rows(output/'poi.csv')
    assert counts['unique_objects'] == 2
    assert {r['osm_uid'] for r in rows} == {'node/1','way/1'}
    table = pq.read_table(output/'poi_objects.parquet')
    assert table.num_rows == 2
    assert sorted(r['tag_count'] for r in table.to_pylist()) == [1,2]
    with pytest.raises(FileExistsError):
        export_objects(store.db, output)
    assert len(csv_rows(output/'poi.csv')) == 2
    store.close()


def test_primary_export_and_read_only_existing_run_conversion(project):
    invoke(project)
    run = Path(manifest(project)['output'])
    unique = csv_rows(run/'poi.csv')
    assert len(unique) == 3
    assert len({r['osm_uid'] for r in unique}) == 3
    assert sum(len(csv_rows(p)) for p in (run/'full').glob('*.csv')) == 4
    assert sum(len(csv_rows(p)) for p in (run/'objects').glob('*.csv')) == 3
    source = run/'registry.sqlite'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    command = [sys.executable,'-m','poi_osm.objects',str(run)]
    result = subprocess.run(command,cwd=Path(__file__).resolve().parents[1],capture_output=True,encoding='utf-8',timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['unique_objects'] == 3
    assert len(csv_rows(Path(report['output'])/'poi.csv')) == 3
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert manifest(project)['run_id'] == run.name


def test_failed_registry_is_not_exported_as_complete(project):
    folder = project/'failed'
    folder.mkdir()
    (folder/'summary.json').write_text(json.dumps({'status':'FAILED'}),encoding='utf-8')
    with sqlite3.connect(folder/'preview.sqlite') as db:
        db.execute('CREATE TABLE poi(osm_id TEXT)')
    result = subprocess.run([sys.executable,'-m','poi_osm.objects',str(folder)],
                            cwd=Path(__file__).resolve().parents[1],capture_output=True,encoding='utf-8',timeout=30)
    assert result.returncode != 0
    assert 'did not complete' in result.stderr
    assert not list(folder.glob('unique-*'))
