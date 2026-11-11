import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from helpers import configure, csv_rows, part, run_collection, supplement, write_json
from shapely.geometry import Polygon

from poi_osm.collection import inspect_sources, profile_from, worker_for
from poi_osm.config import load_settings
from poi_osm.geography import combine_boundaries, split_dateline
from poi_osm.region_files import split_regions


def test_collection_merge_region_chunks_and_checkpoint_reuse(collection):
    first=run_collection(collection)
    assert first['status']=='COLLECTION_PREVIEW_SUCCESS'
    assert first['metrics']['unique_objects']==3
    assert first['duplicate_category_rows_removed']==1
    assert first['object_export']['teradata_objects']==3
    assert first['regional_export']['total_rows']==3
    assert len(first['regional_export']['files'])==3  # A splits 2 rows into 2 parts; B has 1.
    output=Path(first['output'])
    assert len(csv_rows(output/'poi_teradata.csv'))==3
    assert {r['region_code'] for r in csv_rows(output/'poi_teradata.csv')}=={'RU-AA','RU-BB'}
    assert all(info['rows']<=1 for info in first['regional_export']['files'])
    assert len(csv_rows(output/'regions_index.csv'))==3
    assert not (collection/'results/state/last_success.json').exists()
    second=run_collection(collection)
    assert second['cached_extracts']==2
    assert second['metrics']['unique_objects']==3
    assert Path(first['registry']).is_file()


def test_collection_check_is_read_only_and_requires_complete_file_set(collection):
    check=run_collection(collection,'--check')
    assert len(check['sources'])==2
    assert len(check['excluded_files'])==1
    assert not (collection/'results').exists()
    (collection/'inputs/beta-261108.osm.pbf').unlink()
    failed=run_collection(collection,'--check',ok=False)
    assert 'Missing extracts' in failed.stderr


def test_production_gate_then_safe_full_comparison_and_conflict_failure(collection):
    failed=run_collection(collection,'--production',ok=False)
    assert 'reviewed boundaries' in failed.stderr
    configure(collection,boundaries_reviewed=True,coverage_confirmed=True)
    profile=json.loads((collection/'profile.json').read_text(encoding='utf-8'))
    profile['complete_snapshot_confirmed']=True
    write_json(collection/'profile.json',profile)
    first=run_collection(collection,'--production')
    assert first['status']=='SUCCESS'
    assert first['metrics']['changes']=={'NEW':3,'UNCHANGED':0}
    second=run_collection(collection,'--production')
    assert second['cached_extracts']==2
    assert second['metrics']['changes']=={'UNCHANGED':3}
    part(collection/'inputs/alpha-261108.osm.pbf','RU-AA',0,10,2,include_extra=False)
    third=run_collection(collection,'--production')
    assert third['metrics']['changes']=={'DELETED':1,'UNCHANGED':2}
    pointer=collection/'results/state/last_success.json'
    saved=pointer.read_bytes()
    # Selected category set differs for the same shared object across extracts.
    part(collection/'inputs/beta-261108.osm.pbf','RU-BB',10,20,3,shared_tag='hotel')
    failed=run_collection(collection,'--production',ok=False)
    assert 'Conflicting overlapping object' in failed.stderr
    assert pointer.read_bytes()==saved


def test_wrong_md5_prevents_processing(collection):
    path=collection/'inputs/alpha-261108.osm.pbf'
    Path(str(path)+'.md5').write_text('0'*32+'  '+path.name,encoding='ascii')
    failed=run_collection(collection,'--check',ok=False)
    assert 'MD5 mismatch' in failed.stderr


def test_region_split_preserves_bom_quotes_and_enforces_limit(tmp_path):
    source=tmp_path/'source.csv'
    with source.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['osm_id','region_code','region_name','point_name'],delimiter=';')
        writer.writeheader()
        for n in range(5):writer.writerow({'osm_id':n,'region_code':'RU-AA','region_name':'Регион А','point_name':'Имя; "кавычки"\nстрока'})
    report=split_regions(source,tmp_path/'regions',{'regions':[{'region_code':'RU-AA','region_name':'Регион А'}]},max_rows=2)
    assert [f['rows'] for f in report['files']]==[2,2,1]
    assert sum(len(csv_rows(tmp_path/f['file'])) for f in report['files'])==5


def test_dateline_admin_polygon_can_be_split_for_region_index():
    polygon=Polygon([(179,64),(-179,64),(-179,66),(179,66)])
    result=split_dateline(polygon)
    assert result.is_valid
    assert result.geom_type=='MultiPolygon'
    assert result.area==pytest.approx(4)


def test_interrupted_worker_is_stopped_without_checkpoint(collection,monkeypatch):
    source=inspect_sources(collection/'inputs',profile_from(collection/'profile.json'))[0][0]
    class Process:
        calls=0
        terminated=False
        def wait(self,timeout=None):
            self.calls+=1
            if self.calls==1:raise KeyboardInterrupt()
            return 0
        def terminate(self):self.terminated=True
    process=Process()
    monkeypatch.setattr('poi_osm.collection.subprocess.Popen',lambda *a,**k:process)
    with pytest.raises(KeyboardInterrupt):
        worker_for(source,load_settings(collection/'config/settings.json'),collection/'config/settings.json',collection/'results')
    assert process.terminated
    assert not list((collection/'results/cache').glob('*/current.json'))


