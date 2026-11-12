"""Native-style sharing violations must end with the extraction child process."""
import json
import os
from pathlib import Path

import pytest
from helpers import invoke_collection

from poi_osm.runtime import cleanup_index


def test_shared_index_does_not_block_collection_or_cache_reuse(collection,tmp_path,monkeypatch):
    moved=collection.with_name('проект');collection.rename(moved);collection=moved
    hooks=tmp_path/'hooks';hooks.mkdir()
    (hooks/'sitecustomize.py').write_text('''
from pathlib import Path
import sys
original=Path.unlink
def unlink(self,*args,**kwargs):
    if '--job' in sys.argv and self.suffix=='.idx':
        error=PermissionError('simulated Windows sharing violation')
        error.winerror=32
        raise error
    return original(self,*args,**kwargs)
Path.unlink=unlink
''',encoding='utf-8')
    repo=Path(__file__).resolve().parents[1]
    monkeypatch.setenv('PYTHONPATH',os.pathsep.join([str(hooks),str(repo)]))
    monkeypatch.setenv('PYTHONIOENCODING','cp1251')
    result=invoke_collection(collection)
    assert result.returncode==0,result.stderr
    assert 'cleanup deferred' in result.stderr
    first=json.loads(result.stdout)
    assert first['metrics']['unique_objects']==3
    assert not list((collection/'results').rglob('*.idx'))
    second=invoke_collection(collection)
    assert second.returncode==0,second.stderr
    assert json.loads(second.stdout)['cached_extracts']==2
    assert not list((collection/'results').rglob('*.idx'))


def test_cleanup_does_not_suppress_real_permission_errors(tmp_path,monkeypatch):
    def denied(*args,**kwargs): raise PermissionError('Access denied')
    monkeypatch.setattr(Path,'unlink',denied)
    with pytest.raises(PermissionError,match='Access denied'):
        cleanup_index(tmp_path/'example.idx')


@pytest.mark.skipif(os.name!='nt',reason='Windows native file sharing semantics')
def test_export_child_exit_releases_native_handle_before_publication(collection,tmp_path,monkeypatch):
    from helpers import confirm_collection, run_collection

    from poi_osm.product import alive, execute
    confirm_collection(collection)
    run_collection(collection,'--production')
    root=collection/'results'
    execute(root,'adopt',collection/'config/settings.json',collection/'profile.json')
    hooks=tmp_path/'export-hooks';hooks.mkdir()
    (hooks/'sitecustomize.py').write_text('''
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
import poi_osm.product as product
original = product.render

def render(*args, **kwargs):
    result = original(*args, **kwargs)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(kwargs['registry']), 0x80000000, 3, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    # Deliberately leave the handle without FILE_SHARE_DELETE open until process exit.
    Path(__file__).with_name('native-handle.json').write_text(json.dumps({'pid':os.getpid()}))
    return result

product.render = render
''',encoding='utf-8')
    repo=Path(__file__).resolve().parents[1]
    monkeypatch.setenv('PYTHONPATH',os.pathsep.join([str(hooks),str(repo)]))
    report=execute(root,'files')
    child=json.loads((hooks/'native-handle.json').read_text())
    assert not alive(child['pid'])
    assert report['status']=='PRODUCT_SUCCESS'
    assert (root/'result/.state/registry.sqlite').is_file()
    assert not (root/'.work').exists()
