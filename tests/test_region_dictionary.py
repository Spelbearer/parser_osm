import csv
import json
import sqlite3
import zipfile
from contextlib import closing

import pyarrow.parquet as pq
import pytest
from helpers import confirm_collection, csv_rows, run_collection, write_json

from poi_osm.classification import CONFIG
from poi_osm.product import execute
from poi_osm.region_dictionary import RegionDictionary


def test_bundled_dictionary_has_all_regions_and_agreed_edw_names():
    registry=json.loads((CONFIG/'regions.json').read_text())
    dictionary=RegionDictionary(CONFIG/'region_dictionary.json',registry)
    assert len(dictionary.records)==83
    assert dictionary.column=='EDW'
    assert dictionary.label('RU-TA','Республика Татарстан')=='Казань'
    assert dictionary.label('RU-KEM','Кемеровская область — Кузбасс')=='Кемерово'
    assert dictionary.label('RU-TY','Республика Тыва')=='Хакасия'
    assert dictionary.label('RU-MOW','Москва')==dictionary.label('RU-MOS','Московская область')=='Москва'
    assert dictionary.label('RU-AD','Республика Адыгея')==dictionary.label('RU-KDA','Краснодарский край')=='Краснодар и Адыгея'
    before=json.dumps(registry,ensure_ascii=False)
    view=dictionary.registry(registry)
    assert next(r for r in view['regions'] if r['region_code']=='RU-TA')['region_name']=='Казань'
    assert json.dumps(registry,ensure_ascii=False)==before


def test_flag_zero_is_not_a_filter_and_unassigned_stays_blank(tmp_path):
    dictionary=RegionDictionary(CONFIG/'region_dictionary.json',json.loads((CONFIG/'regions.json').read_text()))
    path=tmp_path/'input.csv'
    with path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['osm_id','region_code','region_name'],delimiter=';');writer.writeheader()
        writer.writerows([{'osm_id':'1','region_code':'RU-NVS','region_name':'Новосибирская область'},
                          {'osm_id':'2','region_code':'','region_name':''}])
    report=dictionary.apply_csv(path)
    rows=csv_rows(path)
    assert len(rows)==2 and rows[0]['region_name']=='Новосибирск'
    assert rows[1]['region_name']==''
    assert dictionary.records['RU-NVS']['FLAG']==0
    assert report['mapped_objects']==1 and report['unassigned_objects']==1
    assert report['flag_used_as_filter'] is False


def fixture_dictionary(path):
    write_json(path,{'coverage_id':'test-two-regions','target_column':'EDW','regions':[
        {'region_code':'RU-AA','region_name':'Регион А','EDW':'Один филиал','FLAG':0},
        {'region_code':'RU-BB','region_name':'Регион Б','EDW':'Один филиал','FLAG':1}]})


def test_output_mapping_preserves_keys_boundaries_and_update_state(collection):
    confirm_collection(collection)
    original=run_collection(collection,'--production')
    root=collection/'results'
    execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    path=root/'config/region_dictionary.json';fixture_dictionary(path)
    config=json.loads((root/'product.json').read_text());config['region_dictionary']='config/region_dictionary.json';write_json(root/'product.json',config)
    profile_path=root/config['export_profile']
    profile=json.loads(profile_path.read_text());profile['fields'].remove('REGION_NAME_EDW');write_json(profile_path,profile)
    boundary=(root/'boundaries/regions.json').read_bytes()
    with closing(sqlite3.connect(root/'result/.state/registry.sqlite')) as db:
        before=db.execute('SELECT osm_type,osm_id,region_name,fingerprint,first_seen_date FROM poi ORDER BY osm_type,osm_id,tag').fetchall()
    report=execute(root,'files')
    assert report['region_mapping']['mapped_objects']==3
    assert report['region_mapping']['renamed_objects']==3
    rows=csv_rows(root/'result/poi_teradata.csv')
    assert len(rows)==3 and len(rows[0])==19
    assert {r['region_name'] for r in rows}=={'Один филиал'}
    assert {r['REGION_NAME_EDW'] for r in rows}=={'Один филиал'}
    assert {r['REGION_NAME_EDW'] for r in pq.read_table(root/'result/poi_teradata.parquet').to_pylist()}=={'Один филиал'}
    assert {r['region_name'] for r in pq.read_table(root/'result/poi_teradata.parquet').to_pylist()}=={'Один филиал'}
    index=csv_rows(root/'result/analysis/regions_index.csv')
    assert {r['region_code'] for r in index}=={'RU-AA','RU-BB'}
    assert {r['region_name'] for r in index}=={'Один филиал'}
    assert len({r['file'] for r in index})==len(index)
    assert all(row['REGION_NAME_EDW']=='Один филиал' for entry in index for row in csv_rows(root/'result'/entry['file']))
    assert (root/'boundaries/regions.json').read_bytes()==boundary
    with closing(sqlite3.connect(root/'result/.state/registry.sqlite')) as db:
        assert db.execute('SELECT osm_type,osm_id,region_name,fingerprint,first_seen_date FROM poi ORDER BY osm_type,osm_id,tag').fetchall()==before
    assert json.loads((root/'result/.state/last_success.json').read_text())['contract_hash']==original['contract_hash']
    with zipfile.ZipFile(root/'result/poi_layers.zip') as archive:
        assert archive.testzip() is None
        assert any('RU-AA_Один филиал' in name for name in archive.namelist())
        assert any('RU-BB_Один филиал' in name for name in archive.namelist())
        assert all('Один филиал' in entry['region_name'] for entry in json.loads(archive.read('summary.json'))['files'])
    updated=execute(root,'run')
    assert updated['metrics']['changes']=={'UNCHANGED':3}
    assert updated['total_objects']==3


@pytest.mark.parametrize('change',['duplicate','missing','empty'])
def test_invalid_dictionary_is_rejected(tmp_path,change):
    path=tmp_path/'dictionary.json';fixture_dictionary(path)
    document=json.loads(path.read_text())
    if change=='duplicate':document['regions'][1]['region_code']='RU-AA'
    elif change=='missing':document['regions'].pop()
    else:document['regions'][0]['EDW']=' '
    write_json(path,document)
    registry={'coverage_id':'test-two-regions','regions':[{'region_code':'RU-AA'},{'region_code':'RU-BB'}]}
    with pytest.raises(ValueError):RegionDictionary(path,registry)
