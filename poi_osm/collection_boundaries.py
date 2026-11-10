"""Combine candidate OSM boundaries, retaining explicit review requirements."""
import json
from pathlib import Path

from shapely import transform
from shapely.geometry import LineString, MultiPolygon, mapping, shape
from shapely.ops import split

from .geography import RegionIndex
from .runtime import atomic_json
from .preview import UnassignedIndex


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
