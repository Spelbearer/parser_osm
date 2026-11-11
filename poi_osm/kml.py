"""Build regional KML point/circle layers and publish a verified local ZIP."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
from pyproj import Geod
from shapely import STRtree, points
from shapely.geometry import Polygon, box

from .classification import CONFIG
from .collection_boundaries import split_dateline
from .runtime import atomic_json
from .sources import _hash_file

LOG = logging.getLogger(__name__)
NS = 'http://www.opengis.net/kml/2.2'
ET.register_namespace('',NS)
GEOD = Geod(ellps='WGS84')
LABELS = {'point_name':'Название','osm_type':'Тип OSM','osm_id':'ID OSM','address':'Адрес',
          'region_name':'Регион','primary_category':'Категория','primary_subcategory':'Подкатегория',
          'primary_osm_key':'Ключ OSM','primary_osm_value':'Тег OSM','is_telecom':'Телефония',
          'telecom_type':'Тип телефонии','is_chain':'Крупная сеть','chain_name':'Название сети',
          'center_osm_type':'Тип OSM центра','center_osm_id':'ID OSM центра','radius_m':'Радиус, м',
          'distinct_names':'Разных названий','poi_count':'Объектов POI','count_mode':'Способ подсчёта'}


def clean(value):
    return ''.join(c for c in str(value) if c in '\t\n\r' or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)


def element(parent, tag, value=None, **attrs):
    child = ET.SubElement(parent,'{'+NS+'}'+tag,attrs)
    if value is not None: child.text = clean(value)
    return child


def validate(config):
    radius = config.get('radius_m')
    if isinstance(radius,bool) or not isinstance(radius,(int,float)) or not math.isfinite(radius) or not 0 < radius <= 10000:
        raise ValueError('radius_m must be in (0, 10000] metres')
    for field,minimum,maximum in [('min_count',1,1000000),('circle_vertices',16,720),('max_features_per_file',1,100000)]:
        value = config.get(field)
        if isinstance(value,bool) or not isinstance(value,int) or not minimum <= value <= maximum:
            raise ValueError(f'Invalid {field}')
    if config.get('count_mode') not in {'distinct_names','objects'}:
        raise ValueError('count_mode must be distinct_names or objects')
    if config.get('points_scope') not in {'all_named','selected_circles'}:
        raise ValueError('points_scope must be all_named or selected_circles')
    for field in ['include_unassigned','telecom_only','exclude_chains']:
        if not isinstance(config.get(field),bool): raise ValueError(f'{field} must be boolean')
    kinds = config.get('osm_types')
    if not isinstance(kinds,list) or not kinds or any(k not in {'node','way','relation'} for k in kinds) or len(kinds)!=len(set(kinds)):
        raise ValueError('Invalid osm_types')
    codes = config.get('regions')
    if codes is not None and (not isinstance(codes,list) or not codes or any(not isinstance(c,str) for c in codes)):
        raise ValueError('regions must be null or a nonempty code list')
    return config


def neighbors(tree, lon, lat, lons, lats, radius):
    # Conservative latitude/longitude envelope; exact distance uses WGS84 below.
    delta = math.degrees(radius/6330000)
    dy = delta*1.001
    cos = math.cos(math.radians(min(89.999999,abs(lat)+dy)))
    dx = min(180,dy/max(cos,1e-12))
    west,east = lon-dx,lon+dx
    boxes = [box(max(-180,west),max(-90,lat-dy),min(180,east),min(90,lat+dy))]
    if west < -180: boxes.append(box(west+360,max(-90,lat-dy),180,min(90,lat+dy)))
    if east > 180: boxes.append(box(-180,max(-90,lat-dy),east-360,min(90,lat+dy)))
    ids = np.unique(np.concatenate([tree.query(b) for b in boxes]))
    if not len(ids): return ids
    _,_,distance = GEOD.inv(np.full(len(ids),lon),np.full(len(ids),lat),lons[ids],lats[ids])
    return ids[np.asarray(distance) < radius]


def circle(lon, lat, radius, vertices):
    bearings = np.linspace(0,360,vertices,endpoint=False)
    xs,ys,_ = GEOD.fwd(np.full(vertices,lon),np.full(vertices,lat),bearings,np.full(vertices,radius))
    return split_dateline(Polygon(zip(xs,ys)))


def placemark(name, data, geometry, style):
    root = ET.Element('{'+NS+'}Placemark')
    element(root,'name',name); element(root,'styleUrl','#'+style)
    description = '\n'.join(f'{LABELS.get(k,k)}: {v}' for k,v in data.items() if v not in (None,''))
    element(root,'description',description)
    extended = element(root,'ExtendedData')
    for key,value in data.items():
        if value is not None:
            element(element(extended,'Data',name=key),'value',value)
    if isinstance(geometry,tuple):
        point = element(root,'Point')
        element(point,'coordinates',f'{geometry[0]:.10f},{geometry[1]:.10f},0')
    else:
        polygons = list(geometry.geoms) if geometry.geom_type=='MultiPolygon' else [geometry]
        parent = element(root,'MultiGeometry') if len(polygons)>1 else root
        for polygon in polygons:
            node = element(parent,'Polygon'); element(node,'altitudeMode','clampToGround')
            for name,ring in [('outerBoundaryIs',polygon.exterior)]+[('innerBoundaryIs',r) for r in polygon.interiors]:
                linear = element(element(node,name),'LinearRing')
                element(linear,'coordinates',' '.join(f'{x:.10f},{y:.10f},0' for x,y in ring.coords))
    return ET.tostring(root,encoding='unicode')


class Layers:
    def __init__(self, root, names, limit):
        self.root,self.names,self.limit = root,names,limit
        self.open = {}; self.parts = Counter(); self.files = []

    def start(self,kind,code):
        self.parts[kind,code] += 1
        label = re.sub(r'[^\w .-]','_',self.names.get(code,'Без региона')).rstrip(' .')[:100]
        filename = f'{code or "unassigned"}_{label}_{self.parts[kind,code]:03d}.kml'
        path = self.root/'layers'/kind/filename
        path.parent.mkdir(parents=True,exist_ok=True)
        stream = path.open('x',encoding='utf-8')
        stream.write(f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="{NS}"><Document>')
        title = ET.Element('{'+NS+'}name'); title.text = clean(self.names.get(code,'Без региона')+' — '+{'circles':'Зоны скопления POI','points':'Точки POI'}[kind])
        stream.write(ET.tostring(title,encoding='unicode'))
        stream.write('<Style id="circle"><LineStyle><color>ff0088ff</color><width>2</width></LineStyle><PolyStyle><color>400088ff</color></PolyStyle></Style>'
                     '<Style id="poi"><IconStyle><color>ffff8800</color></IconStyle></Style>'
                     '<Style id="telecom"><IconStyle><color>ff00bb00</color></IconStyle></Style>')
        info = {'kind':kind,'region_code':code,'region_name':self.names.get(code,'Без региона'),
                'file':path.relative_to(self.root).as_posix(),'features':0}
        self.files.append(info); self.open[kind,code] = (stream,info)

    def write(self,kind,code,feature):
        key = kind,code
        if key not in self.open: self.start(kind,code)
        stream,info = self.open[key]
        if info['features'] >= self.limit:
            stream.write('</Document></kml>');stream.close(); self.start(kind,code)
            stream,info = self.open[key]
        stream.write(feature);info['features'] += 1

    def close(self):
        for stream,_ in self.open.values():
            if not stream.closed: stream.write('</Document></kml>');stream.close()


def load(source, db, config, names):
    stats = Counter({key:0 for key in ['source_rows','eligible_points','candidate_centers','circles',
                     'points_exported','points_in_selected_circles','duplicate_centers_removed',
                     'unnamed_excluded','unassigned_excluded','excluded_type','excluded_region',
                     'non_telecom_excluded','chain_excluded']})
    reverse = {name:code for code,name in names.items() if code}
    db.execute('CREATE TABLE keys(kind TEXT,id TEXT,PRIMARY KEY(kind,id)) WITHOUT ROWID')
    db.execute('CREATE TABLE points(seq INTEGER PRIMARY KEY,lon REAL,lat REAL,region TEXT,name TEXT,payload TEXT)')
    with source.open(encoding='utf-8-sig',newline='') as stream:
        reader = csv.DictReader(stream,delimiter=';')
        if not {'osm_type','osm_id','point_name','lon','lat','region_name'} <= set(reader.fieldnames or []):
            raise ValueError('Source must contain object identities, names, coordinates and regions')
        for row in reader:
            stats['source_rows'] += 1
            if None in row or any(v is None for v in row.values()): raise ValueError('Malformed source CSV row')
            kind,ident = row['osm_type'],row['osm_id']
            if kind not in {'node','way','relation'} or not ident.isascii() or not ident.isdigit() or int(ident)<=0:
                raise ValueError('Invalid source identity')
            try: db.execute('INSERT INTO keys VALUES(?,?)',(kind,ident))
            except sqlite3.IntegrityError as error: raise ValueError(f'Duplicate object {kind}/{ident}') from error
            lon,lat = float(row['lon']),float(row['lat'])
            if not (math.isfinite(lon) and math.isfinite(lat) and -180<=lon<=180 and -90<=lat<=90):
                raise ValueError('Invalid source coordinates')
            code = (row['region_code'] or '') if 'region_code' in row else (reverse.get(row['region_name'],'') if row['region_name'] else '')
            if row['region_name'] and (not code or code not in names): raise ValueError('Unknown source region')
            if code and names[code]!=row['region_name']: raise ValueError('Region label differs from index')
            if kind not in config['osm_types']: stats['excluded_type'] += 1;continue
            if not code and not config['include_unassigned']: stats['unassigned_excluded'] += 1;continue
            if config['regions'] is not None and code not in config['regions']: stats['excluded_region'] += 1;continue
            name = row['point_name'].strip()
            if not name: stats['unnamed_excluded'] += 1;continue
            if config['telecom_only'] and row.get('is_telecom')!='1': stats['non_telecom_excluded'] += 1;continue
            if config['exclude_chains'] and row.get('is_chain')=='1': stats['chain_excluded'] += 1;continue
            if abs(lat)>89.9: raise ValueError('KML circles near geographic poles are unsupported')
            names[code] = row['region_name'] or 'Без региона'
            seq = stats['eligible_points']
            row.update(point_name=name,region_code=code)
            db.execute('INSERT INTO points VALUES(?,?,?,?,?,?)',(seq,lon,lat,code,name,json.dumps(row,ensure_ascii=False)))
            stats['eligible_points'] += 1
            if stats['source_rows'] % 10000 == 0: db.commit()
            if stats['source_rows'] % 100000 == 0: LOG.info('Loaded %s source objects',stats['source_rows'])
    db.commit()
    return stats


def build(run, output_root, config_path, *, keep_unpacked=False, source_reference=None):
    run = Path(run).resolve()
    config = validate(json.loads(Path(config_path).read_text(encoding='utf-8-sig')))
    info = json.loads((run/'summary.json').read_text(encoding='utf-8-sig'))
    if info.get('status') not in {'SUCCESS','COLLECTION_PREVIEW_SUCCESS','TERADATA_EXPORT_SUCCESS'}:
        raise ValueError('Use a completed successful collection run or warehouse package')
    if info.get('status')=='TERADATA_EXPORT_SUCCESS' and not info.get('file_checks_ok'):
        raise ValueError('Warehouse file checks did not pass')
    names = {}
    with (run/'regions_index.csv').open(encoding='utf-8-sig',newline='') as stream:
        for row in csv.DictReader(stream,delimiter=';'):
            code,name = row['region_code'],row['region_name']
            if code and not all(c.isalnum() or c in '-_' for c in code): raise ValueError('Unsafe region code')
            if code in names and names[code]!=name: raise ValueError('Conflicting region labels')
            names[code] = name
    if config['regions'] is not None and set(config['regions'])-names.keys(): raise ValueError('Unknown configured regions')
    source = run/'poi_teradata.csv'; source_hash = _hash_file(source)
    output_root = Path(output_root).resolve(); output_root.mkdir(parents=True,exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
    stage = output_root/'work'/run_id; stage.mkdir(parents=True)
    destination = output_root/'runs'/run_id
    archive_path = output_root/f'poi_layers-{run_id}.zip'
    atomic_json(stage/'summary.json',{'status':'RUNNING','source_run':str(run)})
    atomic_json(stage/'config.json',config)
    db_path = stage/'points.sqlite';db = sqlite3.connect(db_path)
    db.execute('PRAGMA cache_size=-32768');db.execute('PRAGMA temp_store=FILE')
    layers = Layers(stage,names,config['max_features_per_file'])
    try:
        stats = load(source,db,config,names)
        expected = info.get('total_rows',info.get('object_export',{}).get('teradata_objects'))
        if stats['source_rows']!=expected: raise ValueError('Source row count differs from summary')
        count = stats['eligible_points']
        lons,lats = np.empty(count),np.empty(count)
        name_ids = np.empty(count,dtype=np.int32); dictionary = {}
        for seq,lon,lat,name in db.execute('SELECT seq,lon,lat,name FROM points ORDER BY seq'):
            lons[seq],lats[seq] = lon,lat
            name_ids[seq] = dictionary.setdefault(name,len(dictionary))
        tree = STRtree(points(lons,lats)) if count else None
        included = np.zeros(count,dtype=bool)
        for seq,code,lon,lat in db.execute('SELECT MIN(seq),region,lon,lat FROM points GROUP BY region,lon,lat ORDER BY MIN(seq)'):
            ids = neighbors(tree,lon,lat,lons,lats,config['radius_m'])
            distinct = len(np.unique(name_ids[ids]))
            metric = distinct if config['count_mode']=='distinct_names' else len(ids)
            stats['candidate_centers'] += 1
            if metric >= config['min_count']:
                included[ids] = True
                center = json.loads(db.execute('SELECT payload FROM points WHERE seq=?',(seq,)).fetchone()[0])
                data = {'center_osm_type':center['osm_type'],'center_osm_id':center['osm_id'],
                        'radius_m':config['radius_m'],'distinct_names':distinct,'poi_count':len(ids),
                        'count_mode':config['count_mode'],'region_name':names[code]}
                feature = placemark(f"{center['point_name']} — {distinct} названий, {len(ids)} POI",data,
                                    circle(lon,lat,config['radius_m'],config['circle_vertices']),'circle')
                layers.write('circles',code,feature);stats['circles'] += 1
            if stats['candidate_centers'] % 5000==0: LOG.info('Checked %s circle centers; selected %s',stats['candidate_centers'],stats['circles'])
        stats['duplicate_centers_removed'] = count-stats['candidate_centers']
        stats['points_in_selected_circles'] = int(included.sum())
        for seq,code,payload in db.execute('SELECT seq,region,payload FROM points ORDER BY seq'):
            if config['points_scope']=='selected_circles' and not included[seq]: continue
            row = json.loads(payload)
            data = {field:row.get(field) for field in LABELS if field in row}
            feature = placemark(row['point_name'],data,(float(row['lon']),float(row['lat'])),
                                'telecom' if row.get('is_telecom')=='1' else 'poi')
            layers.write('points',code,feature);stats['points_exported'] += 1
        visible_codes = [c for c in names if (c or config['include_unassigned']) and (config['regions'] is None or c in config['regions'])]
        for kind in ['circles','points']:
            for code in sorted(visible_codes):
                if not layers.parts[kind,code]: layers.start(kind,code)
        layers.close()
        for file in layers.files:
            seen = 0
            for _,node in ET.iterparse(stage/file['file'],events=('end',)):
                if node.tag=='{'+NS+'}Placemark': seen += 1
                node.clear()
            if seen!=file['features']: raise ValueError('KML feature count verification failed')
        if _hash_file(source)!=source_hash: raise ValueError('Source CSV changed during KML generation')
        report = {'status':'KML_EXPORT_SUCCESS','source_run':str(source_reference or run),'source_status':info['status'],
                  'source_csv_sha256':source_hash,'output':str(destination),'archive':str(archive_path),
                  'config':config,'counts':dict(stats),'files':layers.files,'file_checks_ok':True,
                  'layers_kept':bool(keep_unpacked),'layer_paths':'ZIP entries',
                  'license':'ODbL-1.0; © OpenStreetMap contributors',
                  'semantics':'WGS84 distances; named eligible objects; center included; strict radius; circles may overlap or cross regional borders; membership is not clipped by region.'}
        atomic_json(stage/'summary.json',report)
        with (stage/'regions_index.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer = csv.DictWriter(stream,fieldnames=['kind','region_code','region_name','file','features'],delimiter=';')
            writer.writeheader();writer.writerows(layers.files)
        temporary_zip = stage/'archive.zip.partial'
        paths = [stage/f['file'] for f in layers.files]+[stage/'summary.json',stage/'regions_index.csv',stage/'config.json']
        with zipfile.ZipFile(temporary_zip,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
            for path in paths: archive.write(path,path.relative_to(stage).as_posix())
        with zipfile.ZipFile(temporary_zip) as archive:
            if archive.testzip() is not None or len(archive.namelist())!=len(paths): raise ValueError('ZIP verification failed')
        db.close(); db_path.unlink()
        destination.parent.mkdir(parents=True,exist_ok=True);stage.replace(destination)
        (destination/'archive.zip.partial').replace(archive_path)
        if not keep_unpacked: shutil.rmtree(destination/'layers')
        atomic_json(output_root/'last_success.json',report)
        try: (output_root/'work').rmdir()
        except OSError: pass
        return report
    except BaseException as error:
        layers.close();db.close()
        atomic_json(output_root/'last_failure.json',{'status':'FAILED','error':str(error),'source_run':str(run)})
        if stage.exists(): shutil.rmtree(stage)
        try: (output_root/'work').rmdir()
        except OSError: pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path,help='Completed internal run or reduced Teradata package')
    parser.add_argument('--output',type=Path,required=True,help='Local folder in which ZIP archives and run reports are saved')
    parser.add_argument('--config',type=Path,default=CONFIG/'kml.json')
    parser.add_argument('--keep-unpacked',action='store_true',help='Keep unpacked KML only for explicit debugging')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s')
    report = build(args.run,args.output,args.config,keep_unpacked=args.keep_unpacked)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    try: raise SystemExit(main())
    except KeyboardInterrupt: raise SystemExit(130)
    except Exception as error:
        LOG.exception('KML export failed; previous ZIP retained')
        raise SystemExit(1)
