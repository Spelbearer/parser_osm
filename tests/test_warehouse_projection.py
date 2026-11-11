import csv
import json
from pathlib import Path
import subprocess
import sys

import pyarrow.parquet as pq
import pytest

from poi_osm.prepare_production import prepare
from poi_osm.teradata_export import Projection,export
from poi_osm.selection import TERADATA_FIELDS
from test_collection import collection,run_collection,part,supplement
from test_pipeline import project,write_json


DROP = {'osm_type','osm_uid','region_code','region_id','country','address','primary_tag','primary_reason',
        'all_tags','telecom_evidence','brand','operator','chain_id','chain_status','chain_evidence',
        'source_date','first_seen_date','last_seen_date','source_file'}


def rows(path):
    with path.open(encoding='utf-8-sig',newline='') as stream:
        return list(csv.DictReader(stream,delimiter=';'))


def test_projection_preserves_typed_identity_and_drops_fields():
    profile = {'osm_id_format':'typed','fields':[f for f in TERADATA_FIELDS if f not in DROP]}
    mapper = Projection(profile)
    source = {f:'' for f in TERADATA_FIELDS}
    source.update(osm_type='node',osm_id='123')
    a = mapper.apply(source)
    source['osm_type'] = 'way'
    b = mapper.apply(source)
    assert a['osm_id']=='node/123' and b['osm_id']=='way/123'
    assert not DROP.intersection(a)
    assert len(a)==len(TERADATA_FIELDS)-19
    with pytest.raises(ValueError,match='requires osm_type'):
        Projection({**profile,'osm_id_format':'numeric_with_type'})
    numeric = Projection({'osm_id_format':'numeric_with_type','fields':['osm_type','osm_id']})
    assert numeric.apply(source)=={'osm_type':'way','osm_id':'123'}


def test_reduced_csv_parquet_and_regional_files_preserve_original(collection,tmp_path):
    first = run_collection(collection)
    run = Path(first['output'])
    saved = (run/'poi_teradata.csv').read_bytes()
    profile = tmp_path/'export.json'
    write_json(profile,{'osm_id_format':'typed','fields':[f for f in TERADATA_FIELDS if f not in DROP]})
    output,report = export(run,tmp_path/'warehouse',profile)
    assert report['file_checks_ok']
    assert report['total_rows']==report['unique_objects']==3
    assert report['regional_export']['by_region']==first['regional_export']['by_region']
    assert (run/'poi_teradata.csv').read_bytes()==saved
    assert not (output/'keys.sqlite').exists()
    data = rows(output/'poi_teradata.csv')
    assert not DROP.intersection(data[0])
    assert all(r['osm_id'].startswith('node/') for r in data)
    assert pq.read_table(output/'poi_teradata.parquet').num_rows==3
    assert pq.read_table(output/'poi_teradata.parquet').schema.field('is_telecom').type.__str__()=='bool'
    parts = [r for entry in report['regional_export']['files'] for r in rows(output/entry['file'])]
    assert sorted(parts,key=lambda r:r['osm_id'])==sorted(data,key=lambda r:r['osm_id'])


def test_prepare_reviewed_boundaries_then_production_reuses_cache(collection,tmp_path):
    first = run_collection(collection)
    output,report = prepare(Path(first['output']),tmp_path/'approved',collection/'config/settings.json',collection/'profile.json')
    assert report['status']=='PRODUCTION_CONFIGURATION_PREPARED'
    assert not (collection/'results/state/last_success.json').exists()
    assert json.loads((output/'regions.geojson').read_text())['review_required'] is False
    assert (output/'run_production.ps1').is_file()
    result = subprocess.run([sys.executable,'-m','poi_osm.collection',str(collection/'inputs'),
         '--output',str(collection/'results'),'--config',report['settings'],'--profile',report['profile'],
         '--production','--require-cache'],cwd=Path(__file__).resolve().parents[1],capture_output=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
    final = json.loads(result.stdout)
    assert final['status']=='SUCCESS' and final['boundaries_reviewed']
    assert final['cached_extracts']==2
    assert (collection/'results/state/last_success.json').is_file()


def test_prepare_and_run_cli_makes_reduced_production_package(collection,tmp_path):
    part(collection/'inputs/beta-261108.osm.pbf','RU-XX',10,20,3)
    initial = run_collection(collection)
    path = supplement(collection,initial['sources'])
    first = run_collection(collection,'--boundary-supplement',str(path),'--require-cache')
    result = subprocess.run([sys.executable,'-m','poi_osm.prepare_production',first['output'],
        '--output',str(tmp_path/'approved'),'--config',str(collection/'config/settings.json'),
        '--profile',str(collection/'profile.json'),'--run'],cwd=Path(__file__).resolve().parents[1],
        capture_output=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
    report = json.loads(result.stdout)
    assert report['status']=='PRODUCTION_AND_EXPORT_SUCCESS'
    assert len(json.loads((Path(report['production_run'])/'approved_regions.json').read_text())['regions'])==2
    production = json.loads((Path(report['production_run'])/'summary.json').read_text())
    assert production['boundary_report']['missing_region_codes']==[]
    assert production['extraction_candidate_boundary_report']['missing_region_codes']==['RU-BB']
    package = report['warehouse_export']
    assert package['status']=='TERADATA_EXPORT_SUCCESS'
    assert package['production'] and package['boundaries_reviewed'] and package['production_ready']
    assert package['total_rows']==3
    assert set(package['excluded_fields'])==DROP-{'osm_type'}
    assert 'osm_type' in rows(Path(package['output'])/'poi_teradata.csv')[0]
    assert all(r['osm_id'].isdigit() for r in rows(Path(package['output'])/'poi_teradata.csv'))
    assert len(package['columns'])==17
    assert str(pq.read_schema(Path(package['output'])/'poi_teradata.parquet').field('osm_id').type)=='int64'
    approved = json.loads(Path(report['teradata_profile']).read_text())
    assert approved['osm_id_format']=='numeric_with_type'
