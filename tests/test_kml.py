import csv
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import pytest
from helpers import run_collection
from pyproj import Geod
from shapely import STRtree, points

from poi_osm.kml import NS, build, circle, neighbors, validate
from poi_osm.teradata_export import export

GEOD = Geod(ellps='WGS84')
BASE = {'export_points':True,'circle_mode':'per_point','radius_m':60,'min_count':5,'count_mode':'distinct_names','points_scope':'all_named',
        'circle_vertices':72,'max_features_per_file':10000,'osm_types':['node','way'],
        'include_unassigned':False,'telecom_only':False,'exclude_chains':False,'regions':None}


def source(tmp_path, *, repeated=False, dateline=False, same_coordinate=False):
    root = tmp_path/'source';root.mkdir()
    lon,lat = (179.9999,65) if dateline else (37,55)
    rows = []
    for i in range(5):
        x,y,_ = GEOD.fwd(lon,lat,90,0 if same_coordinate else i*3)
        rows.append(dict(osm_type='node' if i!=1 else 'way',osm_id=str(1 if i==1 else i+1),point_name='Сеть' if repeated else f'Точка {i} & <магазин>',
                         lon=str(x),lat=str(y),region_code='RU-AA',region_name='Регион А',
                         is_telecom='1' if i==0 else '0',is_chain='1' if i==1 else '',address='Улица; дом 1',primary_category='Услуги'))
    # Named distant POI belongs to the point layer even though its circle fails.
    x,y,_ = GEOD.fwd(lon,lat,0,1000)
    rows.append({**rows[0],'osm_id':'100','point_name':'Отдельная точка','lon':str(x),'lat':str(y)})
    rows.append({**rows[0],'osm_id':'101','point_name':''})
    rows.append({**rows[0],'osm_id':'102','region_code':'','region_name':''})
    with (root/'poi_teradata.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]),delimiter=';');writer.writeheader();writer.writerows(rows)
    with (root/'regions_index.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['region_code','region_name','file','rows'],delimiter=';');writer.writeheader()
        writer.writerows([{'region_code':'RU-AA','region_name':'Регион А','file':'unused.csv','rows':7},
                          {'region_code':'RU-BB','region_name':'Регион Б','file':'empty.csv','rows':0},
                          {'region_code':'','region_name':'Без региона','file':'none.csv','rows':1}])
    (root/'summary.json').write_text(json.dumps({'status':'SUCCESS','object_export':{'teradata_objects':len(rows)}}))
    return root


def config(tmp_path,**overrides):
    path=tmp_path/'config.json';path.write_text(json.dumps({**BASE,**overrides}),encoding='utf-8');return path


def kml_nodes(report,kind):
    result=[]
    with zipfile.ZipFile(report['archive']) as archive:
        for entry in report['files']:
            if entry['kind']==kind:
                result.extend(ET.fromstring(archive.read(entry['file'])).findall('.//{'+NS+'}Placemark'))
    return result


def test_named_points_circles_and_zip_with_split_files(tmp_path):
    root=source(tmp_path)
    before=(root/'poi_teradata.csv').read_bytes()
    report=build(root,tmp_path/'out',config(tmp_path,max_features_per_file=2))
    assert report['counts']['eligible_points']==6
    assert report['counts']['circles']==5
    assert report['counts']['points_exported']==6
    assert report['counts']['points_in_selected_circles']==5
    assert report['counts']['unnamed_excluded']==1
    assert report['counts']['unassigned_excluded']==1
    assert all(f['features']<=2 for f in report['files'])
    assert not list(Path(report['output']).rglob('*.kml'))
    assert not (tmp_path/'out/work').exists()
    assert len(kml_nodes(report,'circles'))==5
    points=kml_nodes(report,'points')
    assert len(points)==6
    assert 'Точка 0 & <магазин>' in [n.find('{'+NS+'}name').text for n in points]
    assert (root/'poi_teradata.csv').read_bytes()==before
    with zipfile.ZipFile(report['archive']) as archive:
        assert archive.testzip() is None
        assert all(not name.startswith('/') and '..' not in Path(name).parts for name in archive.namelist())
        assert not any('sqlite' in name for name in archive.namelist())
        saved=json.loads(archive.read('summary.json'))
        assert saved['status']=='KML_EXPORT_SUCCESS'
    assert json.loads((tmp_path/'out/last_success.json').read_text())['archive']==report['archive']


def test_threshold_counts_names_not_objects_and_optional_scope(tmp_path):
    root=source(tmp_path,repeated=True)
    report=build(root,tmp_path/'out',config(tmp_path))
    assert report['counts'].get('circles',0)==0
    assert report['counts']['points_exported']==6
    report=build(root,tmp_path/'out',config(tmp_path,count_mode='objects',points_scope='selected_circles'))
    assert report['counts']['circles']==5
    assert report['counts']['points_exported']==5


def test_accurate_distances_without_rounding_and_at_date_line():
    lon,lat=179.9999,65
    coordinates=[(lon,lat)]
    for distance in [59.9,60.1]:
        x,y,_=GEOD.fwd(lon,lat,90,distance);coordinates.append((x,y))
    xs,ys=np.array(coordinates).T
    index=STRtree(points(xs,ys))
    assert set(neighbors(index,lon,lat,xs,ys,60))=={0,1}
    geometry=circle(lon,lat,60,72)
    assert geometry.is_valid and geometry.geom_type=='MultiPolygon'
    # Away from the split seam, every generated vertex is 60 m from its center.
    geometry=circle(37,55,60,72)
    for x,y in geometry.exterior.coords:
        _,_,distance=GEOD.inv(37,55,x,y)
        assert distance==pytest.approx(60,abs=0.0001)


def test_dateline_kml_and_region_filter(tmp_path):
    root=source(tmp_path,dateline=True)
    report=build(root,tmp_path/'out',config(tmp_path,regions=['RU-AA']))
    circles=kml_nodes(report,'circles')
    assert len(circles)==5
    assert any(n.find('{'+NS+'}MultiGeometry') is not None for n in circles)
    assert {f['region_code'] for f in report['files']}=={'RU-AA'}


def test_failure_retains_previous_zip_and_pointer(tmp_path):
    root=source(tmp_path)
    output=tmp_path/'out'
    report=build(root,output,config(tmp_path))
    old=(output/'last_success.json').read_bytes()
    with (root/'poi_teradata.csv').open('a',encoding='utf-8') as stream:
        stream.write('bad;row\n')
    with pytest.raises(ValueError,match='Malformed'):
        build(root,output,config(tmp_path))
    assert (output/'last_success.json').read_bytes()==old
    assert Path(report['archive']).is_file()
    assert not (output/'work').exists()
    assert (output/'last_failure.json').is_file()


@pytest.mark.parametrize('field,value',[('radius_m',0),('radius_m',True),('min_count',0),('count_mode','bad'),
                                       ('points_scope','bad'),('circle_vertices',3),('regions',[])])
def test_invalid_options(field,value):
    with pytest.raises(ValueError): validate({**BASE,field:value})


def test_coincident_centers_deduplicated_but_all_objects_kept(tmp_path):
    root=source(tmp_path,same_coordinate=True)
    report=build(root,tmp_path/'out',config(tmp_path))
    assert report['counts']['circles']==1
    assert report['counts']['duplicate_centers_removed']==4
    assert report['counts']['points_exported']==6


def test_business_filters_applied_before_density_count(tmp_path):
    root=source(tmp_path)
    report=build(root,tmp_path/'out',config(tmp_path,exclude_chains=True))
    assert report['counts']['eligible_points']==5
    assert report['counts']['chain_excluded']==1
    assert report['counts']['circles']==0
    report=build(root,tmp_path/'out',config(tmp_path,telecom_only=True,min_count=1))
    assert report['counts']['eligible_points']==2
    assert report['counts']['circles']==2


def test_full_run_and_reduced_warehouse_are_supported(collection,tmp_path):
    first=run_collection(collection)
    config_path=config(tmp_path,min_count=1)
    internal=build(first['output'],tmp_path/'kml-internal',config_path)
    assert internal['counts']['points_exported']==3
    assert internal['counts']['circles']==3
    from poi_osm.classification import CONFIG
    profile=json.loads((CONFIG/'teradata_export.json').read_text())
    profile['fields'].remove('CNT_CIRCLE')
    profile_path=tmp_path/'projection.json';profile_path.write_text(json.dumps(profile))
    package,_=export(first['output'],tmp_path/'warehouse',profile_path)
    reduced=build(package,tmp_path/'kml-reduced',config_path)
    assert reduced['counts']==internal['counts']
    assert len(kml_nodes(reduced,'points'))==3


def test_reduced_csv_with_ambiguous_region_names_is_not_guessed(tmp_path):
    root=tmp_path/'source';root.mkdir()
    with (root/'poi_teradata.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['osm_type','osm_id','point_name','lon','lat','region_name'],delimiter=';')
        writer.writeheader();writer.writerow({'osm_type':'node','osm_id':'1','point_name':'Точка','lon':37,'lat':55,'region_name':'Один филиал'})
    with (root/'regions_index.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['region_code','region_name'],delimiter=';');writer.writeheader()
        writer.writerows([{'region_code':'RU-AA','region_name':'Один филиал'},{'region_code':'RU-BB','region_name':'Один филиал'}])
    (root/'summary.json').write_text(json.dumps({'status':'SUCCESS','object_export':{'teradata_objects':1}}))
    with pytest.raises(ValueError,match='Ambiguous region label'):
        build(root,tmp_path/'out',config(tmp_path))
    assert not (tmp_path/'out/last_success.json').exists()
    assert not (tmp_path/'out/work').exists()


def zone_source(tmp_path, coordinates, *, repeated=False):
    root=source(tmp_path)
    with (root/'poi_teradata.csv').open(encoding='utf-8-sig', newline='') as stream:
        template=next(csv.DictReader(stream, delimiter=';'))
    records=[{**template, 'osm_type':'node', 'osm_id':str(i+1),
              'point_name':'Одинаковое имя' if repeated else f'Магазин {i}',
              'lon':str(lon), 'lat':str(lat), 'is_telecom':'1' if i<2 else '0',
              'is_chain':'1' if i==2 else '0'} for i,(lon,lat) in enumerate(coordinates)]
    with (root/'poi_teradata.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(template),delimiter=';');writer.writeheader();writer.writerows(records)
    (root/'summary.json').write_text(json.dumps({'status':'SUCCESS','object_export':{'teradata_objects':len(records)}}))
    return root


def zone_data(node):
    return {item.attrib['name']:item.find('{'+NS+'}value').text for item in node.findall('.//{'+NS+'}Data')}


def displayed_center(node):
    coordinates=node.find('.//{'+NS+'}LinearRing/{'+NS+'}coordinates').text.split()
    first=tuple(map(float,coordinates[0].split(',')[:2]))
    opposite=tuple(map(float,coordinates[(len(coordinates)-1)//2].split(',')[:2]))
    bearing,_,distance=GEOD.inv(*first,*opposite)
    return GEOD.fwd(*first,bearing,distance/2)[:2]


def test_visit_zone_replaces_five_circles_and_reports_business_counts(tmp_path):
    root=source(tmp_path)
    report=build(root,tmp_path/'out',config(tmp_path,circle_mode='visit_zones'))
    assert report['counts']['circles']==1
    assert report['counts']['points_exported']==6
    assert report['counts']['points_in_selected_circles']==5
    data=zone_data(kml_nodes(report,'circles')[0])
    assert data['poi_count']=='5'
    assert set(data)=={'poi_count','chain_count','telecom_count','region_name','radius_m'}
    assert data['telecom_count']==data['chain_count']=='1'
    assert displayed_center(kml_nodes(report,'circles')[0])[0]!=37
    assert 'center_osm_id' not in data


def test_zone_center_between_points_finds_group_missed_by_poi_centers(tmp_path):
    coordinates=[GEOD.fwd(37,55,bearing,55)[:2] for bearing in np.linspace(0,360,5,endpoint=False)]
    root=zone_source(tmp_path,coordinates)
    old=build(root,tmp_path/'old',config(tmp_path,circle_mode='per_point'))
    assert old['counts']['circles']==0
    report=build(root,tmp_path/'out',config(tmp_path,circle_mode='visit_zones'))
    assert report['counts']['circles']==1
    data=zone_data(kml_nodes(report,'circles')[0])
    assert data['poi_count']=='5' and data['telecom_count']=='2' and data['chain_count']=='1'
    for lon,lat in coordinates:
        assert GEOD.inv(*displayed_center(kml_nodes(report,'circles')[0]),lon,lat)[2]<60


def test_long_street_is_split_into_compact_zones_without_losing_point_layer(tmp_path,monkeypatch):
    from poi_osm import kml
    original=kml.select_zones
    captured=[]
    def track(*args,**kwargs):
        for zone in original(*args,**kwargs):
            captured.append(dict(zone))
            yield zone
    monkeypatch.setattr(kml,'select_zones',track)
    coordinates=[GEOD.fwd(37,55,90,i*20)[:2] for i in range(20)]
    root=zone_source(tmp_path,coordinates)
    report=build(root,tmp_path/'out',config(tmp_path,circle_mode='visit_zones'))
    zones=kml_nodes(report,'circles')
    assert 1<len(zones)<20 and report['counts']['points_exported']==20
    assert report['counts']['near_duplicate_zones_removed']>0
    memberships=[]
    for node,internal in zip(zones,captured):
        data=zone_data(node)
        ids={i for i,(lon,lat) in enumerate(coordinates) if GEOD.inv(internal['center_lon'],internal['center_lat'],lon,lat)[2]<60}
        assert len(ids)==int(data['poi_count']) and 5<=len(ids)<=7
        assert all(len(ids & old)/min(len(ids),len(old))<0.8 for old in memberships)
        memberships.append(ids)


def test_zones_at_dateline_and_repeated_name_threshold(tmp_path):
    coordinates=[GEOD.fwd(179.9999,65,bearing,55)[:2] for bearing in np.linspace(0,360,5,endpoint=False)]
    root=zone_source(tmp_path,coordinates,repeated=True)
    report=build(root,tmp_path/'out',config(tmp_path,circle_mode='visit_zones'))
    assert report['counts']['circles']==0
    report=build(root,tmp_path/'out',config(tmp_path,circle_mode='visit_zones',count_mode='objects'))
    assert report['counts']['circles']==1
    assert kml_nodes(report,'circles')[0].find('{'+NS+'}MultiGeometry') is not None


def test_existing_config_without_zone_options_defaults_to_visit_zones(tmp_path):
    root=source(tmp_path)
    path=config(tmp_path)
    settings=json.loads(path.read_text());settings.pop('circle_mode');path.write_text(json.dumps(settings))
    report=build(root,tmp_path/'out',path)
    assert report['config']['circle_mode']=='visit_zones' and report['counts']['circles']==1
    assert report['config']['zone_overlap_threshold']==0.8


@pytest.mark.parametrize('field,value',[('circle_mode','bad'),('zone_overlap_threshold',0),
                                       ('zone_overlap_threshold',True),('zone_overlap_threshold',1.1)])
def test_invalid_zone_options(field,value):
    with pytest.raises(ValueError): validate({**BASE,field:value})
