import json
import os

import pytest
from helpers import confirm_collection, run_collection, write_json

from poi_osm.classification import CONFIG
from poi_osm.installation import local_settings, portable_product, proxy_environment
from poi_osm.product import execute, main


def test_move_installation_keeps_registry_contract_and_unchanged_status(collection,tmp_path):
    confirm_collection(collection)
    original=run_collection(collection,'--production')
    root=collection/'results'
    execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    product=json.loads((root/'product.json').read_text())
    assert product['version']==2
    assert product['region_dictionary']=='config/region_dictionary.json'
    settings=json.loads((root/'config/settings.json').read_text())
    assert settings['project_root']=='..' and settings['boundaries']=='boundaries/regions.geojson'
    assert (root/settings['tag_mapping']).read_bytes()==(collection/'config/tag_mapping.json').read_bytes()
    assert (root/'config/telecom.json').read_bytes()==(CONFIG/'telecom.json').read_bytes()
    boundary=(root/'boundaries/regions.geojson').read_bytes()
    home=tmp_path/'Другой компьютер'/'poi';home.parent.mkdir()
    collection.rename(home)
    moved=home/'results'
    machine=tmp_path/'machine.json'
    write_json(machine,{'base_dir':str(home),'product_folder':'results','input_folder':'inputs',
                        'source_mode':'local','proxy':{'enabled':False}})
    assert main(['--local-config',str(machine),'--files'])==0
    state=json.loads((moved/'result/.state/last_success.json').read_text())
    assert state['registry']==str(moved/'result/.state/registry.sqlite')
    assert state['contract_hash']==original['contract_hash']
    assert (moved/'boundaries/regions.geojson').read_bytes()==boundary
    assert main(['--local-config',str(machine)])==0
    report=json.loads((moved/'result/analysis/summary.json').read_text())
    assert report['metrics']['changes']=={'UNCHANGED':3}
    assert report['total_objects']==3
    assert not collection.exists()


def test_migrate_version_one_uses_exact_rule_bytes(collection):
    confirm_collection(collection);run_collection(collection,'--production')
    root=collection/'results';execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    product=json.loads((root/'product.json').read_text());product['version']=1
    product['region_dictionary']=str(CONFIG/'region_dictionary.json');write_json(root/'product.json',product)
    settings=json.loads((root/'config/settings.json').read_text());settings['project_root']=str(collection)
    settings['tag_mapping']=str(collection/'config/tag_mapping.json');write_json(root/'config/settings.json',settings)
    before=(root/'result/.state/registry.sqlite').read_bytes()
    result=portable_product(root)
    assert result['version']==2
    assert (root/'config/tag_mapping.json').read_bytes()==(collection/'config/tag_mapping.json').read_bytes()
    assert (root/'result/.state/registry.sqlite').read_bytes()==before
    portable_product(root)


def test_machine_mode_selects_download_and_proxy_without_persisting_environment(tmp_path,monkeypatch):
    machine=tmp_path/'local.json'
    write_json(machine,{'base_dir':'.','product_folder':'result','source_mode':'download',
                        'proxy':{'enabled':True,'url':'http://proxy.example:8080','windows_negotiate':True}})
    calls=[]
    def capture(root,action,*args,**kwargs):
        calls.append((root,action,kwargs,os.environ.get('HTTPS_PROXY')))
        return {'status':'PRODUCT_PORTABLE'}
    monkeypatch.setattr('poi_osm.product.execute',capture)
    monkeypatch.setenv('HTTPS_PROXY','http://original.example:8080')
    assert main(['--local-config',str(machine)])==0
    assert calls[0][0]==tmp_path/'result' and calls[0][1]=='download'
    assert calls[0][2]['windows_proxy'] is True and calls[0][3]=='http://proxy.example:8080'
    assert os.environ['HTTPS_PROXY']=='http://original.example:8080'
    main(['--local-config',str(machine),'--files'])
    assert calls[-1][1]=='files' and calls[-1][2]['windows_proxy'] is False


def test_disable_proxy_ignores_inherited_proxy_only_in_process(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','http://old-proxy.example:8080')
    with proxy_environment({'enabled':False}):
        assert 'HTTPS_PROXY' not in os.environ
        assert os.environ['NO_PROXY']=='*'
    assert os.environ['HTTPS_PROXY']=='http://old-proxy.example:8080'


@pytest.mark.parametrize('change',[{'base_dir':''},{'source_mode':'unknown'},{'product_folder':'../escape'},
                                   {'input_folder':'D:/outside'},{'proxy':{'enabled':'yes'}},
                                   {'proxy':{'enabled':True,'url':'http://user:password@proxy.example'}}])
def test_invalid_machine_settings_fail(tmp_path,change):
    path=tmp_path/'local.json';write_json(path,{'base_dir':'.',**change})
    with pytest.raises(ValueError):local_settings(path)


@pytest.mark.parametrize('changed',[False,True])
def test_stale_standard_paths_are_rebased_only_when_contract_matches(collection,changed):
    confirm_collection(collection);run_collection(collection,'--production')
    root=collection/'results';execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    product=json.loads((root/'product.json').read_text());product['version']=1;write_json(root/'product.json',product)
    settings=json.loads((root/'config/settings.json').read_text());settings['project_root']='Z:/missing-poi-project'
    for key,filename in [('tag_mapping','tag_mapping.json'),('categories','categories.json'),('telecom','telecom.json'),
                         ('chains','chains.json'),('primary_selection','primary_selection.json')]:
        settings[key]='Z:/missing-poi-project/config/'+filename
    write_json(root/'config/settings.json',settings)
    before=(root/'result/.state/registry.sqlite').read_bytes()
    if changed:
        write_json(root/'config/tag_mapping.json',{'bakery':['shop']})
        with pytest.raises(ValueError,match='cannot be safely rebased'):
            portable_product(root)
        assert json.loads((root/'product.json').read_text())['version']==1
    else:
        assert portable_product(root)['version']==2
    assert (root/'result/.state/registry.sqlite').read_bytes()==before
