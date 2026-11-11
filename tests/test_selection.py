import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from helpers import configure, csv_rows, full_row, part, row, run_collection, write_json

from poi_osm.objects import collapse, export_objects, object_rows
from poi_osm.selection import PrimarySelector
from poi_osm.storage import Registry


def assignment(tag, telecom=False, subtype=None):
    return full_row() | {'tag':tag, 'osm_key':'shop','osm_value':tag,
           'is_telecom':telecom,'category':'Телефония и связь' if telecom else 'Прочее',
           'subcategory':subtype or tag,'telecom_type':subtype,'telecom_evidence':'osm_tags' if telecom else None}


@pytest.mark.parametrize('rows,expected', [
    ([assignment('electronics',True), assignment('mobile_phone',True)], 'mobile_phone'),
    ([assignment('repair',True), assignment('mobile_phone_repair',True,'Ремонт телефонов')], 'mobile_phone_repair'),
    ([assignment('computer',True), assignment('electronics_repair',True)], 'electronics_repair'),
    ([assignment('supermarket'), assignment('mobile_phone',True)], 'mobile_phone'),
    ([assignment('electronics',True,'Ремонт телефонов'), assignment('mobile_phone',True,'Салон связи')], 'electronics'),
    ([assignment('hotel'), assignment('supermarket')], 'supermarket'),
    ([assignment('zzz'), assignment('aaa')], 'aaa'),
])
def test_primary_selection_is_stable_and_preserves_all_assignments(rows,expected):
    selector = PrimarySelector()
    for order in [rows,list(reversed(rows))]:
        result = selector.enrich(collapse(order))
        assert result['primary_tag'] == expected
        assert result['primary_subcategory'] == next(r['subcategory'] for r in rows if r['tag']==expected)
        assert set(json.loads(result['all_tags'])) == {r['tag'] for r in rows}
        assert len(json.loads(result['classifications'])) == len(rows)


def test_unknown_telecom_still_beats_known_nontelecom_and_custom_priority():
    selector = PrimarySelector({'telecom_type_priority':[], 'telecom_tag_priority':[], 'other_tag_priority':['supermarket']})
    result = selector.enrich(collapse([assignment('supermarket'),assignment('new_telecom',True)]))
    assert result['primary_tag'] == 'new_telecom'
    selector = PrimarySelector({'telecom_type_priority':[], 'telecom_tag_priority':['computer','electronics'], 'other_tag_priority':[]})
    result = selector.enrich(collapse([assignment('computer',True),assignment('electronics',True)]))
    assert result['primary_tag'] == 'computer'


def test_duplicate_priority_entries_rejected():
    with pytest.raises(ValueError,match='unique'):
        PrimarySelector({'telecom_type_priority':[], 'telecom_tag_priority':['repair','repair'], 'other_tag_priority':[]})


def test_warehouse_snapshot_priority_change_keeps_the_same_key(collection):
    for stem,code,west,east,extra in [('alpha','RU-AA',0,10,2),('beta','RU-BB',10,20,3)]:
        part(collection/f'inputs/{stem}-261108.osm.pbf',code,west,east,extra,
             shared_tags={'shop':'bakery','tourism':'hotel','name':'Общий'})
    profile=json.loads((collection/'profile.json').read_text());profile['complete_snapshot_confirmed']=True
    write_json(collection/'profile.json',profile)
    first=run_collection(collection,'--production')
    selected=next(r for r in csv_rows(Path(first['output'])/'poi_teradata.csv') if r['osm_id']=='1')
    assert selected['primary_tag']=='bakery'
    assert set(json.loads(selected['all_tags']))=={'bakery','hotel'}
    write_json(collection/'config/priorities.json',{'telecom_type_priority':[],'telecom_tag_priority':[], 'other_tag_priority':['hotel','bakery']})
    configure(collection,primary_selection='config/priorities.json')
    second=run_collection(collection,'--production')
    updated=next(r for r in csv_rows(Path(second['output'])/'poi_teradata.csv') if r['osm_id']=='1')
    assert updated['primary_tag']=='hotel' and updated['osm_uid']==selected['osm_uid']
    assert second['metrics']['changes']=={'UNCHANGED':4}
    assert second['cached_extracts']==2
    assert first['object_export']['primary_rules_sha256']!=second['object_export']['primary_rules_sha256']


