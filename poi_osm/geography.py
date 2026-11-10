"""Validated administrative polygons and unambiguous point-in-region lookup."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4
from .runtime import cleanup_index

from shapely.geometry import Point, mapping, shape
from shapely.strtree import STRtree


class RegionIndex:
    """Use explicit, documented WGS84 polygons, never administrative name tags.

    A registry is ``{source, coverage_id, regions: [{region_code, region_name,
    region_id?}]}``. GeoJSON features must carry ``region_code``. Polygons
    sharing a boundary deliberately return no match for a point on that border.
    """

    def __init__(self, geojson_path: Path, regions_path: Path):
        registry = json.loads(Path(regions_path).read_text(encoding="utf-8"))
        document = json.loads(Path(geojson_path).read_text(encoding="utf-8"))
        if not registry.get("source") or not registry.get("coverage_id"):
            raise ValueError("Region registry requires source and coverage_id metadata")
        if not document.get("source"):
            raise ValueError("Boundary GeoJSON requires source metadata")
        if document.get("coverage_id") != registry["coverage_id"]:
            raise ValueError("Boundary and registry coverage_id must match")
        crs = document.get("crs")
        if crs and crs.get("properties", {}).get("name") not in {
            "EPSG:4326", "urn:ogc:def:crs:OGC:1.3:CRS84", "urn:ogc:def:crs:EPSG::4326"
        }:
            raise ValueError("Only WGS84 / EPSG:4326 boundaries are supported")
        if document.get("type") != "FeatureCollection":
            raise ValueError("Boundaries must be a GeoJSON FeatureCollection")
        self.coverage_id = registry["coverage_id"]
        self.registry = {}
        for region in registry.get("regions", []):
            code = region.get("region_code")
            if not code or not region.get("region_name") or code in self.registry:
                raise ValueError("Region registry needs unique nonempty codes and names")
            self.registry[code] = dict(region)
        if not self.registry:
            raise ValueError("Region registry is empty")
        self.geometries = []
        self.regions = []
        seen = set()
        for feature in document.get("features", []):
            code = feature.get("properties", {}).get("region_code")
            if code not in self.registry or code in seen:
                raise ValueError(f"Unknown or duplicate boundary region_code: {code}")
            geometry = shape(feature["geometry"])
            if (geometry.geom_type not in {"Polygon", "MultiPolygon"}
                    or geometry.is_empty or not geometry.is_valid):
                raise ValueError(f"Invalid region polygon: {code}")
            minx, miny, maxx, maxy = geometry.bounds
            if not (-180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90):
                raise ValueError(f"Boundary coordinates outside EPSG:4326: {code}")
            pieces = geometry.geoms if geometry.geom_type == "MultiPolygon" else [geometry]
            for piece in pieces:
                for ring in [piece.exterior, *piece.interiors]:
                    coordinates = list(ring.coords)
                    if any(abs(a[0] - b[0]) > 180 for a, b in zip(coordinates, coordinates[1:])):
                        raise ValueError(f"Boundary {code} crosses the antimeridian; split its polygon at ±180° before use")
            self.geometries.append(geometry)
            self.regions.append(self.registry[code])
            seen.add(code)
        missing = self.registry.keys() - seen
        if missing:
            raise ValueError(f"Missing boundary polygons for: {', '.join(sorted(missing))}")
        self.tree = STRtree(self.geometries)
        self.last_problem = None

    def lookup(self, lon: float, lat: float) -> dict | None:
        point = Point(lon, lat)
        matches = [int(index) for index in self.tree.query(point)
                   if self.geometries[int(index)].covers(point)]
        self.last_problem = None if len(matches) == 1 else (
            "ambiguous_region" if matches else "outside_configured_regions")
        return dict(self.regions[matches[0]]) if len(matches) == 1 else None


def prepare_boundaries(pbf_path: Path, output_path: Path, work_dir: Path) -> dict:
    """Extract *candidate* admin_level=4 RU polygons for human validation.

    OSM reflects differing territorial claims. Consequently this output has no
    coverage_id and cannot be used for publication until its polygons and codes
    have been reviewed against the chosen registry and geographic scope.
    """
    import osmium
    from osmium.geom import WKBFactory
    from shapely import wkb

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(output_path)
    temporary = output_path.with_name(output_path.name + ".partial")
    factory = WKBFactory()
    index_dir = work_dir.parent / "indexes"
    index_dir.mkdir(parents=True, exist_ok=True)
    index = index_dir / f"boundaries-{uuid4().hex}.idx"
    count = 0
    seen = set()
    with temporary.open("w", encoding="utf-8") as stream:
        metadata = {
            "type": "FeatureCollection", "source": f"OSM PBF: {Path(pbf_path).name}",
            "license": "ODbL-1.0; © OpenStreetMap contributors",
            "review_required": True,
            "scope_warning": "Candidate OSM administrative claims; validate territorial scope before use."
        }
        stream.write(json.dumps(metadata, ensure_ascii=False)[:-1] + ', "features": [')

        class Handler(osmium.SimpleHandler):
            def area(self, area):
                nonlocal count
                tags = dict(area.tags)
                code = tags.get("ISO3166-2", "")
                if (tags.get("boundary") != "administrative" or tags.get("admin_level") != "4"
                        or not code.startswith("RU-")):
                    return
                if code in seen:
                    raise ValueError(f"Duplicate administrative area {code}; review the source")
                geometry = wkb.loads(factory.create_multipolygon(area), hex=True)
                if not geometry.is_valid or geometry.is_empty:
                    raise ValueError(f"Invalid administrative area {code}")
                feature = {"type": "Feature", "properties": {
                    "region_code": code, "region_name": tags.get("name:ru", tags.get("name", "")),
                    "osm_type": "way" if area.from_way() else "relation", "osm_id": str(area.orig_id())
                }, "geometry": mapping(geometry)}
                if count:
                    stream.write(",")
                stream.write(json.dumps(feature, ensure_ascii=False))
                seen.add(code)
                count += 1

        Handler().apply_file(str(pbf_path), locations=True, idx=f"sparse_file_array,{index}")
        stream.write("]}\n")
    if not count:
        raise ValueError("No administrative RU admin_level=4 polygons found")
    temporary.replace(output_path)
    cleanup_index(index)
    return {"candidate_regions": count, "review_required": True}
