"""One production entry point, one current result, disposable working data."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import uuid
import zipfile
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .audit import audit
from .classification import CONFIG
from .collection import inspect_sources, profile_from
from .config import load_settings
from .geography import RegionIndex
from .kml import build as build_kml
from .objects import export_objects
from .region_dictionary import RegionDictionary
from .region_files import split_regions
from .runtime import atomic_json, configure_utf8, process_lock
from .sources import _hash_file
from .teradata_export import export as export_warehouse

LOG = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def is_link(path):
    if path.is_symlink(): return True
    try: return bool(getattr(path.lstat(),'st_file_attributes',0) & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0))
    except FileNotFoundError: return False


def remove_owned(path, root, protected=()):
    path,root = Path(path),Path(root).resolve()
    if not path.exists() and not is_link(path): return
    target = path.resolve()
    if target == root or not target.is_relative_to(root) or is_link(path):
        raise ValueError(f'Refusing cleanup outside owned output: {path}')
    for item in protected:
        item=Path(item).resolve()
        if item==target or item.is_relative_to(target): raise ValueError(f'Cleanup would remove protected input: {item}')
    if path.is_dir():
        for directory,dirs,files in os.walk(path,followlinks=False):
            if any(is_link(Path(directory)/name) for name in dirs+files):
                raise ValueError(f'Refusing cleanup of links/junctions: {path}')
        shutil.rmtree(path)
    else: path.unlink()


def cleanup_legacy(root, source):
    """Delete only recognised application artifacts after a valid current bundle exists."""
    root=Path(root); removed=[]; skipped=[]
    protected=[s['path'] for s in source.get('sources',[])]
    def delete(path):
        try:
            remove_owned(path,root,protected); removed.append(str(path.relative_to(root)))
        except (OSError,ValueError) as error:
            skipped.append({'path':str(path),'reason':str(error)})
    for family in ['runs','preview_runs','teradata','approved','work']:
        directory=root/family
        if not directory.is_dir() or is_link(directory): continue
        for item in list(directory.iterdir()):
            if not item.is_dir() or is_link(item):
                skipped.append({'path':str(item),'reason':'Unrecognised artifact'});continue
            known=False
            try:
                if family=='approved':
                    known=item.name.startswith('approved-') and (item/'approval.json').is_file() and read(item/'settings.json').get('boundaries_reviewed') is True
                else:
                    info=read(item/'summary.json')
                    known=bool(info.get('source_run') or info.get('sources') or info.get('run_id')) and info.get('status') in {
                        'SUCCESS','COLLECTION_PREVIEW_SUCCESS','COLLECTION_PREVIEW_INCOMPLETE','TERADATA_EXPORT_SUCCESS','RUNNING','FAILED','CANCELLED'}
            except (OSError,ValueError): pass
            if known: delete(item)
            else: skipped.append({'path':str(item),'reason':'Unrecognised artifact'})
        try: directory.rmdir()
        except OSError: pass
    cache=root/'cache'
    if cache.is_dir() and not is_link(cache):
        for item in list(cache.iterdir()):
            if item.is_dir() and re.fullmatch(r'[0-9a-f]{64}',item.name): delete(item)
            else: skipped.append({'path':str(item),'reason':'Unrecognised cache entry'})
        try: cache.rmdir()
        except OSError: pass
    kml=root/'kml'
    if (kml/'last_success.json').is_file():
        try:
            if read(kml/'last_success.json').get('status')=='KML_EXPORT_SUCCESS':
                allowed=all(p.name in {'runs','work','last_success.json','last_failure.json'} or re.fullmatch(r'poi_layers-[0-9T-]+[a-f0-9]+\.zip',p.name) for p in kml.iterdir())
                if allowed: delete(kml)
                else: skipped.append({'path':str(kml),'reason':'Contains unrecognised files; preserved'})
        except (OSError,ValueError): pass
    checks=root/'checks'
    if checks.is_dir() and not is_link(checks):
        for item in list(checks.iterdir()):
            known=False
            try:
                if item.is_file() and re.fullmatch(r'(audit|boundary-diagnostics)-[0-9T-]+[a-f0-9]+\.json',item.name):
                    doc=read(item);known='run' in doc
                elif item.is_dir() and item.name.startswith('boundary-recovery-'):
                    known=read(item/'report.json').get('status') in {'RECOVERED_CANDIDATES','RECOVERY_INCOMPLETE'}
            except (OSError,ValueError): pass
            if known: delete(item)
            else: skipped.append({'path':str(item),'reason':'Unrecognised diagnostic file'})
        try: checks.rmdir()
        except OSError: pass
    # The lock itself is removed only after the legacy lock handle has closed.
    state=root/'state'
    if state.is_dir() and set(p.name for p in state.iterdir()) <= {'last_success.json','process.lock'}:
        delete(state)
    return {'removed':removed,'skipped':skipped}


def cleanup_project(project, source):
    project=Path(project).resolve(); result={'removed':[],'skipped':[]}
    protected=[s['path'] for s in source.get('sources',[])]
    def delete(path):
        try:
            remove_owned(path,project,protected);result['removed'].append(str(path))
        except (OSError,ValueError) as error: result['skipped'].append({'path':str(path),'reason':str(error)})
    demo=project/'demo'
    try:
        if (demo/'settings.json').is_file():
            settings=read(demo/'settings.json')
            if settings.get('coverage_id')=='demo' and settings.get('input',{}).get('path')=='example.osm.pbf' and (demo/'example.osm.pbf').stat().st_size<1000000:
                delete(demo)
    except (OSError,ValueError) as error: result['skipped'].append({'path':str(demo),'reason':str(error)})
    previews=project/'preview'
    if previews.is_dir() and not is_link(previews):
        for child in list(previews.iterdir()):
            try:
                document=read(child/'summary.json')
                if document.get('preview_only') is True and document.get('status') in {'PREVIEW_SUCCESS','PREVIEW_INCOMPLETE','FAILED','CANCELLED'}:
                    delete(child)
                else: result['skipped'].append({'path':str(child),'reason':'Not a completed owned preview'})
            except (OSError,ValueError): result['skipped'].append({'path':str(child),'reason':'Unrecognised preview artifact'})
        try: previews.rmdir()
        except OSError: pass
    for relative in ['__pycache__','poi_osm/__pycache__','tests/__pycache__','scripts/__pycache__']:
        path=project/relative
        if path.exists(): delete(path)
    pytest_cache=project/'.pytest_cache'
    if (pytest_cache/'CACHEDIR.TAG').is_file(): delete(pytest_cache)
    return result


def recover_swap(root):
    journal=root/'.swap.json'
    if not journal.exists(): return
    doc=read(journal); backup=Path(doc['backup'])
    if not backup.resolve().is_relative_to(root/'.work'): raise ValueError('Invalid publication journal')
    current=root/'result'
    if not current.exists() and backup.exists(): backup.replace(current)
    elif current.exists() and backup.exists():
        if read(current/'analysis/summary.json').get('product_run_id')!=doc['run_id']:
            raise ValueError('Publication needs recovery; previous registry kept in '+str(backup))
        remove_owned(backup,root)
    journal.unlink()


def publish(bundle, root, work, run_id):
    current=root/'result'; backup=work/'previous-result'
    atomic_json(root/'.swap.json',{'run_id':run_id,'backup':str(backup)})
    try:
        if current.exists(): current.replace(backup)
        bundle.replace(current)
    except BaseException:
        if backup.exists(): backup.replace(current)
        (root/'.swap.json').unlink(missing_ok=True)
        raise
    if backup.exists(): remove_owned(backup,root)
    (root/'.swap.json').unlink()


def configure_product(root, settings_path, profile_path, boundary, input_folder):
    settings=load_settings(Path(settings_path))
    profile=profile_from(Path(profile_path))
    if profile['coverage_id']!=settings['coverage_id']:
        raise ValueError('Collection profile and geographic coverage differ')
    RegionIndex(Path(boundary),Path(settings['regions']))
    for folder in ['config','boundaries']:
        if (root/folder).exists(): raise ValueError(f'{root/folder} already exists without product.json; preserve it and choose another output root')
    (root/'config').mkdir();(root/'boundaries').mkdir()
    shutil.copyfile(boundary,root/'boundaries/regions.geojson')
    shutil.copyfile(settings['regions'],root/'boundaries/regions.json')
    allowed={'project_root','coverage_id','coverage_confirmed','boundaries_reviewed','tag_mapping','regions',
             'boundaries','name_priority','line_policy','batch_size','quality','categories','chains','telecom','primary_selection'}
    document={k:v for k,v in read(settings_path).items() if k in allowed}
    document.update(project_root=settings['root'],regions=str(root/'boundaries/regions.json'),
                    boundaries=str(root/'boundaries/regions.geojson'),coverage_confirmed=True,boundaries_reviewed=True)
    atomic_json(root/'config/settings.json',document)
    profile['complete_snapshot_confirmed']=True
    atomic_json(root/'config/collection.json',profile)
    shutil.copyfile(CONFIG/'teradata_export.json',root/'config/teradata_export.json')
    shutil.copyfile(CONFIG/'kml.json',root/'config/kml.json')
    config={'version':1,'input_folder':str(Path(input_folder).resolve()),'settings':'config/settings.json',
            'collection_profile':'config/collection.json','export_profile':'config/teradata_export.json',
            'kml_config':'config/kml.json','region_dictionary':str(CONFIG/'region_dictionary.json'),'keep_results':1,'logs_to_keep':10}
    atomic_json(root/'product.json',config)
    return config


def initialise(root, source, settings_path=None, profile_path=None):
    old=read(source/'summary.json')
    boundary=source/'approved_boundaries.geojson'
    if not boundary.is_file(): boundary=Path(old['boundary_report']['source'])
    original=Path(settings_path) if settings_path else Path(old['boundary_report']['source']).parent/'settings.json'
    profile=Path(profile_path) if profile_path else original.parent/'collection.json'
    parents=[str(Path(s['path']).resolve().parent) for s in old['sources']]
    return configure_product(root,original,profile,boundary,os.path.commonpath(parents))


def copy_registry(source, destination):
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True)) as old:
        with closing(sqlite3.connect(destination)) as new:
            old.backup(new)
            new.execute('DROP TABLE IF EXISTS overlap_objects');new.commit()
            new.execute('VACUUM')
            if new.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('Registry copy verification failed')


def repack(source, destination, report):
    with zipfile.ZipFile(source) as old:
        if old.testzip() is not None: raise ValueError('Source ZIP CRC check failed')
        expected={f['file'] for f in report['files']}|{'summary.json','config.json','regions_index.csv'}
        if set(old.namelist())!=expected: raise ValueError('Unexpected ZIP contents')
        with zipfile.ZipFile(destination,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as new:
            for name in old.namelist():
                if name=='summary.json': new.writestr(name,json.dumps(report,ensure_ascii=False,indent=2))
                else:
                    with old.open(name) as incoming,new.open(name,'w',force_zip64=True) as outgoing:
                        shutil.copyfileobj(incoming,outgoing,1024*1024)
    with zipfile.ZipFile(destination) as saved:
        if saved.testzip() is not None: raise ValueError('Published ZIP verification failed')


def render(registry, manifest, root, work, config, run_id, review=None, reuse=None):
    settings=load_settings(root/config['settings'])
    regions=read(settings['regions'])
    source=work/'source';source.mkdir()
    LOG.info('Preparing exports from the saved registry')
    with closing(sqlite3.connect(Path(registry).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        objects=export_objects(db,source,selection_rules=settings['primary_rules'],parquet=False)
    dictionary=RegionDictionary(root/config['region_dictionary'] if config.get('region_dictionary') else CONFIG/'region_dictionary.json',regions)
    region_mapping=dictionary.apply_csv(source/'poi_teradata.csv')
    display_regions=dictionary.registry(regions)
    regional=split_regions(source/'poi_teradata.csv',source/'regions',display_regions)
    source_info={**manifest,'status':'SUCCESS','production':True,'boundaries_reviewed':True,'complete_coverage_verified':True,
                 'object_export':objects,'regional_export':regional,'region_mapping':region_mapping}
    atomic_json(source/'summary.json',source_info)
    atomic_json(source/'approved_regions.json',display_regions)
    _,checks=audit(source,work/'checks')
    if not checks['file_checks_ok']: raise ValueError('Source audit failed')
    warehouse,export=export_warehouse(source,work/'warehouse',root/config['export_profile'])
    bundle=work/'bundle';bundle.mkdir();(bundle/'analysis').mkdir();(bundle/'.state').mkdir()
    for name in ['poi_teradata.csv','poi_teradata.parquet','regions']:
        shutil.move(str(warehouse/name),str(bundle/name))
    shutil.copyfile(warehouse/'regions_index.csv',bundle/'analysis/regions_index.csv')
    shutil.copyfile(warehouse/'profile.json',bundle/'analysis/export_profile.json')
    dictionary.write_index(bundle/'analysis/region_mapping.csv',regions)
    if review and Path(review).is_file(): shutil.copyfile(review,bundle/'analysis/review.csv')
    checks['run']=str(root/'result/.state/registry.sqlite');atomic_json(bundle/'analysis/audit.json',checks)
    final_registry=root/'result/.state/registry.sqlite'
    kml_config=read(root/config['kml_config']); kml_report=None; archive=None
    if reuse and Path(reuse).is_file():
        try:
            candidate=read(reuse)
            dictionary_matches=(not region_mapping['applicable_regions'] or (candidate.get('region_mapping') or {}).get('dictionary_sha256')==region_mapping['dictionary_sha256'])
            if dictionary_matches and candidate.get('source_csv_sha256')==_hash_file(source/'poi_teradata.csv') and candidate.get('config')==kml_config and candidate.get('status')=='KML_EXPORT_SUCCESS':
                archive=Path(candidate['archive'])
                if archive.is_file():
                    with zipfile.ZipFile(archive) as existing:
                        archived=json.loads(existing.read('summary.json'))
                        archived_dictionary_matches=(not region_mapping['applicable_regions'] or (archived.get('region_mapping') or {}).get('dictionary_sha256')==region_mapping['dictionary_sha256'])
                        if archived_dictionary_matches and existing.testzip() is None and archived.get('source_csv_sha256')==candidate['source_csv_sha256'] and archived.get('config')==kml_config:
                            kml_report=archived
        except (OSError,ValueError,KeyError,RuntimeError,zipfile.BadZipFile):
            LOG.warning('Existing KML ZIP cannot be reused; rebuilding it from the registry')
    if kml_report is None:
        LOG.info('Building KML and ZIP')
        kml_report=build_kml(source,work/'kml',root/config['kml_config'],source_reference=final_registry)
        archive=Path(kml_report['archive'])
    else: LOG.info('Reusing the verified KML archive without recalculating circles')
    kml_report={**kml_report,'source_run':str(final_registry),'output':str(root/'result/analysis'),
                'archive':str(root/'result/poi_layers.zip'),'layers_kept':False,'layer_paths':'ZIP entries','region_mapping':region_mapping}
    repack(archive,bundle/'poi_layers.zip',kml_report)
    atomic_json(bundle/'analysis/kml_summary.json',kml_report)
    copy_registry(registry,bundle/'.state/registry.sqlite')
    state={key:manifest[key] for key in ['contract_hash','source_date','metrics','sources']}
    state.update(status='SUCCESS',output=str(root/'result'),registry=str(final_registry),
                 boundaries_reviewed=True,complete_coverage_verified=True,production=True,unresolved_objects=0,
                 boundary_report={'candidate_region_count':len(regions['regions']),'missing_region_codes':[],
                                  'conflicting_region_codes':[],'invalid_boundary_codes':{},'review_required':False})
    atomic_json(bundle/'.state/last_success.json',state)
    report={'status':'PRODUCT_SUCCESS','version':__version__,'product_run_id':run_id,'source_date':manifest['source_date'],
            'output':str(root/'result'),'archive':str(root/'result/poi_layers.zip'),
            'total_objects':export['total_rows'],'columns':export['columns'],'kml_counts':kml_report['counts'],
            'unassigned_objects':export['regional_export']['by_region'].get('',0),'file_checks_ok':True,
            'production_ready':True,'keep_results':1,'temporary_files_removed':False,
            'metrics':manifest['metrics'],'source_files':len(manifest['sources']),'region_mapping':region_mapping}
    atomic_json(bundle/'analysis/summary.json',report)
    return bundle,report


def run_collection(root, work, config):
    transient=work/'collection';(transient/'state').mkdir(parents=True)
    previous=root/'result/.state/last_success.json'
    if previous.exists(): atomic_json(transient/'state/last_success.json',read(previous))
    args=[sys.executable,'-m','poi_osm.collection',config['input_folder'],'--output',str(transient),
          '--config',str(root/config['settings']),'--profile',str(root/config['collection_profile']),'--production','--registry-only']
    LOG.info('Extracting the current PBF snapshot; temporary caches will be removed after completion')
    with (work/'collection-result.json').open('w',encoding='utf-8') as saved:
        process=subprocess.Popen(args,cwd=REPO,stdout=saved,stderr=subprocess.PIPE,text=True,encoding='utf-8',
                                 start_new_session=os.name!='nt',creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
        marker=read(work/'owner.json');marker['child_pid']=process.pid;atomic_json(work/'owner.json',marker)
        try:
            for line in process.stderr:
                LOG.info('%s',line.rstrip().split(' | ',2)[-1])
            code=process.wait()
            if code: raise RuntimeError(f'Collection failed with exit code {code}; see product log')
        except BaseException:
            if process.poll() is None:
                if os.name=='nt': process.send_signal(signal.CTRL_BREAK_EVENT)
                else: os.killpg(process.pid,signal.SIGTERM)
                try: process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    if os.name=='nt': subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
                    else: os.killpg(process.pid,signal.SIGKILL)
                    process.wait()
            raise
        finally: process.stderr.close()
    return read(work/'collection-result.json')


def alive(pid):
    if not pid: return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle: return ctypes.get_last_error()==5
        try:
            code=wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle,ctypes.byref(code)): return True
            return code.value==259
        finally: kernel.CloseHandle(handle)
    try: os.kill(pid,0);return True
    except ProcessLookupError: return False
    except PermissionError: return True


def validate_product(root, config):
    if config.get('keep_results')!=1: raise ValueError('This product profile keeps only the latest successful result')
    limit=config.get('logs_to_keep',10)
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=100:
        raise ValueError('logs_to_keep must be between 1 and 100')
    inputs=Path(config['input_folder']).resolve()
    if inputs.is_relative_to(root) or root.is_relative_to(inputs):
        raise ValueError('PBF inputs and product outputs must be separate directories')
    current=root/'result'
    if current.exists() and not (current/'.state/registry.sqlite').is_file():
        raise ValueError('Previous registry is missing; restore result/.state before updating')
    settings=load_settings(root/config['settings'])
    if not (settings.get('coverage_confirmed') is True and settings.get('boundaries_reviewed') is True):
        raise ValueError('Production needs the previously reviewed boundary configuration')
    RegionIndex(Path(settings['boundaries']),Path(settings['regions']))
    RegionDictionary(root/config['region_dictionary'] if config.get('region_dictionary') else CONFIG/'region_dictionary.json',read(settings['regions']))
    return limit


def execute(root, action, settings_path=None, profile_path=None, *, input_folder=None, boundary=None):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    (root/'logs').mkdir(exist_ok=True)
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
    logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s',
                        handlers=[logging.StreamHandler(),logging.FileHandler(root/'logs'/f'product-{run_id}.log',encoding='utf-8')],force=True)
    with process_lock(root/'.product.lock'), ExitStack() as legacy_locks:
        recover_swap(root)
        if action!='adopt' and (root/'state').exists():
            legacy_locks.enter_context(process_lock(root/'state/process.lock'))
        logs_limit=validate_product(root,read(root/'product.json')) if (root/'product.json').exists() else 10
        old_work=root/'.work'
        if old_work.exists():
            for item in old_work.iterdir():
                if not (item/'owner.json').is_file() or read(item/'owner.json').get('root')!=str(root):
                    raise ValueError('Unrecognised working directory: '+str(item))
                if alive(read(item/'owner.json').get('child_pid')): raise RuntimeError('Previous collection child is still running')
                remove_owned(item,root)
        work=root/'.work'/run_id;work.mkdir(parents=True)
        atomic_json(work/'owner.json',{'root':str(root),'child_pid':None})
        report=None
        try:
            if action=='init':
                if (root/'product.json').exists(): raise ValueError('Already initialized; run without --init')
                if input_folder is None or boundary is None: raise ValueError('--init requires --input and --boundaries')
                settings_path=settings_path or CONFIG/'settings.json'
                profile_path=profile_path or CONFIG/'collection.json'
                # Read-only source verification runs before configuration is saved.
                inspect_sources(Path(input_folder),profile_from(Path(profile_path)))
                config=configure_product(root,settings_path,profile_path,boundary,input_folder)
                logs_limit=validate_product(root,config)
                LOG.info('Configuration initialized; run the same command without --init to process PBF')
                return {'status':'PRODUCT_INITIALIZED','version':__version__,'root':str(root),'input_folder':config['input_folder']}
            if action=='adopt':
                if (root/'product.json').exists() and (root/'result').exists():
                    raise ValueError('Already migrated; use --files or a normal run')
                legacy_locks.enter_context(process_lock(root/'state/process.lock'))
                legacy=read(root/'state/last_success.json')
                source=Path(legacy['output']).resolve()
                if legacy.get('status')!='SUCCESS' or not legacy.get('boundaries_reviewed') or not source.is_relative_to(root/'runs'):
                    raise ValueError('A reviewed successful production run is required for migration')
                if (root/'product.json').exists():
                    if (root/'result').exists(): raise ValueError('Already migrated; use --files or a normal run')
                    config=read(root/'product.json')
                else: config=initialise(root,source,settings_path,profile_path)
                manifest=read(source/'summary.json');registry=Path(legacy['registry'])
                if manifest.get('status')!='SUCCESS' or not manifest.get('boundaries_reviewed') or manifest.get('registry')!=legacy['registry']:
                    raise ValueError('Legacy registry and successful manifest disagree; migration refused')
                review=source/'review.csv';reuse=root/'kml/last_success.json'
            else:
                if not (root/'product.json').exists(): raise ValueError('First use --adopt with the existing reviewed production result')
                config=read(root/'product.json')
                logs_limit=validate_product(root,config)
                if action=='files':
                    if not (root/'result/.state/last_success.json').exists():
                        raise ValueError('No saved registry yet; run without --files first')
                    manifest=read(root/'result/.state/last_success.json');registry=Path(manifest['registry'])
                    review=root/'result/analysis/review.csv';reuse=root/'result/analysis/kml_summary.json'
                else:
                    manifest=run_collection(root,work,config);registry=Path(manifest['registry'])
                    review=Path(manifest['output'])/'review.csv';reuse=None
            logs_limit=validate_product(root,config)
            bundle,report=render(registry,manifest,root,work,config,run_id,review,reuse)
            publish(bundle,root,work,run_id)
            legacy_locks.close()
            cleanup=cleanup_legacy(root,manifest)
            if action!='adopt': cleanup['removed'].extend(['working files','unpacked KML','previous result'])
            project_cleanup=cleanup_project(load_settings(root/config['settings'])['root'],manifest)
            cleanup['removed'].extend(project_cleanup['removed'])
            cleanup['skipped'].extend(project_cleanup['skipped'])
            atomic_json(root/'result/analysis/cleanup.json',cleanup)
            report['cleanup']=cleanup
            report['cleanup_complete']=not cleanup['skipped']
            atomic_json(root/'result/analysis/summary.json',report)
        except BaseException as error:
            atomic_json(root/'logs/last_failure.json',{'status':'FAILED','action':action,'error':str(error),'run_id':run_id})
            LOG.exception('Failed; last successful result preserved')
            raise
        finally:
            legacy_locks.close()
            # A failed rollback journal protects the only surviving registry.
            if not (root/'.swap.json').exists() and work.exists(): remove_owned(work,root)
            if report and (root/'result/analysis/summary.json').exists() and read(root/'result/analysis/summary.json').get('product_run_id')==run_id:
                report['temporary_files_removed']=not work.exists()
                if (root/'.swap.json').exists(): report['cleanup_complete']=False
                atomic_json(root/'result/analysis/summary.json',report)
            try: (root/'.work').rmdir()
            except OSError: pass
            logs=sorted([p for p in (root/'logs').glob('*.log') if re.fullmatch(r'(product-)?[0-9T-]+[a-f0-9]+\.log',p.name)],key=lambda p:p.stat().st_mtime,reverse=True)
            others=[path for path in logs if path.name!=f'product-{run_id}.log']
            for path in others[logs_limit-1:]: path.unlink()
        LOG.info('SUCCESS: %s objects; ZIP=%s',report['total_objects'],report['archive'])
        return report


def main(argv=None):
    configure_utf8()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',action='version',version=f'%(prog)s {__version__}')
    parser.add_argument('--root',required=True,type=Path,help='Output root, e.g. D:\\osm_results')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--init',action='store_true',help='Initialize a new installation using previously reviewed boundaries')
    group.add_argument('--adopt',action='store_true',help='Migrate a reviewed successful legacy result and remove recognised legacy artifacts')
    group.add_argument('--files',action='store_true',help='Regenerate CSV/Parquet/KML from the saved registry, without PBF')
    parser.add_argument('--input',type=Path,help='Local folder of all federal-district PBFs for --init')
    parser.add_argument('--boundaries',type=Path,help='Previously reviewed complete GeoJSON for --init')
    parser.add_argument('--settings',type=Path,help='Settings template for initialization or legacy migration')
    parser.add_argument('--profile',type=Path,help='Collection profile for initialization or legacy migration')
    args=parser.parse_args(argv)
    if not args.init and (args.input is not None or args.boundaries is not None):
        parser.error('--input and --boundaries are only valid with --init; edit product.json for an existing installation')
    if args.init and (args.input is None or args.boundaries is None): parser.error('--init requires --input and --boundaries')
    report=execute(args.root,'init' if args.init else 'adopt' if args.adopt else 'files' if args.files else 'run',args.settings,args.profile,input_folder=args.input,boundary=args.boundaries)
    if report['status']=='PRODUCT_INITIALIZED':
        print(json.dumps(report,ensure_ascii=False,indent=2));return 0
    brief={key:report[key] for key in ['status','output','archive','total_objects','unassigned_objects','cleanup_complete']}
    brief['kml_points']=report['kml_counts']['points_exported']
    brief['circles']=report['kml_counts']['circles']
    brief['summary']=str(Path(args.root).resolve()/'result/analysis/summary.json')
    print(json.dumps(brief,ensure_ascii=False,indent=2))
    return 0