def test_production_review_flags_must_be_real_booleans(collection):
    configure(collection,boundaries_reviewed='false',coverage_confirmed=True)
    profile=json.loads((collection/'profile.json').read_text(encoding='utf-8'))
    write_json(collection/'profile.json',dict(profile,complete_snapshot_confirmed=True))
    result=run_collection(collection,'--production',ok=False)
    assert 'requires reviewed boundaries' in result.stderr
    assert not (collection/'results/cache').exists()


def test_mixed_replication_states_are_rejected_before_parsing(collection):
    date=datetime.now(timezone.utc).date().isoformat()
    part(collection/'inputs/beta-261108.osm.pbf','RU-BB',10,20,3,timestamp=date+'T12:00:00Z')
    result=run_collection(collection,'--check',ok=False)
    assert 'replication timestamps differ' in result.stderr
    assert not (collection/'results').exists()


def test_nested_autonomous_regions_get_unambiguous_candidate_assignment(tmp_path):
    job=tmp_path/'job';job.mkdir()
    geoms={'RU-TYU':[(0,0),(10,0),(10,10),(0,10),(0,0)],
           'RU-KHM':[(2,2),(4,2),(4,4),(2,4),(2,2)],
           'RU-YAN':[(6,6),(8,6),(8,8),(6,8),(6,6)]}
    features=[{'type':'Feature','properties':{'region_code':code},'geometry':{'type':'Polygon','coordinates':[coords]}}
              for code,coords in geoms.items()]
    write_json(job/'candidate.geojson',{'features':features})
    registry={'source':'synthetic nesting test','coverage_id':'test','regions':[
        {'region_code':code,'region_name':code} for code in geoms]}
    output=tmp_path/'output';output.mkdir()
    index,report=combine_boundaries([{'job':str(job)}],registry,output)
    assert index.lookup(3,3)['region_code']=='RU-KHM'
    assert index.lookup(7,7)['region_code']=='RU-YAN'
    assert index.lookup(1,1)['region_code']=='RU-TYU'
    assert report['nested_region_adjustments']==['RU-TYU']
    assert report['missing_region_codes']==[]


def test_different_polygons_for_same_code_are_reported_not_arbitrarily_selected(tmp_path):
    jobs=[]
    for n,size in enumerate([2,3]):
        job=tmp_path/str(n);job.mkdir();jobs.append({'job':str(job)})
        write_json(job/'candidate.geojson',{'features':[{'type':'Feature','properties':{'region_code':'RU-AA'},
                  'geometry':{'type':'Polygon','coordinates':[[[0,0],[size,0],[size,size],[0,size],[0,0]]]}}]})
    registry={'source':'synthetic conflict test','coverage_id':'test','regions':[{'region_code':'RU-AA','region_name':'A'}]}
    output=tmp_path/'output';output.mkdir()
    index,report=combine_boundaries(jobs,registry,output)
    assert index.lookup(1,1) is None
    assert report['conflicting_region_codes']==['RU-AA']
    assert report['missing_region_codes']==['RU-AA']


def test_recovered_supplement_reassigns_cached_pois_without_extraction(collection):
    # A valid polygon exists in this extract, but it lacks the configured subject code.
    part(collection/'inputs/beta-261108.osm.pbf','RU-XX',10,20,3)
    first=run_collection(collection)
    assert first['status']=='COLLECTION_PREVIEW_INCOMPLETE'
    assert first['boundary_report']['missing_region_codes']==['RU-BB']
    assert first['metrics']['by_region']['']==1
    saved=(Path(first['output'])/'summary.json').read_bytes()
    path=supplement(collection,first['sources'])
    second=run_collection(collection,'--boundary-supplement',str(path),'--require-cache')
    assert second['cached_extracts']==2
    assert second['status']=='COLLECTION_PREVIEW_SUCCESS'
    assert second['boundary_report']['missing_region_codes']==[]
    assert second['boundary_report']['candidate_region_count']==2
    assert second['object_export']['teradata_objects']==first['object_export']['teradata_objects']==3
    assert second['metrics']['by_region']=={'RU-AA':2,'RU-BB':1}
    assert second['metrics']['unassigned_fraction']==0
    assert second['boundary_supplement']['review_required']
    assert not second['production'] and not second['boundaries_reviewed']
    assert (Path(first['output'])/'summary.json').read_bytes()==saved
    assert not (collection/'results/state/last_success.json').exists()
    assert len(list((collection/'results/cache').glob('*/attempt-*')))==2


def test_cache_only_refuses_missing_checkpoint(collection):
    failed=run_collection(collection,'--require-cache',ok=False)
    assert 'prevents PBF extraction' in failed.stderr
    assert not (collection/'results/cache').exists()


def test_supplement_provenance_checked_before_workers(collection):
    path=supplement(collection,[])
    failed=run_collection(collection,'--boundary-supplement',str(path),ok=False)
    assert 'sources differ' in failed.stderr
    assert not (collection/'results/cache').exists()


def test_unreviewed_supplement_not_accepted_in_production(collection):
    failed=run_collection(collection,'--production','--boundary-supplement',str(collection/'supplement.geojson'),ok=False)
    assert 'unreviewed candidates' in failed.stderr
    assert not (collection/'results').exists()
