"""Streaming OSM extraction, with disk backed locations and relation dependencies."""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from uuid import uuid4

import osmium
from .runtime import cleanup_index
from osmium.geom import WKBFactory
from shapely import transform, wkb
from shapely.geometry import Point
from shapely.ops import unary_union


def matching_categories(tags: dict, mapping: dict[str, list[str]]):
    """Exact key/value matching; one deterministic row per category."""
    for category, keys in mapping.items():
        for key in keys:
            if tags.get(key) == category:
                yield category, key
                break


def extract_pois(pbf_path: Path, mapping: dict[str, list[str]], name_priority: list[str],
                 region_index, emit, review, work_dir: Path,
                 line_policy: str = "midpoint") -> dict:
    """Read complete PBF without keeping its objects or coordinates in memory.

    Area assembly is delegated to libosmium's multipolygon manager. A separate
    relation pass stores member references in SQLite so linear relations can
    reuse geometries without retaining all ways in RAM. Any matching object
    whose geometry cannot be resolved aborts the snapshot, protecting deletions.
    """
    if line_policy not in {"midpoint", "reject"}:
        raise ValueError("line_policy must be midpoint or reject")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    dependency_path = work_dir / f"geometry-{token}.sqlite"
    # Native map handles can remain open on Windows until process exit. Keep
    # these disposable files outside the directory atomically published later.
    index_dir = work_dir.parent / "indexes"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / f"locations-{token}.idx"
    connection = sqlite3.connect(dependency_path)
    connection.executescript("""
        PRAGMA cache_size=-32768;
        CREATE TABLE relations(id INTEGER PRIMARY KEY, members TEXT NOT NULL, tags TEXT);
        CREATE TABLE needed_ways(id INTEGER PRIMARY KEY, geometry BLOB);
        CREATE TABLE needed_nodes(id INTEGER PRIMARY KEY, geometry BLOB);
        CREATE TABLE pending(kind TEXT, id INTEGER, PRIMARY KEY(kind,id));
    """)
    stats = Counter()
    factory = WKBFactory()
    values_by_key = {}
    for category, keys in mapping.items():
        for key in keys:
            values_by_key.setdefault(key, set()).add(category)

    def selected_tags(tags):
        # Check the few configured keys, not all fifty categories per OSM node.
        return any(tags.get(key) in values for key, values in values_by_key.items())

    class Relations(osmium.SimpleHandler):
        def relation(self, relation):
            tags = dict(relation.tags)
            selected = selected_tags(tags)
            members = [(member.type, member.ref) for member in relation.members]
            connection.execute("INSERT INTO relations VALUES(?,?,?)", (
                relation.id, json.dumps(members), json.dumps(tags) if selected else None))

    try:
        Relations().apply_file(str(pbf_path))
        connection.commit()

        def mark_dependencies(relation_id, ancestors):
            if relation_id in ancestors:
                raise ValueError(f"Cyclic relation dependency at relation/{relation_id}")
            row = connection.execute("SELECT members FROM relations WHERE id=?", (relation_id,)).fetchone()
            if row is None:
                raise ValueError(f"Missing member relation/{relation_id} in source")
            for kind, member_id in json.loads(row[0]):
                if kind == "w":
                    connection.execute("INSERT OR IGNORE INTO needed_ways(id) VALUES(?)", (member_id,))
                elif kind == "n":
                    connection.execute("INSERT OR IGNORE INTO needed_nodes(id) VALUES(?)", (member_id,))
                elif kind == "r":
                    mark_dependencies(member_id, ancestors | {relation_id})

        for relation_id, tags_json in connection.execute("SELECT id,tags FROM relations WHERE tags IS NOT NULL"):
            tags = json.loads(tags_json)
            if tags.get("type") not in {"multipolygon", "boundary"}:
                mark_dependencies(relation_id, set())
            connection.execute("INSERT INTO pending VALUES('relation',?)", (relation_id,))
        connection.commit()
        has_needed_nodes = bool(connection.execute("SELECT 1 FROM needed_nodes LIMIT 1").fetchone())
        has_needed_ways = bool(connection.execute("SELECT 1 FROM needed_ways LIMIT 1").fetchone())

        def output(kind, object_id, tags, geometry, area=False):
            # Dateline-crossing coordinates must be interpreted in a continuous
            # longitude interval before planar point selection. POIs are local
            # geometries; shifting negative longitudes avoids a midpoint at 0°.
            if geometry.bounds[2] - geometry.bounds[0] > 180:
                def unwrap(coordinates):
                    shifted = coordinates.copy()
                    shifted[shifted[:, 0] < 0, 0] += 360
                    return shifted
                geometry = transform(geometry, unwrap)
            if geometry.is_empty or not geometry.is_valid:
                raise ValueError(f"Invalid geometry for {kind}/{object_id}")
            if area:
                point = geometry.representative_point()
                if not geometry.covers(point):
                    raise ValueError(f"Representative point outside {kind}/{object_id}")
            elif geometry.geom_type == "Point":
                point = geometry
            else:
                if line_policy == "reject":
                    raise ValueError(f"Linear POI rejected by configured policy: {kind}/{object_id}")
                # Linear-reference interpolation works on lines and multilines.
                if geometry.geom_type not in {"LineString", "MultiLineString"}:
                    raise ValueError(f"Unsupported non-area geometry for {kind}/{object_id}: {geometry.geom_type}")
                point = geometry.interpolate(0.5, normalized=True)
            lon, lat = point.x, point.y
            if lon > 180:
                lon -= 360
                point = Point(lon, lat)
            if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError(f"Invalid WGS84 coordinates for {kind}/{object_id}")
            region = region_index.lookup(lon, lat)
            name = next((tags[k] for k in name_priority if tags.get(k)), None)
            address = json.dumps({k: v for k, v in tags.items() if k.startswith("addr:")},
                                 ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for category, key in matching_categories(tags, mapping):
                row = {
                    "tag": category, "osm_id": str(object_id), "osm_type": kind,
                    "point_name": name, "lon": lon, "lat": lat, "wkt": point.wkt,
                    "osm_key": key, "osm_value": tags[key],
                    "region_name": region.get("region_name", "") if region else "",
                    "region_code": region.get("region_code", "") if region else "",
                    "region_id": str(region.get("region_id", "")) if region else "",
                    "country": "Россия" if region else None, "address": address,
                }
                if region is None:
                    review({**row, "review_reason": getattr(region_index, "last_problem", None) or "unknown_region"})
                    stats["unknown_region"] += 1
                emit(row)
                stats["poi_records"] += 1
                if name is None:
                    stats["unnamed_records"] += 1

        class Handler(osmium.SimpleHandler):
            def node(self, node):
                stats["processed_nodes"] += 1
                tags = dict(node.tags)
                selected = selected_tags(tags)
                needed = has_needed_nodes and connection.execute("SELECT 1 FROM needed_nodes WHERE id=?", (node.id,)).fetchone()
                if not selected and not needed:
                    return
                geometry = wkb.loads(factory.create_point(node), hex=True)
                if needed:
                    connection.execute("UPDATE needed_nodes SET geometry=? WHERE id=?", (geometry.wkb, node.id))
                if selected:
                    output("node", node.id, tags, geometry)

            def way(self, way):
                stats["processed_ways"] += 1
                tags = dict(way.tags)
                selected = selected_tags(tags)
                needed = has_needed_ways and connection.execute("SELECT 1 FROM needed_ways WHERE id=?", (way.id,)).fetchone()
                if not selected and not needed:
                    return
                is_area = way.is_closed() and len(way.nodes) >= 4 and tags.get("area") != "no"
                if needed or not is_area:
                    geometry = wkb.loads(factory.create_linestring(way), hex=True)
                    if needed:
                        connection.execute("UPDATE needed_ways SET geometry=? WHERE id=?", (geometry.wkb, way.id))
                    if selected and not is_area:
                        output("way", way.id, tags, geometry)
                if selected and is_area:
                    connection.execute("INSERT INTO pending VALUES('way',?)", (way.id,))

            def relation(self, relation):
                stats["processed_relations"] += 1

            def area(self, area):
                tags = dict(area.tags)
                if not selected_tags(tags):
                    return
                kind = "way" if area.from_way() else "relation"
                # area=no closed ways must retain the configured linear rule.
                if kind == "way" and tags.get("area") == "no":
                    return
                geometry = wkb.loads(factory.create_multipolygon(area), hex=True)
                output(kind, area.orig_id(), tags, geometry, area=True)
                connection.execute("DELETE FROM pending WHERE kind=? AND id=?", (kind, area.orig_id()))

        # Unlike SimpleHandler.apply_file(area=...), restrict the area manager's
        # first pass to selected tag pairs. Otherwise libosmium would retain
        # topology for every unrelated multipolygon relation in the country.
        manager = osmium.area.AreaManager()
        area_filter = osmium.filter.TagFilter(*[(key, value) for key, values in values_by_key.items() for value in values])
        with osmium.io.Reader(str(pbf_path), osmium.osm.osm_entity_bits.RELATION) as reader:
            osmium.apply(reader, area_filter, manager.first_pass_handler())
        node_index = osmium.index.create_map(f"sparse_file_array,{index_path}")
        location_handler = osmium.NodeLocationsForWays(node_index)
        location_handler.ignore_errors()
        handler = Handler()
        area_handler = manager.second_pass_handler(handler)
        with osmium.io.Reader(str(pbf_path), osmium.osm.osm_entity_bits.OBJECT) as reader:
            osmium.apply(reader, location_handler, area_handler, handler)
        # Release mmap and C++ location-handler references before Windows unlink.
        del area_handler, handler, location_handler, node_index, manager

        def relation_geometry(relation_id, ancestors):
            if relation_id in ancestors:
                raise ValueError(f"Cyclic relation geometry at {relation_id}")
            row = connection.execute("SELECT members FROM relations WHERE id=?", (relation_id,)).fetchone()
            if row is None:
                raise ValueError(f"Missing relation/{relation_id}")
            geometries = []
            for kind, member_id in json.loads(row[0]):
                if kind == "r":
                    geometries.append(relation_geometry(member_id, ancestors | {relation_id}))
                else:
                    table = "needed_ways" if kind == "w" else "needed_nodes"
                    member = connection.execute(f"SELECT geometry FROM {table} WHERE id=?", (member_id,)).fetchone()
                    if member is None or member[0] is None:
                        raise ValueError(f"Missing geometry for relation/{relation_id} member {kind}/{member_id}")
                    geometries.append(wkb.loads(member[0]))
            if not geometries:
                raise ValueError(f"Empty relation/{relation_id}")
            result = unary_union(geometries)
            if result.geom_type == "GeometryCollection":
                lines = [part for part in result.geoms if part.geom_type in {"LineString", "MultiLineString"}]
                if not lines:
                    raise ValueError(f"Relation/{relation_id} has no usable line or area geometry")
                result = unary_union(lines)
            return result

        for relation_id, tags_json in connection.execute("SELECT id,tags FROM relations WHERE tags IS NOT NULL"):
            tags = json.loads(tags_json)
            if tags.get("type") in {"multipolygon", "boundary"}:
                continue
            geometry = relation_geometry(relation_id, set())
            output("relation", relation_id, tags, geometry)
            connection.execute("DELETE FROM pending WHERE kind='relation' AND id=?", (relation_id,))
        unresolved = connection.execute("SELECT kind,id FROM pending LIMIT 10").fetchall()
        if unresolved:
            raise ValueError(f"Incomplete or unassembled POI geometry; snapshot rejected: {unresolved}")
        stats["processed_osm_objects"] = sum(stats[k] for k in ("processed_nodes", "processed_ways", "processed_relations"))
        connection.close()
        dependency_path.unlink(missing_ok=True)
        cleanup_index(index_path)
        return dict(stats)
    except BaseException:
        connection.close()
        # Preserve failed-stage files for diagnosis; the caller owns publication.
        raise
