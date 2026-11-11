"""Validated administrative polygons and unambiguous point-in-region lookup."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from shapely import transform
from shapely.geometry import LineString, MultiPolygon, Point, mapping, shape
from shapely.ops import split
from shapely.strtree import STRtree

from .runtime import atomic_json, cleanup_index


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


class UnassignedIndex:
    last_problem = "no_usable_preview_boundary"

    def lookup(self, lon, lat):
        return None


def split_dateline(geometry):
    polygons=list(geometry.geoms) if geometry.geom_type=='MultiPolygon' else [geometry]
    result=[]
    for polygon in polygons:
        crossing=any(abs(a[0]-b[0])>180 for ring in [polygon.exterior,*polygon.interiors]
                     for a,b in zip(list(ring.coords),list(ring.coords)[1:]))
        if not crossing:
            result.append(polygon)
            continue
        def unwrap(coords):
            values=coords.copy()
            values[values[:,0]<0,0]+=360
            return values
        continuous=transform(polygon,unwrap)
        if not continuous.is_valid:
            raise ValueError('Invalid administrative geometry after dateline unwrapping')
        for part in split(continuous,LineString([(180,-90),(180,90)])).geoms:
            if part.representative_point().x>180:
                def shift(coords):
                    values=coords.copy(); values[:,0]-=360
                    return values
                part=transform(part,shift)
            result.append(part)
    value=result[0] if len(result)==1 else MultiPolygon(result)
    if value.is_empty or not value.is_valid:
        raise ValueError('Invalid administrative geometry')
    return value


def combine_boundaries(jobs,registry,output):
    known={r['region_code']:r for r in registry['regions']}
    geometries={}
    conflicts=set()
    excluded=set()
    invalid={}
    for job in jobs:
        path=Path(job['job'])/'candidate.geojson'
        if not path.exists():
            continue
        document=json.loads(path.read_text(encoding='utf-8'))
        for feature in document['features']:
            code=feature['properties']['region_code']
            if code not in known:
                excluded.add(code); continue
            if code in conflicts:
                continue
            try:
                geom=split_dateline(shape(feature['geometry']))
            except (ValueError,RuntimeError) as error:
                invalid[code]=str(error);conflicts.add(code);geometries.pop(code,None)
                continue
            if code in geometries and not geometries[code].equals(geom):
                conflicts.add(code); del geometries[code]
            else:
                geometries[code]=geom
    adjustments=[]
    for parent,children in [('RU-TYU',['RU-KHM','RU-YAN']),('RU-ARK',['RU-NEN'])]:
        if parent in geometries:
            original=geometries[parent]
            for child in children:
                if child in geometries:
                    geometries[parent]=geometries[parent].difference(geometries[child])
            if not original.equals(geometries[parent]):
                adjustments.append(parent)
    report={'candidate_region_count':len(geometries),'missing_region_codes':sorted(set(known)-geometries.keys()),
            'conflicting_region_codes':sorted(conflicts),'excluded_boundary_codes':sorted(excluded),
            'nested_region_adjustments':adjustments,'invalid_boundary_codes':invalid,'review_required':True}
    features=[{'type':'Feature','properties':{'region_code':code},'geometry':mapping(geom)} for code,geom in sorted(geometries.items())]
    atomic_json(output/'candidate_boundaries.geojson',{'type':'FeatureCollection','coverage_id':registry['coverage_id'],
                'source':'Unreviewed OSM boundaries combined from local collection; dateline split and nested-region differences',
                'review_required':True,'features':features})
    subset=dict(registry,regions=[known[code] for code in sorted(geometries)])
    atomic_json(output/'candidate_regions.json',subset)
    index=RegionIndex(output/'candidate_boundaries.geojson',output/'candidate_regions.json') if geometries else UnassignedIndex()
    return index,report


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
                geometry = split_dateline(geometry)
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
