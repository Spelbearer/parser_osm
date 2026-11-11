import hashlib
import json
import sqlite3

import pyarrow.parquet as pq
import pytest
from helpers import csv_rows, full_row, row

from poi_osm.objects import collapse, export_objects, object_rows
from poi_osm.storage import Registry


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
    rows = csv_rows(output/'poi_teradata.csv')
    assert counts['unique_objects'] == 2
    assert {r['osm_uid'] for r in rows} == {'node/1','way/1'}
    table = pq.read_table(output/'poi_teradata.parquet')
    assert table.num_rows == 2
    assert sorted(r['tag_count'] for r in object_rows(store.db)) == [1,2]
    with pytest.raises(FileExistsError):
        export_objects(store.db, output)
    assert len(csv_rows(output/'poi_teradata.csv')) == 2
    store.close()


def test_read_only_registry_export_does_not_modify_source(tmp_path):
    path=tmp_path/'registry.sqlite'
    store=Registry(path,{'bakery':['shop'],'hotel':['tourism']},'2026-11-11',{'source_date':'2026-11-11','path':'input.pbf'})
    store.insert(row());store.insert(row(tag='hotel'));store.compare(None);store.close()
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        report=export_objects(db,tmp_path/'export',parquet=False)
    assert report['unique_objects']==1 and report['category_rows']==2
    assert len(csv_rows(tmp_path/'export/poi_teradata.csv'))==1
    assert not (tmp_path/'export/objects').exists()
    assert not (tmp_path/'export/poi.csv').exists()
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
