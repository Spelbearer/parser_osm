"""End-to-end CLI regression tests on real, complete synthetic PBF snapshots.

Fixtures describe two fictional regions, not production Russian boundaries.
Every invocation runs the actual command in a separate Python process.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import osmium
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from shapely import from_wkt

from poi_osm.runtime import process_lock

ENTRYPOINT = Path(__file__).resolve().parents[1] / "main.py"


def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def current_date():
    return datetime.now(timezone.utc).date().isoformat()


def snapshot(path: Path, nodes, source_date=None):
    path.unlink(missing_ok=True)
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", (source_date or current_date()) + "T00:00:00Z")
    with osmium.SimpleWriter(str(path), header=header) as writer:
        for node_id, lon, lat, tags in nodes:
            writer.add_node(osmium.osm.mutable.Node(id=node_id, location=(lon, lat), tags=tags))


INITIAL = [
    (1, 37.0, 55.0, {"shop": "bakery", "tourism": "hotel", "name": 'Пекарня; "Уют"\nВход № 1', "name:ru": "Русское имя"}),
    (2, 39.0, 55.0, {"shop": "bakery"}),
    (3, 37.5, 55.0, {"shop": "bakery", "brand": "Бренд"}),
]

UPDATED = [
    (1, 37.01, 55.01, {"shop": "bakery", "tourism": "hotel", "name": "Обновлённая пекарня"}),
    (2, 39.0, 55.0, {"shop": "bakery"}),
    (4, 37.5, 55.0, {"shop": "bakery", "operator": "Новый магазин"}),
]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "poi"
    (root / "config").mkdir(parents=True)
    (root / "boundaries").mkdir()
    write_json(root / "config/tag_mapping.json", {"bakery": ["shop"], "hotel": ["tourism"]})
    regions = [
        {"region_code": "TEST-A", "region_name": "Тестовый регион А", "region_id": "a"},
        {"region_code": "TEST-B", "region_name": "Тестовый регион Б", "region_id": "b"},
    ]
    write_json(root / "config/regions.json", {"source": "Synthetic regression fixture", "coverage_id": "test-two-regions", "regions": regions})
    features = []
    for region, west, east in [(regions[0], 36, 38), (regions[1], 38, 40)]:
        features.append({"type": "Feature", "properties": {"region_code": region["region_code"]}, "geometry": {
            "type": "Polygon", "coordinates": [[[west, 54], [east, 54], [east, 56], [west, 56], [west, 54]]],
        }})
    write_json(root / "boundaries/regions.geojson", {"type": "FeatureCollection", "source": "Synthetic regression fixture",
                                                    "coverage_id": "test-two-regions", "features": features})
    settings = {
        "project_root": "..", "coverage_id": "test-two-regions", "coverage_confirmed": True,
        "boundaries_reviewed": True, "input": {"path": "fixture.osm.pbf", "coverage_id": "test-two-regions", "complete": True},
        "quality": {"max_count_drop_fraction": 0.99, "max_region_drop_fraction": 0.99, "max_unassigned_fraction": 0.01},
        "batch_size": 2, "write_parquet": True,
    }
    snapshot(root / "fixture.osm.pbf", INITIAL)
    settings["input"]["sha256"] = hashlib.sha256((root / "fixture.osm.pbf").read_bytes()).hexdigest()
    write_json(root / "config/settings.json", settings)
    return root


def configure(root, **updates):
    path = root / "config/settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings.update(updates)
    write_json(path, settings)


def replace_snapshot(root, nodes, source_date=None):
    path = root / "fixture.osm.pbf"
    snapshot(path, nodes, source_date)
    settings = json.loads((root / "config/settings.json").read_text(encoding="utf-8"))
    settings["input"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    configure(root, input=settings["input"])


def invoke(root, *args, succeeds=True):
    result = subprocess.run([sys.executable, str(ENTRYPOINT), "--config", str(root / "config/settings.json"), *args],
                            cwd=root.parent, text=True, encoding="utf-8", capture_output=True, timeout=30)
    if succeeds:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, "Failure case unexpectedly published data"
    return result


def manifest(root, region=None):
    path = root / "state" / "last_success.json" if not region else root / "state/scopes" / region / "last_success.json"
    return json.loads(path.read_text(encoding="utf-8"))


def csv_rows(path):
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream, delimiter=";"))


def test_snapshot_lifecycle_csv_parquet_and_tombstones(project):
    invoke(project)
    first = manifest(project)
    assert first["metrics"]["total"] == 4
    assert first["metrics"]["changes"] == {"NEW": 4, "UNCHANGED": 0}
    assert first["metrics"]["nameless_fraction"] == 0.25
    assert first["metrics"]["unassigned_fraction"] == 0
    output = Path(first["output"])
    rows = csv_rows(output / "full/TEST-A.csv") + csv_rows(output / "full/TEST-B.csv")
    assert len({(r["osm_type"], r["osm_id"], r["tag"]) for r in rows}) == 4
    assert {r["tag"] for r in rows if r["osm_id"] == "1"} == {"bakery", "hotel"}
    assert all(r["point_name"] == INITIAL[0][3]["name"] for r in rows if r["osm_id"] == "1")
    assert next(r for r in rows if r["osm_id"] == "2")["point_name"] == ""
    assert next(r for r in rows if r["osm_id"] == "3")["point_name"] == "Бренд"
    for row in rows:
        point = from_wkt(row["wkt"])
        assert point.x == pytest.approx(float(row["lon"]))
        assert point.y == pytest.approx(float(row["lat"]))
    table = pq.read_table(output / "all_russia.parquet")
    assert table.num_rows == 4
    assert all(table.schema.field(field).type == pa.date32() for field in
               ["update_date", "source_date", "first_seen_date", "last_seen_date"])
    assert sum(row["point_name"] is None for row in table.to_pylist()) == 1
    assert pq.read_table(output / "all_russia.parquet", filters=[("region_code", "=", "TEST-B")]).num_rows == 1

    invoke(project, "--mode", "incremental")
    second = manifest(project)
    assert second["run_id"] != first["run_id"]
    assert second["metrics"]["changes"] == {"UNCHANGED": 4}
    assert len(csv_rows(Path(second["output"]) / "delta/new_poi.csv")) == 0

    replace_snapshot(project, UPDATED)
    invoke(project)
    third = manifest(project)
    assert third["metrics"]["changes"] == {"NEW": 1, "UPDATED": 2, "DELETED": 1, "UNCHANGED": 1}
    assert third["metrics"]["total"] == 4
    output = Path(third["output"])
    deleted = csv_rows(output / "delta/deleted_poi.csv")
    assert [(r["osm_type"], r["osm_id"], r["tag"], r["record_status"]) for r in deleted] == [("node", "3", "bakery", "DELETED")]
    assert csv_rows(output / "delta/new_poi.csv")[0]["osm_id"] == "4"
    assert len(csv_rows(output / "delta/updated_poi.csv")) == 2
    with sqlite3.connect(third["registry"]) as database:
        assert database.execute("SELECT COUNT(*) FROM poi").fetchone()[0] == 5
        assert database.execute("SELECT record_status FROM poi WHERE osm_id='3'").fetchone()[0] == "DELETED"
    assert pq.read_table(output / "all_russia.parquet").num_rows == 4
    # Previously published directories stay usable after pointer advancement.
    assert Path(first["registry"]).is_file()
    assert pq.read_table(Path(first["output"]) / "all_russia.parquet").num_rows == 4

    invoke(project)
    fourth = manifest(project)
    assert fourth["metrics"]["changes"] == {"UNCHANGED": 4}
    assert csv_rows(Path(fourth["output"]) / "delta/deleted_poi.csv") == []


@pytest.mark.parametrize("failure", ["missing", "sha256", "invalid_header", "stale", "older", "incomplete"])
def test_input_failure_retains_previous_success(project, failure):
    invoke(project)
    pointer = project / "state/last_success.json"
    before = pointer.read_bytes()
    previous = manifest(project)
    settings = json.loads((project / "config/settings.json").read_text(encoding="utf-8"))
    source = project / "fixture.osm.pbf"
    if failure == "missing":
        source.unlink()
    elif failure == "sha256":
        source.write_bytes(source.read_bytes() + b"corruption")
    elif failure == "invalid_header":
        source.write_bytes(b"invalid PBF")
        settings["input"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        configure(project, input=settings["input"])
    elif failure in {"stale", "older"}:
        date = (datetime.now(timezone.utc).date() - timedelta(days=90 if failure == "stale" else 1)).isoformat()
        replace_snapshot(project, INITIAL, date)
    elif failure == "incomplete":
        settings["input"]["complete"] = False
        configure(project, input=settings["input"])
    invoke(project, succeeds=False)
    assert pointer.read_bytes() == before
    assert Path(previous["registry"]).is_file()
    assert pq.read_table(Path(previous["output"]) / "all_russia.parquet").num_rows == 4
    assert any((project / "work/temporary").iterdir()), "failed work retained"


def test_anomalous_drop_preserves_previous_success(project):
    invoke(project)
    pointer = project / "state/last_success.json"
    before = pointer.read_bytes()
    configure(project, quality={"max_count_drop_fraction": 0.25, "max_region_drop_fraction": 0.25, "max_unassigned_fraction": 0.01})
    replace_snapshot(project, [INITIAL[0]])
    result = invoke(project, succeeds=False)
    assert "Anomalous" in result.stderr
    assert pointer.read_bytes() == before


def test_changed_filter_contract_rejected(project):
    invoke(project)
    pointer = project / "state/last_success.json"
    before = pointer.read_bytes()
    write_json(project / "config/tag_mapping.json", {"bakery": ["shop"]})
    result = invoke(project, succeeds=False)
    assert "changed" in result.stderr
    assert pointer.read_bytes() == before


def test_region_scoped_runs_do_not_delete_other_regions(project):
    invoke(project)
    full_pointer = (project / "state/last_success.json").read_bytes()
    invoke(project, "--region", "Тестовый регион А")
    selected = manifest(project, "TEST-A")
    assert selected["scope"] == "TEST-A"
    assert selected["metrics"]["total"] == 3
    assert selected["metrics"]["by_region"] == {"TEST-A": 3}
    assert (project / "state/last_success.json").read_bytes() == full_pointer
    output = Path(selected["output"])
    assert (output / "full/TEST-A.csv").exists()
    assert not (output / "full/TEST-B.csv").exists()
    invoke(project, "--region", "a")
    assert manifest(project, "TEST-A")["metrics"]["changes"] == {"UNCHANGED": 3}
    assert (project / "state/last_success.json").read_bytes() == full_pointer


def test_check_creates_no_files_and_leaves_state_unchanged(project):
    def contents():
        return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in project.rglob("*") if path.is_file()}
    before = contents()
    result = invoke(project, "--check")
    assert json.loads(result.stdout)["last_success"] is None
    assert contents() == before
    assert not (project / "state").exists()
    invoke(project)
    before = contents()
    result = invoke(project, "--check")
    assert json.loads(result.stdout)["last_success"]["status"] == "SUCCESS"
    assert contents() == before


def test_concurrent_cli_cannot_publish_while_lock_held(project):
    invoke(project)
    pointer = project / "state/last_success.json"
    before = pointer.read_bytes()
    with process_lock(project / "state/process.lock"):
        result = invoke(project, succeeds=False)
    assert "already running" in result.stderr
    assert pointer.read_bytes() == before
    # The kernel lock is released when its owning context closes.
    invoke(project)
    assert pointer.read_bytes() != before


def test_unassigned_objects_are_reviewed_and_block_publication(project):
    invoke(project)
    pointer = project / "state/last_success.json"
    before = pointer.read_bytes()
    replace_snapshot(project, INITIAL + [(10, 80, 70, {"shop": "bakery"})])
    result = invoke(project, succeeds=False)
    assert "unambiguous region" in result.stderr
    assert pointer.read_bytes() == before
    reviews = list((project / "work/temporary").glob("*/review.csv"))
    assert any(any(row["osm_id"] == "10" for row in csv_rows(path)) for path in reviews)


def test_region_scope_previously_known_poi_cannot_be_deleted_if_region_is_unresolved(project):
    invoke(project, "--region", "TEST-A")
    pointer = project / "state/scopes/TEST-A/last_success.json"
    before = pointer.read_bytes()
    moved = [(ident, 80 if ident == 1 else lon, 70 if ident == 1 else lat, tags)
             for ident, lon, lat, tags in INITIAL]
    replace_snapshot(project, moved)
    result = invoke(project, "--region", "TEST-A", succeeds=False)
    assert "deletion is unsafe" in result.stderr
    assert pointer.read_bytes() == before


def test_duplicate_candidates_are_reported_without_removal_or_false_address_matches(project):
    nodes = INITIAL + [
        (11, 37.0001, 55.0001, {"shop": "bakery", "name": INITIAL[0][3]["name"]}),
        (12, 37.2, 55.0, {"shop": "bakery", "name": "Адрес", "addr:street": "Тестовая", "addr:housenumber": "1"}),
        (13, 37.2001, 55.0001, {"shop": "bakery", "name": "Адрес", "addr:street": "Тестовая", "addr:housenumber": "1"}),
    ]
    replace_snapshot(project, nodes)
    invoke(project, "--duplicates")
    result = manifest(project)
    assert result["metrics"]["total"] == 7
    output = Path(result["output"])
    candidates = csv_rows(output / "duplicate_candidates.csv")
    assert len(candidates) == 2
    assert all(candidate["decision"] == "REVIEW" for candidate in candidates)
    assert all(float(candidate["distance_m"]) <= 30 for candidate in candidates)
    assert next(c for c in candidates if c["object_a"] == "node/1")["address_matches"] == "False"
    assert next(c for c in candidates if c["object_a"] == "node/12")["address_matches"] == "True"
    assert pq.read_table(output / "all_russia.parquet").num_rows == 7