def test_warehouse_sql_key_and_merge_do_not_include_primary_tag():
    sql = (Path(__file__).resolve().parents[1]/'docs/teradata_compact.sql').read_text(encoding='utf-8')
    assert 'UNIQUE PRIMARY INDEX ("OSM_TYPE", "OSM_ID")' in sql
    on_clause = sql.split('ON t.')[1].split('WHEN MATCHED')[0]
    assert 'PRIMARY_TAG' not in on_clause


def test_real_multicategory_telecom_object_has_one_warehouse_row(tmp_path):
    from poi_osm.classification import default_classifier
    tags={'shop':'electronics','repair':'mobile_phone','brand':'МТС','name':'Мастерская'}
    store=Registry(tmp_path/'registry.sqlite',{'electronics':['shop'],'mobile_phone':['repair']},'2026-11-11',
                   {'source_date':'2026-11-11','path':'fixture.pbf'})
    for tag,key in [('electronics','shop'),('mobile_phone','repair')]:
        value=full_row()
        value.update(tag=tag,osm_key=key,osm_value=tag,point_name=tags['name'])
        value.update(default_classifier().classify(tags,tag))
        store.insert(value)
    store.compare(None)
    output=tmp_path/'export';report=export_objects(store.db,output)
    records=csv_rows(output/'poi_teradata.csv');assert len(records)==1
    record=records[0]
    assert record['primary_tag']=='mobile_phone'
    assert record['primary_subcategory']=='Ремонт телефонов'
    assert record['primary_osm_key']=='repair' and record['is_telecom']=='1'
    assert record['chain_id']=='mts' and record['is_chain']=='1'
    assert set(json.loads(record['all_tags']))=={'electronics','mobile_phone'}
    assert report['category_rows']==2 and report['unique_objects']==1
    assert pq.read_table(output/'poi_teradata.parquet').to_pylist()[0]['is_telecom'] is True
    store.close()


@pytest.mark.parametrize('only_relations', [False, True])
def test_relations_are_excluded_only_from_teradata_export(tmp_path, only_relations):
    store = Registry(tmp_path/'registry.sqlite', {'bakery':['shop']},'2026-11-11',
                     {'source_date':'2026-11-11','path':'fixture.osm.pbf'})
    types = ['relation'] if only_relations else ['node','way','relation']
    for typ in types:
        store.insert(row(osm_type=typ))
    store.compare(None)
    output = tmp_path/'export'
    report = export_objects(store.db,output,batch_size=1)
    assert {r['osm_type'] for r in object_rows(store.db)} == set(types)
    assert report['unique_objects'] == len(types)
    assert report['teradata_objects'] == len(types)-1
    assert report['teradata_excluded_by_type'] == {'relation':1}
    expected = set(types)-{'relation'}
    assert {r['osm_type'] for r in csv_rows(output/'poi_teradata.csv')} == expected
    assert {r['osm_type'] for r in pq.read_table(output/'poi_teradata.parquet').to_pylist()} == expected
    assert store.db.execute("SELECT COUNT(*) FROM poi WHERE osm_type='relation'").fetchone()[0] == 1
    # Explicitly restoring relation is possible without re-parsing or deleting
    # source data; defaults remain node+way.
    rules = dict(PrimarySelector().rules, teradata_osm_types=['node','way','relation'])
    restored = tmp_path/'restored'
    export_objects(store.db,restored,selection_rules=rules)
    assert {r['osm_type'] for r in csv_rows(restored/'poi_teradata.csv')} == set(types)
    store.close()


@pytest.mark.parametrize('types',[[],['rel'],['node','node'],[{}]])
def test_invalid_warehouse_type_filter_is_rejected(types):
    rules = dict(PrimarySelector().rules,teradata_osm_types=types)
    with pytest.raises(ValueError,match='teradata_osm_types'):
        PrimarySelector(rules)
