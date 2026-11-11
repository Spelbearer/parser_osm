import json

import osmium
import pytest
from helpers import boundaries
from shapely.geometry import Point, Polygon, mapping

from poi_osm.extract import extract_pois, matching_categories
from poi_osm.geography import RegionIndex, prepare_boundaries


def write_pbf(path, broken=False):
    with osmium.SimpleWriter(str(path)) as writer:
        for node_id, coordinate in enumerate([(1, 1), (3, 1), (3, 3), (1, 3), (5, 5), (7, 5), (7, 7), (5, 7)], 1):
            writer.add_node(osmium.osm.mutable.Node(id=node_id, location=coordinate,
                tags={"shop": "bakery", "name": "Булочная; №1"} if node_id == 1 else {}))
        writer.add_way(osmium.osm.mutable.Way(id=1, nodes=[1, 2, 3, 4, 1],
            tags={"shop": "bakery", "name:ru": "Площадь"}))
        writer.add_way(osmium.osm.mutable.Way(id=2, nodes=[5, 6, 7, 8, 5]))
        writer.add_way(osmium.osm.mutable.Way(id=3, nodes=[1, 2], tags={"shop": "bakery", "area": "no"}))
        writer.add_relation(osmium.osm.mutable.Relation(id=1,
            members=[("w", 999 if broken else 2, "outer")], tags={"type": "multipolygon", "shop": "bakery"}))
        writer.add_relation(osmium.osm.mutable.Relation(id=2,
            members=[("w", 3, "")], tags={"type": "route", "shop": "bakery"}))


def test_exact_key_mapping():
    assert list(matching_categories({"description": "bakery", "shop": "clothes"}, {"bakery": ["shop"]})) == []
    assert list(matching_categories({"shop": "bakery", "craft": "bakery"}, {"bakery": ["shop", "craft"]})) == [("bakery", "shop")]


def test_nodes_ways_relations_and_disk_cache(tmp_path):
    index, _, _ = boundaries(tmp_path)
    pbf = tmp_path / "sample.osm.pbf"
    write_pbf(pbf)
    rows, reviews = [], []
    stats = extract_pois(pbf, {"bakery": ["shop"]}, ["name", "name:ru", "brand"],
                         index, rows.append, reviews.append, tmp_path / "work")
    keys = {(r["osm_type"], r["osm_id"], r["tag"]) for r in rows}
    assert keys == {("node", "1", "bakery"), ("way", "1", "bakery"), ("way", "3", "bakery"),
                    ("relation", "1", "bakery"), ("relation", "2", "bakery")}
    assert len(rows) == len(keys) == stats["poi_records"] == 5
    assert stats["processed_osm_objects"] == 13
    assert reviews == []
    area = next(r for r in rows if (r["osm_type"], r["osm_id"]) == ("relation", "1"))
    assert Polygon([(5, 5), (7, 5), (7, 7), (5, 7)]).contains(Point(area["lon"], area["lat"]))
    assert area["point_name"] is None
    line = next(r for r in rows if (r["osm_type"], r["osm_id"]) == ("way", "3"))
    assert (line["lon"], line["lat"]) == (2, 1)
    assert not list((tmp_path / "work").iterdir())


def test_incomplete_relation_aborts_snapshot(tmp_path):
    index, _, _ = boundaries(tmp_path)
    pbf = tmp_path / "broken.osm.pbf"
    write_pbf(pbf, broken=True)
    with pytest.raises((ValueError, RuntimeError), match="Incomplete|location|Missing"):
        extract_pois(pbf, {"bakery": ["shop"]}, ["name"], index, lambda r: None,
                     lambda r: None, tmp_path / "work")


def test_unassembled_way_is_only_quarantined_for_explicit_preview(tmp_path):
    index, _, _ = boundaries(tmp_path)
    pbf = tmp_path / "degenerate.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=(1,1), tags={"shop":"bakery"}))
        writer.add_node(osmium.osm.mutable.Node(id=2, location=(2,2)))
        # Closed but only two distinct vertices: no valid area can be built.
        writer.add_way(osmium.osm.mutable.Way(id=17, nodes=[1,2,1,2,1], tags={"shop":"bakery"}))
    with pytest.raises((ValueError, RuntimeError), match="unassembled|invalid area"):
        extract_pois(pbf, {"bakery":["shop"]}, ["name"], index, lambda row:None,
                     lambda row:None, tmp_path / "strict")
    rows, reviews = [], []
    stats = extract_pois(pbf, {"bakery":["shop"]}, ["name"], index, rows.append,
                         reviews.append, tmp_path / "preview", strict_geometry=False)
    assert [(r["osm_type"],r["osm_id"]) for r in rows] == [("node","1")]
    assert stats["unresolved_objects"] == 1
    assert stats["skipped_poi_records"] == 1
    assert reviews[0]["osm_id"] == "17"
    assert reviews[0]["review_reason"] == "unassembled_geometry"
    assert json.loads(reviews[0]["detail"])["node_count"] == 5


def test_unknown_region_is_retained_for_review(tmp_path):
    index, _, _ = boundaries(tmp_path)
    pbf = tmp_path / "outside.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=(20, 20), tags={"shop": "bakery"}))
    rows, reviews = [], []
    stats = extract_pois(pbf, {"bakery": ["shop"]}, ["name"], index, rows.append, reviews.append, tmp_path / "work")
    assert rows[0]["region_code"] == ""
    assert rows[0]["country"] is None
    assert reviews[0]["review_reason"] == "outside_configured_regions"
    assert stats["unknown_region"] == 1


