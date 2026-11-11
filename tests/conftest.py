"""Shared fixtures for the production pipeline."""
import pytest
from helpers import part, write_json


@pytest.fixture
def collection(tmp_path):
    project=tmp_path/"poi"
    (project/"config").mkdir(parents=True)
    (project/"boundaries").mkdir()
    write_json(project/"config/tag_mapping.json", {"bakery":["shop"],"hotel":["tourism"]})
    inputs=project/'inputs';inputs.mkdir()
    part(inputs/'alpha-261108.osm.pbf','RU-AA',0,10,2)
    part(inputs/'beta-261108.osm.pbf','RU-BB',10,20,3)
    # Deliberately not a PBF: the excluded extract must never be read.
    (inputs/'crimean-261108.osm.pbf').write_bytes(b'excluded')
    regions=[{'region_code':'RU-AA','region_name':'Регион А','region_id':'a'},
             {'region_code':'RU-BB','region_name':'Регион Б','region_id':'b'}]
    write_json(project/'config/regions.json',{'source':'synthetic test','coverage_id':'test-two-regions','regions':regions})
    features=[]
    for r,w,e in [(regions[0],0,10),(regions[1],10,20)]:
        features.append({'type':'Feature','properties':{'region_code':r['region_code']},
                         'geometry':{'type':'Polygon','coordinates':[[[w,0],[e,0],[e,10],[w,10],[w,0]]]}})
    write_json(project/'boundaries/regions.geojson',{'type':'FeatureCollection','coverage_id':'test-two-regions',
                                                   'source':'synthetic test','features':features})
    profile={'coverage_id':'test-two-regions','expected_extracts':['alpha','beta'],
             'excluded_extracts':['crimean'],'max_rows_per_region_file':1,'complete_snapshot_confirmed':False}
    write_json(project/'profile.json',profile)
    write_json(project/'config/settings.json',{
        'project_root':'..','coverage_id':'test-two-regions','coverage_confirmed':True,'boundaries_reviewed':True,
        'quality':{'max_count_drop_fraction':0.99,'max_region_drop_fraction':0.99,'max_unassigned_fraction':0.01},
        'batch_size':2,'write_parquet':True})
    return project
