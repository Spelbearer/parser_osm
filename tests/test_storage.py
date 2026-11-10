import csv
import sqlite3
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from poi_osm.storage import Registry, check_quality


MAPPING = {"bakery": ["shop"], "hotel": ["tourism"]}
QUALITY = {"allow_empty": False, "max_unassigned_fraction": 0.1,
           "max_count_drop_fraction": 0.3, "max_region_drop_fraction": 0.4}


def row(object_id="1", tag="bakery", name="Булочная; №1", code="RU-TST", osm_type="node"):
    return {"osm_id": object_id, "osm_type": osm_type, "tag": tag, "point_name": name,
            "lon": 1.0, "lat": 2.0, "wkt": "POINT (1 2)", "region_name": "Тест",
            "region_code": code, "region_id": "test", "country": "Россия", "address": "{}",
            "osm_key": MAPPING[tag][0], "osm_value": tag}


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
    second.export(output, ["RU-TST"], parquet=True)
    csv_path = output / "full" / "RU-TST.csv"
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        values = list(csv.DictReader(stream, delimiter=";"))
    assert len(values) == 1
    assert values[0]["point_name"] == "Название; с \"кавычками\"\nи строкой"
    assert values[0]["lon"] == "1.0"
    full = pq.read_table(output / "all_russia.parquet")
    assert full.num_rows == 1
    assert full.column("osm_id").to_pylist() == ["1"]
    with (output / "delta" / "deleted_poi.csv").open(encoding="utf-8-sig", newline="") as stream:
        deleted = list(csv.DictReader(stream, delimiter=";"))
    assert deleted[0]["osm_id"] == "2"
    assert deleted[0]["point_name"] == ""
    second.close()


def test_unassigned_quarantine_not_asserted_as_russian_parquet(tmp_path):
    store, _ = snapshot(tmp_path, "quarantine", [row(), {**row("2", code=""), "country": None}], "2026-01-01")
    output = tmp_path / "output"
    output.mkdir()
    store.export(output, ["RU-TST"], parquet=True)
    assert store.metrics()["total"] == 2
    assert pq.read_table(output / "all_russia.parquet").num_rows == 1
    with (output / "full/unassigned.csv").open(encoding="utf-8-sig", newline="") as stream:
        unassigned = list(csv.DictReader(stream, delimiter=";"))
    assert len(unassigned) == 1
    assert unassigned[0]["country"] == ""
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