def test_boundary_registry_completeness_and_overlaps(tmp_path):
    index, geojson, registry = boundaries(tmp_path)
    assert index.lookup(1, 1)["region_code"] == "RU-TST"
    assert index.lookup(20, 20) is None
    document = json.loads(registry.read_text())
    document["regions"].append({"region_code": "RU-MISSING", "region_name": "Missing"})
    registry.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="Missing boundary"):
        RegionIndex(geojson, registry)
    document = json.loads(geojson.read_text())
    document["features"].append({**document["features"][0], "properties": {"region_code": "RU-MISSING"}})
    geojson.write_text(json.dumps(document))
    assert RegionIndex(geojson, registry).lookup(1, 1) is None


def test_boundary_candidates_require_review(tmp_path):
    pbf = tmp_path / "admin.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as writer:
        for n, point in enumerate([(1, 1), (3, 1), (3, 3), (1, 3)], 1):
            writer.add_node(osmium.osm.mutable.Node(id=n, location=point))
        writer.add_way(osmium.osm.mutable.Way(id=1, nodes=[1, 2, 3, 4, 1], tags={
            "boundary": "administrative", "admin_level": "4", "ISO3166-2": "RU-TST", "name": "Тест"}))
    output = tmp_path / "candidate.geojson"
    assert prepare_boundaries(pbf, output, tmp_path / "work")["candidate_regions"] == 1
    document = json.loads(output.read_text())
    assert document["review_required"] is True
    assert "coverage_id" not in document


def test_concave_area_point_inside_and_multiple_categories(tmp_path):
    index, _, _ = boundaries(tmp_path)
    pbf = tmp_path / "concave.osm.pbf"
    points = [(1, 1), (5, 1), (5, 2), (2, 2), (2, 5), (1, 5)]
    polygon = Polygon(points)
    assert not polygon.contains(polygon.centroid)
    with osmium.SimpleWriter(str(pbf)) as writer:
        for n, point in enumerate(points, 1):
            writer.add_node(osmium.osm.mutable.Node(id=n, location=point))
        writer.add_way(osmium.osm.mutable.Way(id=4, nodes=[1, 2, 3, 4, 5, 6, 1], tags={
            "shop": "bakery", "tourism": "hotel", "name": "Original", "name:ru": "Русское"}))
    rows = []
    extract_pois(pbf, {"bakery": ["shop"], "hotel": ["tourism"]}, ["name:ru", "name"],
                 index, rows.append, lambda r: None, tmp_path / "work")
    assert {r["tag"] for r in rows} == {"bakery", "hotel"}
    assert all(r["point_name"] == "Русское" for r in rows)
    assert all(polygon.contains(Point(r["lon"], r["lat"])) for r in rows)


def test_reject_policy_prevents_partial_linear_snapshot(tmp_path):
    index, _, _ = boundaries(tmp_path)
    pbf = tmp_path / "linear.osm.pbf"
    write_pbf(pbf)
    with pytest.raises(ValueError, match="Linear POI rejected"):
        extract_pois(pbf, {"bakery": ["shop"]}, ["name"], index, lambda r: None,
                     lambda r: None, tmp_path / "work", line_policy="reject")


def test_boundary_crs_and_invalid_polygon_rejected(tmp_path):
    _, geojson, registry = boundaries(tmp_path)
    document = json.loads(geojson.read_text())
    document["crs"] = {"type": "name", "properties": {"name": "EPSG:3857"}}
    geojson.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="WGS84"):
        RegionIndex(geojson, registry)
    document.pop("crs")
    document["features"][0]["geometry"] = mapping(Polygon([(0, 0), (2, 2), (2, 0), (0, 2)]))
    geojson.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="Invalid region polygon"):
        RegionIndex(geojson, registry)


def test_antimeridian_line_and_polygon_points_stay_near_dateline(tmp_path):
    class DatelineIndex:
        def lookup(self, lon, lat):
            assert abs(lon) >= 179
            return {"region_code": "RU-CHU", "region_name": "Чукотка"}
    pbf = tmp_path / "dateline.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as writer:
        for n, point in enumerate([(179, 65), (-179, 65), (-179, 66), (179, 66)], 1):
            writer.add_node(osmium.osm.mutable.Node(id=n, location=point))
        writer.add_way(osmium.osm.mutable.Way(id=1, nodes=[1, 2], tags={"shop": "bakery"}))
        writer.add_way(osmium.osm.mutable.Way(id=2, nodes=[1, 2, 3, 4, 1], tags={"shop": "bakery"}))
    rows = []
    extract_pois(pbf, {"bakery": ["shop"]}, ["name"], DatelineIndex(), rows.append,
                 lambda r: None, tmp_path / "work")
    assert len(rows) == 2
    assert all(179 <= abs(r["lon"]) <= 180 for r in rows)
    from shapely import from_wkt
    assert all(from_wkt(r["wkt"]).x == r["lon"] for r in rows)


def test_unrelated_missing_way_locations_do_not_invalidate_selected_node(tmp_path):
    index, _, _ = boundaries(tmp_path)
    pbf = tmp_path / "unrelated.osm.pbf"
    with osmium.SimpleWriter(str(pbf)) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=(1, 1), tags={"shop": "bakery"}))
        writer.add_way(osmium.osm.mutable.Way(id=5, nodes=[999, 1000]))
    rows = []
    extract_pois(pbf, {"bakery": ["shop"]}, ["name"], index, rows.append,
                 lambda r: None, tmp_path / "work")
    assert len(rows) == 1


def test_unsplit_antimeridian_boundaries_rejected(tmp_path):
    _, geojson, registry = boundaries(tmp_path)
    document = json.loads(geojson.read_text())
    document["features"][0]["geometry"] = mapping(Polygon([(179, 65), (-179, 65), (-179, 66), (179, 66)]))
    geojson.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="split its polygon"):
        RegionIndex(geojson, registry)
