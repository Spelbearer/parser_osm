import csv
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

import numpy as np
from pyproj import Geod
import pytest
from shapely import STRtree,points

from poi_osm.kml import build,circle,neighbors,validate,NS
from poi_osm.teradata_export import export
from test_collection import collection,run_collection
from test_pipeline import project

GEOD = Geod(ellps='WGS84')
BASE = {'radius_m':60,'min_count':5,'count_mode':'distinct_names','points_scope':'all_named',
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
    for entry in report['files']:
        if entry['kind']==kind:
            result.extend(ET.parse(Path(report['output'])/entry['file']).findall('.//{'+NS+'}Placemark'))
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
    package,_=export(first['output'],tmp_path/'warehouse',CONFIG/'teradata_export.json')
    reduced=build(package,tmp_path/'kml-reduced',config_path)
    assert reduced['counts']==internal['counts']
    assert len(kml_nodes(reduced,'points'))==3
