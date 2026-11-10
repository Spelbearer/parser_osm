import csv
import json
from pathlib import Path

import osmium
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from poi_osm.classification import CONFIG, Classifier, default_classifier
from poi_osm.extract import extract_pois
from poi_osm.storage import Registry
from test_extract import boundaries
from test_pipeline import project, configure, write_json, invoke, manifest


@pytest.mark.parametrize("tags,tag,sub,evidence", [
    ({"shop":"mobile_phone"}, "mobile_phone", "Салон связи и мобильные телефоны", "osm_tags"),
    ({"shop":"mobile_phone", "mobile_phone:repair":"yes"}, "mobile_phone", "Ремонт телефонов", "osm_tags"),
    ({"shop":"electronics_repair", "service:mobile_phone:repair":"yes"}, "electronics_repair", "Ремонт телефонов", "osm_tags"),
    ({"shop":"repair", "repair":"mobile_phone"}, "mobile_phone", "Ремонт телефонов", "osm_tags"),
    ({"shop":"electronics", "name":"Ремонт смартфонов"}, "electronics", "Ремонт телефонов", "name_hint"),
    ({"office":"telecommunication"}, "telecommunication", "Офис связи", "osm_tags"),
    ({"shop":"electronics", "brand":"МТС"}, "electronics", "Салон связи", "chain_brand"),
])
def test_telecom_categories(tags, tag, sub, evidence):
    row = default_classifier().classify(tags, tag)
    assert row["is_telecom"] is True
    assert row["category"] == "Телефония и связь"
    assert row["subcategory"] == sub
    assert row["telecom_evidence"] == evidence


@pytest.mark.parametrize("name,tag,chain", [
    ('Магазин «Пятёрочка»', 'supermarket', 'pyaterochka'),
    ('Магнит Косметик', 'cosmetics', 'magnit'),
    ('Пункт выдачи OZON', 'outpost', 'ozon'),
    ('ПВЗ Wildberries', 'outpost', 'wildberries'),
    ('WB', 'parcel_locker', 'wildberries'),
    ('М.Видео', 'electronics', 'mvideo'),
    ('МТС', 'mobile_phone', 'mts'),
])
def test_normalized_chain_names(name, tag, chain):
    row = default_classifier().classify({"name":name}, tag)
    assert row["is_chain"] is True
    assert row["chain_id"] == chain
    assert row["chain_evidence"] == "name_hint:name"


@pytest.mark.parametrize("name,tag", [('Пятерочкино','supermarket'), ('Магнитогорск','supermarket'),
                                      ('Магнит','electronics'), ('WB','beauty'), ('Частная булочная','bakery')])
def test_unmatched_names_do_not_prove_independence(name, tag):
    row = default_classifier().classify({"name":name}, tag)
    assert row["chain_status"] == "not_identified"
    assert row["is_chain"] is None


def test_brand_priority_and_conflicts():
    row = default_classifier().classify({"brand":"Ozon", "name":"Wildberries", "operator":"СДЭК"}, 'outpost')
    assert row["chain_id"] == 'ozon'
    assert row["chain_evidence"] == 'brand:brand'
    row = default_classifier().classify({"brand":"Ozon;Wildberries"}, 'outpost')
    assert row["chain_status"] == 'conflict'
    assert row["is_chain"] is None
    assert row["chain_name"] is None


def test_all_configured_tags_have_business_categories():
    mapping = json.loads((CONFIG / 'tag_mapping.json').read_text(encoding='utf-8'))
    classifier = default_classifier()
    assert set(mapping) <= classifier.categories['tags'].keys()
    assert mapping['outpost'] == ['shop']
    assert all(mapping[k] == [] for k in ['commercial','food','retail','coffee'])


def test_real_pbf_classification_csv_parquet_and_original_tags(tmp_path):
    index, _, _ = boundaries(tmp_path)
    mapping = {'outpost':['shop'], 'mobile_phone':['shop','repair'], 'electronics':['shop']}
    pbf = tmp_path / 'shops.osm.pbf'
    with osmium.SimpleWriter(str(pbf)) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1,location=(1,1),tags={'shop':'outpost','brand':'Ozon','name':'ПВЗ'}))
        writer.add_node(osmium.osm.mutable.Node(id=2,location=(2,2),tags={'shop':'mobile_phone','mobile_phone:repair':'yes','name':'Мастер'}))
        writer.add_node(osmium.osm.mutable.Node(id=3,location=(3,3),tags={'shop':'electronics','name':'Частный магазин'}))
    store = Registry(tmp_path / 'registry.sqlite', mapping, '2026-11-11', {'source_date':'2026-11-11','path':pbf})
    extract_pois(pbf, mapping, ['name'], index, store.insert, lambda row:None, tmp_path / 'work')
    store.compare(None)
    output = tmp_path / 'output'
    output.mkdir()
    store.export(output, ['RU-TST'], True)
    with (output / 'full/RU-TST.csv').open(encoding='utf-8-sig',newline='') as stream:
        rows = {row['osm_id']:row for row in csv.DictReader(stream,delimiter=';')}
    assert rows['1']['tag'] == 'outpost'
    assert rows['1']['osm_key'] == 'shop'
    assert rows['1']['chain_name'] == 'Ozon'
    assert rows['1']['is_chain'] == '1'
    assert rows['2']['telecom_type'] == 'Ремонт телефонов'
    assert rows['3']['is_chain'] == ''
    table = pq.read_table(output / 'all_russia.parquet')
    assert table.schema.field('is_telecom').type == pa.bool_()
    assert table.schema.field('is_chain').type == pa.bool_()
    assert {r['osm_id']:r['is_chain'] for r in table.to_pylist()} == {'1':True,'2':None,'3':None}
    store.close()


def test_custom_categories_are_applied_and_contract_change_preserves_previous(project):
    taxonomy = {"tags":{"bakery":{"category":"Моя группа", "subcategory":"Булочные"},
                         "hotel":{"category":"Туризм", "subcategory":"Гостиницы"}}}
    rules = project / 'config/custom_categories.json'
    write_json(rules, taxonomy)
    configure(project, categories='config/custom_categories.json')
    invoke(project)
    original = manifest(project)
    table = pq.read_table(Path(original['output']) / 'all_russia.parquet')
    assert {r['category'] for r in table.to_pylist()} == {'Моя группа','Туризм'}
    taxonomy['tags']['bakery']['category'] = 'Изменённая группа'
    write_json(rules, taxonomy)
    result = invoke(project, succeeds=False)
    assert 'separate project_root' in result.stderr
    assert manifest(project)['run_id'] == original['run_id']


def test_invalid_taxonomy_and_chain_aliases_rejected():
    with pytest.raises(ValueError, match='Category/subcategory'):
        Classifier({'tags':{'bakery':{'category':'Продукты'}}}, {'chains':[]})
    with pytest.raises(ValueError, match='list of strings'):
        Classifier({'tags':{}}, {'chains':[{'id':'x','name':'X','aliases':'wrong'}]})
