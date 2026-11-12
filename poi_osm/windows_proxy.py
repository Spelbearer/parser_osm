"""Windows SSPI proxy transport using the system curl, without password storage."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .runtime import atomic_json
from .sources import SourceError, _https

LOG = logging.getLogger(__name__)


def curl_executable():
    if sys.platform != 'win32':
        raise SourceError('--windows-proxy requires Windows and curl.exe with SSPI/SPNEGO support')
    executable = shutil.which('curl.exe')
    if not executable:
        raise SourceError('curl.exe is missing; install the approved Windows curl with SSPI support')
    try:
        version = subprocess.run([executable, '--disable', '--version'], capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise SourceError('Cannot check Windows curl capabilities') from error
    if not all(feature in version for feature in ('SSPI', 'SPNEGO', 'Schannel')):
        raise SourceError('Windows proxy mode requires curl with SSPI, SPNEGO and Schannel')
    return executable


class CurlResponse(requests.Response):
    def __init__(self, path, status, url, redirect):
        super().__init__()
        self.path = path
        self.status_code = status
        self.url = url
        self.reason = f'HTTP {status}'
        self.raw = path.open('rb')
        self.headers['Content-Length'] = str(path.stat().st_size)
        if redirect:
            self.headers['Location'] = redirect
        self.progress_reported = True

    def close(self):
        # Response.iter_content can mark the body consumed without closing a file.
        self.raw.close()
        super().close()
        self.path.unlink(missing_ok=True)


class WindowsProxySession:
    def __init__(self, folder):
        self.executable = curl_executable()
        self.folder = Path(folder)
        self.headers = {}
        self.responses = []
        self.proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
        parsed = urlsplit(self.proxy or '')
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise SourceError('Set HTTPS_PROXY to the proxy URL without username/password; Windows SSPI supplies credentials')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        for response in self.responses:
            response.close()

    def mark(self, pid):
        marker = self.folder.parent/'owner.json'
        if marker.is_file():
            document = json.loads(marker.read_text(encoding='utf-8'))
            document['child_pid'] = pid
            atomic_json(marker, document)

    def request(self, method, url, **kwargs):
        if method != 'GET':
            raise SourceError('Windows proxy transport only supports snapshot GET requests')
        url = _https(url)
        path = self.folder/('curl-'+uuid.uuid4().hex+'.response.part')
        connect_timeout, read_timeout = kwargs.get('timeout', (15, 120))
        args = [self.executable, '--disable', '--silent', '--show-error', '--proxy', self.proxy,
                '--proxy-negotiate', '--proxy-user', ':', '--noproxy', 'localhost',
                '--ssl-revoke-best-effort', '--proto', '=https', '--proto-redir', '=https',
                '--connect-timeout', str(connect_timeout), '--speed-limit', '1', '--speed-time', str(read_timeout),
                '--output', str(path), '--write-out', '%{http_code}\n%{url_effective}\n%{redirect_url}\n%{http_connect}\n']
        if urlsplit(url).path.endswith('.md5'):
            args.extend(['--max-filesize', '65536'])
        for key, value in {**self.headers, **kwargs.get('headers', {})}.items():
            args.extend(['--header', f'{key}: {value}'])
        args.append(url)
        process = None
        keep = False
        try:
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(args, stdout=output, stderr=errors)
                self.mark(process.pid)
                last_time = time.monotonic()
                last_size = 0
                while process.poll() is None:
                    size = path.stat().st_size if path.exists() else 0
                    if size-last_size >= 64*1024*1024 or time.monotonic()-last_time >= 15:
                        LOG.info('DOWNLOAD %s (Windows proxy): %.1f MiB', Path(urlsplit(url).path).name, size/(1024*1024))
                        last_time, last_size = time.monotonic(), size
                    time.sleep(0.2)
                output.seek(0)
                lines = output.read(65536).decode('utf-8', errors='replace').splitlines()
                if process.returncode:
                    if len(lines)>3 and lines[3] in {'403','407'}:
                        raise SourceError('Windows proxy CONNECT rejected: HTTP '+lines[3])
                    # Do not persist curl stderr: auth or proxy details may appear there.
                    reasons = {5:'proxy DNS failure', 6:'server DNS failure', 7:'connection failed',
                               18:'incomplete transfer', 23:'disk write failed', 28:'connection or transfer timed out', 35:'TLS handshake failed',
                               60:'certificate validation failed', 67:'proxy authentication failed'}
                    raise SourceError(f'Windows curl failed ({process.returncode}): {reasons.get(process.returncode, "transfer failed")}')
                if len(lines) < 2 or not lines[0].isdigit() or not 100 <= int(lines[0]) <= 599 or not path.is_file():
                    raise SourceError('Invalid Windows curl transfer result')
                response = CurlResponse(path, int(lines[0]), _https(lines[1]), lines[2] if len(lines)>2 else '')
                self.responses.append(response)
                keep = True
                return response
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait()
                if process.poll() is not None:
                    self.mark(None)
            if not keep:
                path.unlink(missing_ok=True)
