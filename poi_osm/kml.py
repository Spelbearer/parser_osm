"""Build regional KML point/circle layers and publish a verified local ZIP."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import shutil
import sqlite3
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from pyproj import Geod
from shapely import STRtree, points
from shapely.geometry import Polygon, box

from .circle_counts import FIELDS as CIRCLE_FIELDS
from .circle_counts import circle_key
from .classification import CONFIG
from .geography import split_dateline
from .runtime import atomic_json, configure_utf8
from .sources import _hash_file
from .visit_zones import select_zones

LOG = logging.getLogger(__name__)
NS = 'http://www.opengis.net/kml/2.2'
ET.register_namespace('',NS)
GEOD = Geod(ellps='WGS84')
RENDERER_VERSION = 4
POINT_ICON = 'http://maps.google.com/mapfiles/kml/paddle/ltblu-blank.png'
LABELS = {'point_name':'Название','osm_type':'Тип OSM','osm_id':'ID OSM','address':'Адрес',
          'region_name':'Регион','region_code':'Код региона','primary_category':'Категория','primary_subcategory':'Подкатегория',
          'primary_osm_key':'Ключ OSM','primary_osm_value':'Тег OSM','is_telecom':'Телефония',
          'telecom_type':'Тип телефонии','is_chain':'Крупная сеть','chain_name':'Название сети',
          'center_osm_type':'Тип OSM центра','center_osm_id':'ID OSM центра','radius_m':'Радиус, м',
          'zone_id':'Зона посещения','center_lon':'Долгота центра','center_lat':'Широта центра',
          'telecom_count':'Точек телефонии','chain_count':'Точек крупных сетей','distinct_names':'Разных названий','poi_count':'Точек в зоне','count_mode':'Способ подсчёта'}


def clean(value):
    return ''.join(c for c in str(value) if c in '\t\n\r' or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)


def element(parent, tag, value=None, **attrs):
    child = ET.SubElement(parent,'{'+NS+'}'+tag,attrs)
    if value is not None: child.text = clean(value)
    return child


def validate(config):
    config = {**config}
    config.setdefault('export_points', False)
    config.setdefault('circle_mode', 'visit_zones')
    config.setdefault('zone_overlap_threshold', 0.8)
    if config['circle_mode'] not in {'visit_zones', 'per_point'}:
        raise ValueError('circle_mode must be visit_zones or per_point')
    overlap = config['zone_overlap_threshold']
    if isinstance(overlap, bool) or not isinstance(overlap, (int, float)) or not math.isfinite(overlap) or not 0 < overlap <= 1:
        raise ValueError('zone_overlap_threshold must be in (0, 1]')
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
    for field in ['include_unassigned','telecom_only','exclude_chains','export_points']:
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
        title = ET.Element('{'+NS+'}name')
        display=self.names.get(code,'Без региона')
        if sum(value==display for value in self.names.values())>1: display+=f' [{code}]'
        title.text = clean(display+' — '+{'circles':'Зоны скопления POI','points':'Точки POI'}[kind])
        stream.write(ET.tostring(title,encoding='unicode'))
        stream.write('<Style id="circle"><LineStyle><color>ff0088ff</color><width>2</width></LineStyle><PolyStyle><color>400088ff</color></PolyStyle></Style>'
                     + ''.join(f'<Style id="{style_kind}"><IconStyle><color>ffffffff</color><scale>1</scale>'
                               f'<Icon><href>{POINT_ICON}</href></Icon>'
                               '<hotSpot x="0.5" y="0" xunits="fraction" yunits="fraction"/>'
                               '</IconStyle></Style>' for style_kind in (('poi','telecom') if kind=='points' else ())))
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
                     'points_exported','points_in_selected_circles','duplicate_centers_removed','zone_candidates','near_duplicate_zones_removed',
                     'unnamed_excluded','unassigned_excluded','excluded_type','excluded_region',
                     'non_telecom_excluded','chain_excluded']})
    reverse = {}
    for code,name in names.items():
        if code:
            reverse[name] = None if name in reverse else code
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
            if 'region_code' not in row and row['region_name'] and reverse.get(row['region_name'],'') is None:
                raise ValueError('Ambiguous region label; generate KML from the full source with ISO region_code')
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
        telecom,chains = np.zeros(count,dtype=bool),np.zeros(count,dtype=bool)
        for seq,lon,lat,name,payload in db.execute('SELECT seq,lon,lat,name,payload FROM points ORDER BY seq'):
            lons[seq],lats[seq] = lon,lat
            name_ids[seq] = dictionary.setdefault(name,len(dictionary))
            attributes = json.loads(payload)
            telecom[seq],chains[seq] = attributes.get('is_telecom')=='1',attributes.get('is_chain')=='1'
        tree = STRtree(points(lons,lats)) if count else None
        included = np.zeros(count,dtype=bool)
        catalog_path=stage/'circles.csv'
        catalog_stream=catalog_path.open('x',encoding='utf-8',newline='')
        catalog_writer=csv.DictWriter(catalog_stream,fieldnames=CIRCLE_FIELDS,delimiter=';');catalog_writer.writeheader()
        circle_keys=set()
        def save_circle(lon,lat,radius):
            key=circle_key(lon,lat,radius)
            if key not in circle_keys:
                circle_keys.add(key)
                catalog_writer.writerow(dict(circle_id=key,lon=lon,lat=lat,radius_m=radius))

        if config['circle_mode']=='visit_zones':
            for zone in select_zones(db,tree,lons,lats,name_ids,telecom,chains,config,neighbors,GEOD,stats):
                ids = zone.pop('members');representative = zone.pop('representative')
                code = db.execute('SELECT region FROM points WHERE seq=?',(representative,)).fetchone()[0]
                included[ids] = True
                data = {field:zone[field] for field in ('poi_count','chain_count','telecom_count')}
                data.update(region_name=names[code],radius_m=config['radius_m'])
                title = f"Зона — {zone['poi_count']} точек, сети: {zone['chain_count']}, телефония: {zone['telecom_count']}"
                layers.write('circles',code,placemark(title,data,circle(zone['center_lon'],zone['center_lat'],config['radius_m'],config['circle_vertices']),'circle'))
                stats['circles'] += 1
                save_circle(zone['center_lon'],zone['center_lat'],config['radius_m'])
        else:
            for seq,code,lon,lat in db.execute('SELECT MIN(seq),region,lon,lat FROM points GROUP BY region,lon,lat ORDER BY MIN(seq)'):
                ids = neighbors(tree,lon,lat,lons,lats,config['radius_m'])
                distinct = len(np.unique(name_ids[ids]))
                metric = distinct if config['count_mode']=='distinct_names' else len(ids)
                stats['candidate_centers'] += 1
                if metric >= config['min_count']:
                    included[ids] = True
                    data = {'poi_count':len(ids),'chain_count':int(chains[ids].sum()),'telecom_count':int(telecom[ids].sum()),
                            'region_name':names[code],'radius_m':config['radius_m']}
                    title = f"Зона — {len(ids)} точек, сети: {data['chain_count']}, телефония: {data['telecom_count']}"
                    feature = placemark(title,data,
                                        circle(lon,lat,config['radius_m'],config['circle_vertices']),'circle')
                    layers.write('circles',code,feature);stats['circles'] += 1
                    save_circle(lon,lat,config['radius_m'])
                if stats['candidate_centers'] % 5000==0: LOG.info('Checked %s circle centers; selected %s',stats['candidate_centers'],stats['circles'])
            stats['duplicate_centers_removed'] = count-stats['candidate_centers']
        catalog_stream.close()
        stats['unique_circles']=len(circle_keys)
        stats['points_in_selected_circles'] = int(included.sum())
        if config['export_points']:
            for seq,code,payload in db.execute('SELECT seq,region,payload FROM points ORDER BY seq'):
                if config['points_scope']=='selected_circles' and not included[seq]: continue
                row = json.loads(payload)
                data = {field:row.get(field) for field in LABELS if field in row}
                feature = placemark(row['point_name'],data,(float(row['lon']),float(row['lat'])),
                                    'telecom' if row.get('is_telecom')=='1' else 'poi')
                layers.write('points',code,feature);stats['points_exported'] += 1
        visible_codes = [c for c in names if (c or config['include_unassigned']) and (config['regions'] is None or c in config['regions'])]
        for kind in (['circles','points'] if config['export_points'] else ['circles']):
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
                  'circle_catalog_sha256':_hash_file(catalog_path),'renderer_version':RENDERER_VERSION,'source_csv_sha256':source_hash,'output':str(destination),'archive':str(archive_path),
                  'config':config,'counts':dict(stats),'files':layers.files,'file_checks_ok':True,
                  'layers_kept':bool(keep_unpacked),'layer_paths':'ZIP entries',
                  'license':'ODbL-1.0; © OpenStreetMap contributors',
                  'semantics':'WGS84 strict radius; named eligible objects; visit_zones use local group centers and greedy member-overlap suppression; per_point uses POI centers; zones can overlap and cross regional borders; membership is not clipped by region.'}
        atomic_json(stage/'summary.json',report)
        with (stage/'regions_index.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer = csv.DictWriter(stream,fieldnames=['kind','region_code','region_name','file','features'],delimiter=';')
            writer.writeheader();writer.writerows(layers.files)
        temporary_zip = stage/'archive.zip.partial'
        paths = [stage/f['file'] for f in layers.files]+[stage/'summary.json',stage/'regions_index.csv',stage/'config.json',catalog_path]
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
        if 'catalog_stream' in locals(): catalog_stream.close()
        layers.close();db.close()
        atomic_json(output_root/'last_failure.json',{'status':'FAILED','error':str(error),'source_run':str(run)})
        if stage.exists(): shutil.rmtree(stage)
        try: (output_root/'work').rmdir()
        except OSError: pass
        raise


def main(argv=None):
    configure_utf8()
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
    except Exception:
        LOG.exception('KML export failed; previous ZIP retained')
        raise SystemExit(1)
