"""Portable installation paths and one local machine configuration."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from .classification import CONFIG
from .config import contract_hash, load_settings
from .region_dictionary import correct_known_kemerovo
from .runtime import atomic_json
from .sources import _hash_file


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def copy_exact(source, target):
    source, target = Path(source), Path(target)
    if target.exists():
        if source.read_bytes() != target.read_bytes():
            raise ValueError(f'Portable configuration conflicts with existing file: {target}')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix+'.portable.tmp')
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def portable_product(root):
    root = Path(root).resolve()
    product = read(root/'product.json')
    if product.get('version') == 2:
        dictionary=root/product.get('region_dictionary','config/region_dictionary.json')
        if dictionary.is_file() and dictionary.resolve().is_relative_to(root): correct_known_kemerovo(dictionary)
        return product
    if product.get('version') != 1:
        raise ValueError('Unsupported product configuration version')
    # Prefer owned copies when absolute paths still point to another machine.
    for key in ('settings', 'collection_profile', 'export_profile', 'kml_config'):
        value = product[key]
        basename = PureWindowsPath(value).name
        local = root/'config'/basename
        source = local if local.is_file() else root/Path(value)
        if not source.is_file():
            raise ValueError(f'Missing installation configuration: {key}; restore {local}')
        copy_exact(source, local)
        product[key] = local.relative_to(root).as_posix()
    settings_path = root/product['settings']
    settings = read(settings_path)
    original_root = (settings_path.parent/settings.get('project_root', '..')).resolve()
    rebased_missing_rules = False
    rules = {'tag_mapping':'tag_mapping.json', 'categories':'categories.json', 'chains':'chains.json',
             'telecom':'telecom.json', 'primary_selection':'primary_selection.json'}
    for key, filename in rules.items():
        value = settings.get(key, 'config/tag_mapping.json' if key=='tag_mapping' else None)
        source = original_root/Path(value) if value else CONFIG/filename
        target = root/'config'/filename
        if not source.is_file():
            rebased_missing_rules = True
            if target.is_file():
                source = target
            elif value and PureWindowsPath(value).name != filename:
                raise ValueError(f'Custom {key} file missing; restore it to {target}')
            else:
                source = CONFIG/filename
        copy_exact(source, target)
        settings[key] = target.relative_to(root).as_posix()
    for key, filename in [('regions','regions.json'), ('boundaries','regions.geojson')]:
        target = root/'boundaries'/filename
        if not target.is_file():
            raise ValueError(f'Missing reviewed geography; restore {target}')
        settings[key] = target.relative_to(root).as_posix()
    value = product.get('region_dictionary')
    source = root/Path(value) if value else CONFIG/'region_dictionary.json'
    target = root/'config/region_dictionary.json'
    if not source.is_file():
        if target.is_file():
            source = target
        elif value and PureWindowsPath(value).name != 'region_dictionary.json':
            raise ValueError(f'Custom region dictionary missing; restore {target}')
        else:
            source = CONFIG/'region_dictionary.json'
    copy_exact(source, target)
    correct_known_kemerovo(target)
    product['region_dictionary'] = target.relative_to(root).as_posix()
    settings['project_root'] = '..'
    product['version'] = 2
    previous = root/'result/.state/last_success.json'
    if rebased_missing_rules and previous.is_file():
        # A stale path must not silently replace unavailable custom rule contents.
        with tempfile.TemporaryDirectory(prefix='portable-',dir=root) as folder:
            check = Path(folder)/'settings.json'
            atomic_json(check,{**settings,'project_root':str(root)})
            loaded = load_settings(check)
            signature = contract_hash(loaded,read(root/settings['regions']),_hash_file(root/settings['boundaries']))
            profile = read(root/product['collection_profile'])
            signature = hashlib.sha256((signature+json.dumps({'expected':profile['expected_extracts'],
                'excluded':profile.get('excluded_extracts',[])},sort_keys=True)).encode()).hexdigest()
            if signature != read(previous)['contract_hash']:
                raise ValueError('Missing old rule files cannot be safely rebased: restore the original dictionaries into installation config')
    # Both old/new configurations refer to identical bytes; interrupted migration is retryable.
    atomic_json(settings_path, settings)
    atomic_json(root/'product.json', product)
    return product


def product_config(root):
    product = read(Path(root)/'product.json')
    product['input_folder'] = str((Path(root)/product['input_folder']).resolve())
    return product


def local_settings(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError(f'Create {path.name} from local.example.json and set base_dir/source_mode, or pass --root')
    config = read(path)
    base = config.get('base_dir')
    if not isinstance(base, str) or not base.strip():
        raise ValueError('local.json base_dir must be a nonempty path')
    base = (path.parent/Path(base)).resolve()
    product_folder = config.get('product_folder', 'result')
    input_folder = config.get('input_folder', 'pbf')
    for field, value in [('product_folder',product_folder), ('input_folder',input_folder)]:
        if not isinstance(value, str) or not value or Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
            raise ValueError(f'{field} must be relative to base_dir')
        if not (base/value).resolve().is_relative_to(base):
            raise ValueError(f'{field} must stay within base_dir')
    mode = config.get('source_mode', 'files')
    if mode not in {'files','local','download'}:
        raise ValueError('source_mode must be files, local or download')
    proxy = config.get('proxy', {'enabled':False, 'url':'', 'windows_negotiate':False})
    if not isinstance(proxy, dict) or any(not isinstance(proxy.get(key,False),bool) for key in ('enabled','windows_negotiate')):
        raise ValueError('proxy enabled/windows_negotiate must be booleans')
    if proxy.get('enabled'):
        parsed = urlsplit(proxy.get('url') or '')
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('proxy.url must be a proxy URL without embedded credentials')
    from .share_export import settings as publication_settings
    publication=publication_settings(config.get('publication'),base)
    return {**config, 'publication':publication, 'root':base/product_folder, 'input':base/input_folder, 'source_mode':mode, 'proxy':proxy}


@contextmanager
def proxy_environment(config):
    keys = ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','http_proxy','https_proxy','all_proxy','no_proxy')
    original = {key:os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key,None)
        if config.get('enabled'):
            os.environ['HTTP_PROXY'] = os.environ['HTTPS_PROXY'] = config['url']
            os.environ['NO_PROXY'] = 'localhost,127.0.0.1'
        else:
            os.environ['NO_PROXY'] = '*'
        yield
    finally:
        for key,value in original.items():
            if value is None:
                os.environ.pop(key,None)
            else:
                os.environ[key] = value
