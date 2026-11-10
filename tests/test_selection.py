import json
from pathlib import Path
import subprocess
import sys

import pyarrow.parquet as pq
import pytest

from poi_osm.selection import PrimarySelector, TERADATA_FIELDS
from poi_osm.objects import collapse, export_objects
from poi_osm.storage import Registry
from test_objects import full_row
from test_storage import row
from test_pipeline import project, invoke, manifest, csv_rows, configure, write_json, replace_snapshot


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


def test_warehouse_snapshot_is_unique_and_priority_change_is_upserted_in_same_key(project):
    invoke(project)
    first = manifest(project)
    run = Path(first['output'])
    rows = csv_rows(run/'poi_teradata.csv')
    assert len(rows) == 3
    assert len({(r['osm_type'],r['osm_id']) for r in rows}) == 3
    selected = next(r for r in rows if r['osm_id']=='1')
    assert selected['primary_tag'] == 'bakery'
    assert set(json.loads(selected['all_tags'])) == {'bakery','hotel'}
    assert 'tag' not in selected
    assert selected['is_telecom'] == '0'
    table = pq.read_table(run/'poi_teradata.parquet')
    assert table.num_rows == 3
    assert table.schema.names == TERADATA_FIELDS
    # Export priorities may change without changing raw snapshot/filter state.
    rules = {'telecom_type_priority':[], 'telecom_tag_priority':[], 'other_tag_priority':['hotel','bakery']}
    write_json(project/'config/priorities.json',rules)
    configure(project,primary_selection='config/priorities.json')
    invoke(project)
    second = manifest(project)
    updated = next(r for r in csv_rows(Path(second['output'])/'poi_teradata.csv') if r['osm_id']=='1')
    assert updated['primary_tag'] == 'hotel'
    assert updated['osm_uid'] == selected['osm_uid']
    assert second['metrics']['changes'] == {'UNCHANGED':4}
    assert first['object_export']['primary_rules_sha256'] != second['object_export']['primary_rules_sha256']


def test_warehouse_sql_key_and_merge_do_not_include_primary_tag():
    sql = (Path(__file__).resolve().parents[1]/'docs/teradata.sql').read_text(encoding='utf-8')
    assert 'UNIQUE PRIMARY INDEX ("OSM_TYPE", "OSM_ID")' in sql
    on_clause = sql.split('ON t.')[1].split('WHEN MATCHED')[0]
    assert 'PRIMARY_TAG' not in on_clause


def test_existing_run_conversion_accepts_priority_config_without_reparsing(project):
    invoke(project)
    original = manifest(project)
    run = Path(original['output'])
    custom = project/'priorities.json'
    write_json(custom,{'telecom_type_priority':[], 'telecom_tag_priority':[], 'other_tag_priority':['hotel','bakery']})
    result = subprocess.run([sys.executable,'-m','poi_osm.objects',str(run),'--priority-config',str(custom)],
                            cwd=Path(__file__).resolve().parents[1],capture_output=True,encoding='utf-8',timeout=30)
    assert result.returncode == 0, result.stderr
    output = Path(json.loads(result.stdout)['output'])
    selected = next(r for r in csv_rows(output/'poi_teradata.csv') if r['osm_id']=='1')
    assert selected['primary_tag'] == 'hotel'
    assert set(json.loads(selected['all_tags'])) == {'bakery','hotel'}
    assert manifest(project)['run_id'] == original['run_id']


def test_real_multicategory_telecom_object_has_one_warehouse_row(project):
    write_json(project/'config/tag_mapping.json',{'electronics':['shop'], 'mobile_phone':['repair']})
    replace_snapshot(project,[(1,37,55,{'shop':'electronics','repair':'mobile_phone','brand':'МТС','name':'Мастерская'})])
    invoke(project)
    report = manifest(project)
    output = Path(report['output'])
    rows = csv_rows(output/'poi_teradata.csv')
    assert len(rows) == 1
    record = rows[0]
    assert record['primary_tag'] == 'mobile_phone'
    assert record['primary_subcategory'] == 'Ремонт телефонов'
    assert record['primary_osm_key'] == 'repair'
    assert record['is_telecom'] == '1'
    assert record['chain_id'] == 'mts'
    assert record['is_chain'] == '1'
    assert set(json.loads(record['all_tags'])) == {'electronics','mobile_phone'}
    parquet = pq.read_table(output/'poi_teradata.parquet').to_pylist()
    assert len(parquet) == 1 and parquet[0]['is_telecom'] is True
    assert report['metrics']['category_rows'] == 2
    assert report['metrics']['unique_objects'] == 1


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
    assert {r['osm_type'] for r in csv_rows(output/'poi.csv')} == set(types)
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
