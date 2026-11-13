import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_kml import config, source

from poi_osm.kml import build
from poi_osm.region_dictionary import correct_known_kemerovo
from poi_osm.share_export import publish_layers


@pytest.fixture
def installation(tmp_path):
    raw=source(tmp_path)
    report=build(raw,tmp_path/'kml',config(tmp_path,circle_mode='visit_zones',export_points=False))
    root=tmp_path/'data'
    (root/'result/analysis').mkdir(parents=True)
    (root/'config').mkdir();(root/'boundaries').mkdir()
    (root/'result/poi_layers.zip').write_bytes(Path(report['archive']).read_bytes())
    (root/'result/analysis/summary.json').write_text(json.dumps({'status':'PRODUCT_SUCCESS','file_checks_ok':True,'production_ready':True,'product_run_id':'example'}))
    (root/'product.json').write_text(json.dumps({'version':2,'input_folder':'../pbf','region_dictionary':'config/region_dictionary.json'}))
    registry={'coverage_id':'test-publication','regions':[{'region_code':'RU-AA','region_name':'Регион А'},
                                                       {'region_code':'RU-BB','region_name':'Регион Б'}]}
    (root/'boundaries/regions.json').write_text(json.dumps(registry))
    dictionary={'coverage_id':'test-publication','target_column':'EDW','regions':[
        {'region_code':'RU-AA','region_name':'Регион А','EDW':'Благовещенск','CEM':'Амурская область',
         'MACROREGION_NAME':'Байкал и Дальний Восток'},
        {'region_code':'RU-BB','region_name':'Регион Б','EDW':'Кемерово','CEM':'Кемерово','MACROREGION_NAME':'Центр'}]}
    (root/'config/region_dictionary.json').write_text(json.dumps(dictionary))
    return root


def test_publish_circle_zip_with_macro_edw_names_and_moscow_date(installation,tmp_path):
    root=installation;share=tmp_path/'share'
    old=(root/'result/poi_layers.zip').read_bytes()
    # UTC date changes to the next day in Moscow.
    report=publish_layers(root,{'directory':str(share),'region_column':'EDW'},now=datetime(2026,11,13,22,tzinfo=timezone.utc))
    target=Path(report['archive'])
    assert target.name=='Данные_из_OSM_по_интересующим_тегам_Выгрузка_на_2026_11_14_с_poi.zip'
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert 'Байкал_и_Дальний_Восток_(Благовещенск).kml' in archive.namelist()
        assert 'Центр_(Кемерово).kml' in archive.namelist()
        assert not any('points/' in name for name in archive.namelist())
        kml=archive.read('Байкал_и_Дальний_Восток_(Благовещенск).kml').decode()
        assert 'Байкал_и_Дальний_Восток_(Благовещенск)' in kml
        assert all(name.endswith('.kml') for name in archive.namelist())
        assert 'OpenStreetMap contributors' in kml
    assert report['status']=='KML_PUBLISH_SUCCESS' and report['circles']==1
    assert (root/'result/poi_layers.zip').read_bytes()==old
    assert not (root/'.work').exists() and not list(share.glob('*.part'))


def test_duplicate_edw_labels_keep_separate_subject_layers(installation,tmp_path):
    root=installation;path=root/'config/region_dictionary.json'
    dictionary=json.loads(path.read_text())
    for row in dictionary['regions']:
        row.update(EDW='Москва',MACROREGION_NAME='Москва и область')
    path.write_text(json.dumps(dictionary))
    report=publish_layers(root,{'directory':str(tmp_path/'share')})
    with zipfile.ZipFile(report['archive']) as archive:
        assert 'Москва_и_область_(Москва)_RU-AA.kml' in archive.namelist()
        assert 'Москва_и_область_(Москва)_RU-BB.kml' in archive.namelist()


def test_network_rename_failure_preserves_old_published_archive(installation,tmp_path,monkeypatch):
    root=installation;share=tmp_path/'share'
    now=datetime(2026,11,13,12,tzinfo=timezone.utc)
    first=publish_layers(root,{'directory':str(share)},now=now)
    archive=Path(first['archive']);before=archive.read_bytes()
    original=Path.replace
    def locked(self,target):
        if self.name.startswith('.poi-osm-'):raise PermissionError('network archive is open')
        return original(self,target)
    monkeypatch.setattr(Path,'replace',locked)
    with pytest.raises(PermissionError,match='network archive is open'):
        publish_layers(root,{'directory':str(share)},now=now)
    assert archive.read_bytes()==before
    assert not (root/'.work').exists() and not list(share.glob('*.part'))
    assert json.loads((root/'result/analysis/publication.json').read_text())['status']=='KML_PUBLISH_FAILED'


def test_kemerovo_correction_is_idempotent_and_preserves_confirmed_macro(tmp_path):
    path=tmp_path/'dictionary.json'
    record={'region_code':'RU-KEM','EDW':'Кемерово','CEM':'Тверь','REGION_NAME_POI':'Тверская область',
            'MACROREGION_ID':7,'MACROREGION_NAME':'Центр','OPERATION_MR_FOLDER':'Operations_Center','FLAG':1}
    path.write_text(json.dumps({'regions':[record]}))
    assert correct_known_kemerovo(path)
    updated=json.loads(path.read_text())['regions'][0]
    assert updated['CEM']=='Кемерово' and updated['REGION_NAME_POI']=='Кемеровская область'
    assert updated['MACROREGION_NAME']=='Центр' and updated['MACROREGION_ID']==7
    assert updated['OPERATION_MR_FOLDER']=='Operations_Center' and updated['FLAG']==1
    before=path.read_bytes()
    assert not correct_known_kemerovo(path) and path.read_bytes()==before


def test_local_settings_enable_automatic_publication_after_files_only(tmp_path,monkeypatch):
    from poi_osm.product import main
    machine=tmp_path/'local.json'
    machine.write_text(json.dumps({'base_dir':'.','source_mode':'download',
                                  'publication':{'enabled':True,'directory':'shared','region_column':'EDW'}}))
    calls=[]
    def generate(root,action,*args,**kwargs):
        if action=='portable':return {'status':'PRODUCT_PORTABLE'}
        assert action=='files'
        return {'status':'PRODUCT_SUCCESS','output':str(root/'result'),'archive':'local.zip',
                'total_objects':5,'unassigned_objects':0,'cleanup_complete':True,
                'kml_counts':{'points_exported':0,'circles':1}}
    def publish(root,options):
        calls.append((root,options))
        return {'archive':str(tmp_path/'shared/final.zip')}
    monkeypatch.setattr('poi_osm.product.execute',generate)
    monkeypatch.setattr('poi_osm.share_export.publish_layers',publish)
    assert main(['--local-config',str(machine),'--files'])==0
    assert len(calls)==1 and calls[0][0]==tmp_path/'result'
    assert calls[0][1]['directory']==str(tmp_path/'shared')
    assert main(['--local-config',str(machine),'--make-portable'])==0
    assert len(calls)==1
