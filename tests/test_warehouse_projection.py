import csv
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from helpers import run_collection, write_json

from poi_osm.selection import TERADATA_FIELDS
from poi_osm.teradata_export import Projection, export

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


def test_edw_column_uses_dictionary_code_independently_of_display_name():
    import json

    from poi_osm.classification import CONFIG
    from poi_osm.region_dictionary import RegionDictionary

    dictionary = RegionDictionary(CONFIG/'region_dictionary.json', json.loads((CONFIG/'regions.json').read_text()))
    mapper = Projection({'osm_id_format':'numeric_with_type',
                         'fields':['osm_type','osm_id','region_name','REGION_NAME_EDW']}, dictionary)
    source = {'osm_type':'node','osm_id':'1','region_code':'RU-TA','region_name':'Республика Татарстан'}
    assert mapper.apply(source) == {'osm_type':'node','osm_id':'1','region_name':'Республика Татарстан',
                                    'REGION_NAME_EDW':'Казань'}
    source.update(region_code='', region_name='')
    assert mapper.apply(source)['REGION_NAME_EDW'] == ''
