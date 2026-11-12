import csv
import sqlite3

import pyarrow.parquet as pq
import pytest
from helpers import row

from poi_osm.objects import export_objects
from poi_osm.storage import Registry, check_quality

MAPPING = {"bakery": ["shop"], "hotel": ["tourism"]}
QUALITY = {"allow_empty": False, "max_unassigned_fraction": 0.1,
           "max_count_drop_fraction": 0.3, "max_region_drop_fraction": 0.4}


def snapshot(tmp_path, name, rows, date, previous=None):
    path = tmp_path / f"{name}.sqlite"
    store = Registry(path, MAPPING, date,
                     {"source_date": date, "path": "test.osm.pbf"}, batch_size=2)
    for item in rows:
        store.insert(item)
    store.compare(previous)
    return store, path


def test_compare_statuses_categories_and_reappearance_first_seen(tmp_path):
    first, first_path = snapshot(tmp_path, "first", [row(), row("2"), row("1", tag="hotel")], "2026-01-01")
    assert first.metrics()["changes"] == {"NEW": 3, "UNCHANGED": 0}
    first.close()
    second, second_path = snapshot(tmp_path, "second", [row(), row("2", name="Новое имя"), row("3")],
                                   "2026-02-01", first_path)
    changes = second.metrics()["changes"]
    assert changes == {"NEW": 1, "UPDATED": 1, "DELETED": 1, "UNCHANGED": 1}
    assert second.db.execute("SELECT last_seen_date FROM poi WHERE record_status='DELETED'").fetchone()[0] == "2026-01-01"
    second.close()
    third, third_path = snapshot(tmp_path, "third", [row(), row("2", name="Новое имя"), row("3")],
                                 "2026-03-01", second_path)
    assert third.metrics()["changes"] == {"UNCHANGED": 3}
    assert third.db.execute("SELECT COUNT(*) FROM poi WHERE record_status='DELETED'").fetchone()[0] == 1
    third.close()
    fourth, _ = snapshot(tmp_path, "fourth", [row(), row("2", name="Новое имя"), row("3"), row("1", tag="hotel")],
                          "2026-04-01", third_path)
    assert fourth.metrics()["changes"] == {"NEW": 1, "UNCHANGED": 3}
    restored = fourth.db.execute("SELECT first_seen_date,last_seen_date FROM poi WHERE tag='hotel'").fetchone()
    assert restored == ("2026-01-01", "2026-04-01")
    fourth.close()


def test_technical_duplicates_conflicts_and_typed_osm_ids(tmp_path):
    store = Registry(tmp_path / "registry.sqlite", MAPPING, "2026-01-01",
                     {"source_date": "2026-01-01", "path": "x.osm.pbf"})
    store.insert(row())
    store.insert(row())
    store.insert(row(osm_type="way"))
    assert store.db.execute("SELECT COUNT(*) FROM poi").fetchone()[0] == 2
    with pytest.raises(ValueError, match="Conflicting technical duplicate"):
        store.insert(row(name="Other"))
    store.close()


@pytest.mark.parametrize("change", [
    {"osm_id": ""}, {"osm_type": "area"}, {"lon": float("nan")},
    {"lat": 95}, {"wkt": "POINT (2 1)"}, {"osm_key": "description"},
])
def test_invalid_rows_rejected(tmp_path, change):
    store = Registry(tmp_path / "registry.sqlite", MAPPING, "2026-01-01",
                     {"source_date": "2026-01-01", "path": "x.osm.pbf"})
    with pytest.raises(ValueError):
        store.insert({**row(), **change})
    store.close()


def test_quality_detects_total_and_region_drop_and_unknowns():
    old = {"total": 100, "by_region": {"A": 50, "B": 50}, "unassigned_fraction": 0}
    with pytest.raises(ValueError, match="total count"):
        check_quality({"total": 60, "by_region": {"A": 30, "B": 30}, "unassigned_fraction": 0}, old, QUALITY)
    with pytest.raises(ValueError, match="in A"):
        check_quality({"total": 100, "by_region": {"A": 20, "B": 80}, "unassigned_fraction": 0}, old, QUALITY)
    with pytest.raises(ValueError, match="without an unambiguous region"):
        check_quality({"total": 100, "by_region": {"A": 50, "B": 50}, "unassigned_fraction": 0.11}, old, QUALITY)
    with pytest.raises(ValueError, match="Empty snapshot"):
        check_quality({"total": 0, "by_region": {}, "unassigned_fraction": 0}, None, QUALITY)


def test_exports_csv_unicode_bom_quotes_null_and_parquet(tmp_path):
    first, first_path = snapshot(tmp_path, "first", [row(), row("2", name=None)], "2026-01-01")
    first.close()
    second, _ = snapshot(tmp_path, "second", [row(name="Название; с \"кавычками\"\nи строкой")],
                          "2026-02-01", first_path)
    output = tmp_path / "output"
    output.mkdir()
    export_objects(second.db,output,parquet=True)
    csv_path = output / "poi_teradata.csv"
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        values = list(csv.DictReader(stream, delimiter=";"))
    assert len(values) == 1
    assert values[0]["point_name"] == "Название; с \"кавычками\"\nи строкой"
    assert values[0]["lon"] == "1.0"
    full = pq.read_table(output / "poi_teradata.parquet")
    assert full.num_rows == 1
    assert full.column("osm_id").to_pylist() == ["1"]
    deleted=second.db.execute("SELECT osm_id,point_name FROM poi WHERE record_status='DELETED'").fetchone()
    assert deleted==('2',None)
    second.close()


