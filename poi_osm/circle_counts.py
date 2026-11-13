"""Count membership of every exported POI in unique final WGS84 circles."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
from pathlib import Path

import numpy as np
from shapely import STRtree, points

from .sources import _hash_file

LOG=logging.getLogger(__name__)
FIELDS=['circle_id','lon','lat','radius_m']


def circle_key(lon,lat,radius):
    lon=-180.0 if lon==180 else float(lon)
    values=[0.0 if value==0 else float(value) for value in (lon,lat,radius)]
    return hashlib.sha256(json.dumps(values,separators=(',',':')).encode()).hexdigest()


def annotate(source,catalog,neighbors):
    source,catalog=Path(source),Path(catalog)
    digest=_hash_file(source)
    with source.open(encoding='utf-8-sig',newline='') as stream:
        rows=list((float(row['lon']),float(row['lat'])) for row in csv.DictReader(stream,delimiter=';'))
    coordinates=np.asarray(rows,dtype=np.float64).reshape((-1,2))
    del rows
    lons,lats=coordinates[:,0],coordinates[:,1]
    if not np.all(np.isfinite(coordinates)) or np.any(np.abs(lons)>180) or np.any(np.abs(lats)>90):
        raise ValueError('Invalid POI coordinates for circle membership')
    tree=STRtree(points(lons,lats)) if len(lons) else None
    counts=np.zeros(len(lons),dtype=np.int64)
    seen=set()
    with catalog.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream,delimiter=';')
        if reader.fieldnames!=FIELDS: raise ValueError('Invalid circle catalog columns')
        for row in reader:
            lon,lat,radius=map(float,(row['lon'],row['lat'],row['radius_m']))
            if not np.isfinite([lon,lat,radius]).all() or abs(lon)>180 or abs(lat)>90 or not 0<radius<=10000:
                raise ValueError('Invalid circle catalog geometry')
            key=circle_key(lon,lat,radius)
            if key!=row['circle_id']: raise ValueError('Circle catalog identity mismatch')
            if key in seen: continue
            seen.add(key)
            if tree is not None:
                ids=neighbors(tree,lon,lat,lons,lats,radius)
                counts[ids]+=1
            if len(seen)%5000==0:LOG.info('Counted membership in %d unique circles',len(seen))
    maximum=int(counts.max()) if len(counts) else 0
    if maximum>2147483647: raise ValueError('CNT_CIRCLE exceeds INTEGER range')
    temporary=source.with_suffix('.counts.tmp')
    try:
        with source.open(encoding='utf-8-sig',newline='') as incoming,temporary.open('x',encoding='utf-8-sig',newline='') as outgoing:
            reader=csv.DictReader(incoming,delimiter=';')
            fields=list(reader.fieldnames or [])
            if 'CNT_CIRCLE' not in fields: fields.append('CNT_CIRCLE')
            writer=csv.DictWriter(outgoing,fieldnames=fields,delimiter=';');writer.writeheader()
            number=0
            for number,row in enumerate(reader,1):
                if number>len(counts):raise ValueError('Source POI count changed during circle membership')
                row['CNT_CIRCLE']=str(int(counts[number-1]));writer.writerow(row)
            if number!=len(counts):raise ValueError('Source POI count changed during circle membership')
        if _hash_file(source)!=digest:raise ValueError('Source changed during circle membership')
        os.replace(temporary,source)
    finally:
        temporary.unlink(missing_ok=True)
    return {'points':len(counts),'unique_circles':len(seen),'points_in_circles':int(np.count_nonzero(counts)),
            'memberships':int(counts.sum()),'max_count':maximum,'catalog_sha256':_hash_file(catalog),
            'source_csv_sha256':digest,'semantics':'all exported POIs; unique geometric circles; WGS84 distance strictly below radius; original coordinates'}
