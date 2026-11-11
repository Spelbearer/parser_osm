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