def test_unassigned_export_keeps_null_country_and_blank_region(tmp_path):
    store,_=snapshot(tmp_path,'quarantine',[row(),{**row('2',code=''),'country':None}],'2026-01-01')
    output=tmp_path/'export'
    export_objects(store.db,output)
    records=pq.read_table(output/'poi_teradata.parquet').to_pylist()
    assert len(records)==2
    unknown=next(record for record in records if record['osm_id']=='2')
    assert unknown['country'] is None and unknown['region_code']==''
    store.close()


def test_old_schema_rejected_before_snapshot_comparison(tmp_path):
    old = tmp_path / 'old.sqlite'
    with sqlite3.connect(old) as db:
        db.execute('CREATE TABLE poi(osm_type TEXT,osm_id TEXT,tag TEXT)')
        db.execute("INSERT INTO poi VALUES('node','1','bakery')")
    store = Registry(tmp_path / 'new.sqlite', MAPPING, '2026-11-11', {'source_date':'2026-11-11','path':'source.pbf'})
    store.insert(row())
    with pytest.raises(ValueError, match='older schema'):
        store.compare(old)
    store.close()
    with sqlite3.connect(old) as db:
        assert db.execute('SELECT COUNT(*) FROM poi').fetchone()[0] == 1


def test_update_date_tracks_last_change_and_deletion_once(tmp_path):
    first, first_path = snapshot(tmp_path, 'date-first', [row(), row('2')], '2026-01-01')
    first.close()
    second, second_path = snapshot(tmp_path, 'date-second', [row(), row('2', name='Новое имя'), row('3')], '2026-02-01', first_path)
    assert second.db.execute('SELECT osm_id,update_date,record_status FROM poi ORDER BY osm_id').fetchall() == [
        ('1', '2026-01-01', 'UNCHANGED'), ('2', '2026-02-01', 'UPDATED'), ('3', '2026-02-01', 'NEW')]
    second.close()
    third, third_path = snapshot(tmp_path, 'date-third', [row(), row('2', name='Новое имя')], '2026-03-01', second_path)
    assert third.db.execute("SELECT update_date,last_seen_date FROM poi WHERE osm_id='1'").fetchone() == ('2026-01-01', '2026-03-01')
    assert third.db.execute("SELECT update_date FROM poi WHERE osm_id='3'").fetchone() == ('2026-03-01',)
    third.close()
    fourth, fourth_path = snapshot(tmp_path, 'date-fourth', [row(), row('2', name='Новое имя')], '2026-04-01', third_path)
    assert fourth.db.execute("SELECT update_date FROM poi WHERE osm_id='3'").fetchone() == ('2026-03-01',)
    fourth.close()
    fifth, _ = snapshot(tmp_path, 'date-fifth', [row(), row('2', name='Новое имя'), row('3')], '2026-05-01', fourth_path)
    assert fifth.db.execute("SELECT update_date,first_seen_date,record_status FROM poi WHERE osm_id='3'").fetchone() == ('2026-05-01', '2026-02-01', 'NEW')
    fifth.close()


def test_removed_category_updates_point_date_and_then_stabilizes(tmp_path):
    from poi_osm.objects import object_rows
    first, first_path = snapshot(tmp_path, 'tags-first', [row(), row('1', tag='hotel')], '2026-01-01')
    first.close()
    second, second_path = snapshot(tmp_path, 'tags-second', [row()], '2026-02-01', first_path)
    exported = list(object_rows(second.db))
    assert len(exported) == 1
    assert exported[0]['update_date'] == '2026-02-01' and exported[0]['record_status'] == 'UPDATED'
    second.close()
    third, _ = snapshot(tmp_path, 'tags-third', [row()], '2026-03-01', second_path)
    exported = list(object_rows(third.db))
    assert exported[0]['update_date'] == '2026-02-01' and exported[0]['record_status'] == 'UNCHANGED'
    third.close()


def test_indirect_telecom_labels_are_clean_in_exports_only(tmp_path):
    store = Registry(tmp_path/'telecom.sqlite', {'electronics':['shop']}, '2026-01-01',
                     {'source_date':'2026-01-01','path':'test.pbf'})
    source = row()
    source.update(tag='electronics', osm_key='shop', osm_value='electronics')
    store.insert(source); store.compare(None)
    original = store.db.execute('SELECT telecom_type,fingerprint FROM poi').fetchone()
    assert original[0] == 'Электроника (косвенная связь)'
    output=tmp_path/'export'; export_objects(store.db, output)
    with (output/'poi_teradata.csv').open(encoding='utf-8-sig', newline='') as stream:
        exported=list(csv.DictReader(stream, delimiter=';'))[0]
    assert exported['telecom_type'] == exported['primary_subcategory'] == 'Электроника'
    parquet=pq.read_table(output/'poi_teradata.parquet').to_pylist()[0]
    assert parquet['telecom_type'] == parquet['primary_subcategory'] == 'Электроника'
    assert store.db.execute('SELECT telecom_type,fingerprint FROM poi').fetchone() == original
    store.close()
