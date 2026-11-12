import hashlib
import json
from pathlib import Path

import pytest
from test_sources import make_pbf, today

from poi_osm.sources import SourceError, _request, obtain_source
from poi_osm.windows_proxy import WindowsProxySession


def curl_process(monkeypatch, folder, payloads, *, code=0, redirect=None, interrupt=False):
    calls=[]
    monkeypatch.setattr('poi_osm.windows_proxy.curl_executable',lambda:'curl.exe')
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.example:8080')
    monkeypatch.setattr('poi_osm.windows_proxy.time.sleep',lambda _:None)
    marker=folder.parent/'owner.json'
    marker.write_text(json.dumps({'root':str(folder.parent),'child_pid':None}))

    class Process:
        def __init__(self,args,stdout,stderr):
            self.pid=24680
            self.returncode=code
            self.running=interrupt
            calls.append(args)
            url=args[-1]
            body=payloads.get(url,b'')
            Path(args[args.index('--output')+1]).write_bytes(body)
            status=302 if redirect and len(calls)==1 else 200
            stdout.write(f'{status}\n{url}\n{redirect if status==302 else ""}\n'.encode());stdout.flush()
            stderr.write(b'private authentication detail must never appear in errors')
            self.polls=0

        def poll(self):
            self.polls+=1
            if self.running:
                return None
            return self.returncode

        def terminate(self):
            self.running=False
            self.returncode=-15

        def kill(self):
            self.running=False
            self.returncode=-9

        def wait(self,timeout=None):
            return self.returncode

    monkeypatch.setattr('poi_osm.windows_proxy.subprocess.Popen',Process)
    if interrupt:
        def stopped(_):
            raise KeyboardInterrupt
        monkeypatch.setattr('poi_osm.windows_proxy.time.sleep',stopped)
    return calls,marker


def test_sspi_transfer_keeps_source_validation_and_cleans_response_files(tmp_path,monkeypatch):
    folder=tmp_path/'downloads';folder.mkdir()
    data=make_pbf(tmp_path/'input.osm.pbf',today())
    url='https://example.org/source.osm.pbf'
    calls,marker=curl_process(monkeypatch,folder,{url:data,url+'.md5':hashlib.md5(data).hexdigest().encode()})
    source=obtain_source({'coverage_id':'test','sources':[{'url':url,'checksum_url':url+'.md5',
                         'coverage_id':'test','complete':False,'retries':1}]},folder,
                         session_factory=lambda:WindowsProxySession(folder))
    assert source['sha256']==hashlib.sha256(data).hexdigest()
    assert source['publisher_checksum']==hashlib.md5(data).hexdigest()
    assert not list(folder.glob('*.part'))
    assert json.loads(marker.read_text())['child_pid'] is None
    assert len(calls)==2
    for args in calls:
        assert args[:2]==['curl.exe','--disable']
        assert '--proxy-negotiate' in args and args[args.index('--proxy-user')+1]==':'
        assert '--ssl-revoke-best-effort' in args
        assert '--insecure' not in args and '-k' not in args
        assert args[args.index('--proto')+1]=='=https'
        assert '--location' not in args
    assert '--max-filesize' in calls[0]


def test_redirect_downgrade_is_rejected_before_following(tmp_path,monkeypatch):
    folder=tmp_path/'downloads';folder.mkdir()
    calls,_=curl_process(monkeypatch,folder,{},redirect='http://example.org/file.pbf')
    with WindowsProxySession(folder) as session:
        with pytest.raises(SourceError,match='HTTPS'):
            _request(session,'GET','https://example.org/first.pbf',timeout=(15,20))
    assert len(calls)==1 and not list(folder.glob('*.part'))


def test_curl_failure_never_leaks_stderr_and_cleans_temporary_files(tmp_path,monkeypatch):
    folder=tmp_path/'downloads';folder.mkdir()
    _,marker=curl_process(monkeypatch,folder,{},code=35)
    with WindowsProxySession(folder) as session:
        with pytest.raises(SourceError,match='TLS handshake failed') as error:
            session.request('GET','https://example.org/source.pbf')
    assert 'private authentication' not in str(error.value)
    assert not list(folder.glob('*.part'))
    assert json.loads(marker.read_text())['child_pid'] is None


def test_interrupted_curl_is_stopped_and_worker_marker_cleared(tmp_path,monkeypatch):
    folder=tmp_path/'downloads';folder.mkdir()
    _,marker=curl_process(monkeypatch,folder,{},interrupt=True)
    with WindowsProxySession(folder) as session:
        with pytest.raises(KeyboardInterrupt):
            session.request('GET','https://example.org/source.pbf')
    assert not list(folder.glob('*.part'))
    assert json.loads(marker.read_text())['child_pid'] is None


def test_proxy_credentials_in_url_are_not_accepted(tmp_path,monkeypatch):
    monkeypatch.setattr('poi_osm.windows_proxy.curl_executable',lambda:'curl.exe')
    monkeypatch.setenv('HTTPS_PROXY','http://username:secret@proxy.example:8080')
    with pytest.raises(SourceError,match='without username/password') as error:
        WindowsProxySession(tmp_path)
    assert 'secret' not in str(error.value)
