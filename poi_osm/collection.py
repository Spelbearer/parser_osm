"""Checkpointed processing of a complete local set of federal-district PBFs."""
from __future__ import annotations

import argparse
import csv
import hashlib
from importlib.metadata import version
from itertools import groupby
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .classification import CONFIG
from .collection_boundaries import combine_boundaries
from .config import contract_hash, load_settings
from .geography import RegionIndex
from .region_files import split_regions
from .runtime import atomic_json, process_lock, cleanup_index
from .sources import obtain_source, _hash_file
from .storage import FIELDS, Registry, check_quality

REPO=Path(__file__).resolve().parents[1]


def profile_from(path):
    profile=json.loads(Path(path).read_text(encoding='utf-8'))
    expected=profile.get('expected_extracts')
    excluded=profile.get('excluded_extracts',[])
    if (not isinstance(expected,list) or not expected
            or any(not isinstance(v,str) or not re.fullmatch(r'[a-z0-9-]+',v) for v in expected) or len(expected)!=len(set(expected))
            or not isinstance(excluded,list) or any(not isinstance(v,str) for v in excluded)
            or set(expected)&set(excluded)):
        raise ValueError('Invalid collection extract lists')
    if not isinstance(profile.get('complete_snapshot_confirmed',False),bool):
        raise ValueError('complete_snapshot_confirmed must be boolean')
    size=profile.get('max_rows_per_region_file',50000)
    if not isinstance(size,int) or isinstance(size,bool) or not 1<=size<=1000000:
        raise ValueError('max_rows_per_region_file must be 1..1000000')
    return profile


def inspect_sources(folder,profile,source_date=None):
    folder=Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError(f'Input directory missing: {folder}')
    found={};excluded=[]
    for path in sorted(folder.rglob('*.osm.pbf')):
        match=re.fullmatch(r'(.+)-(?:[0-9]{6}|latest)\.osm\.pbf',path.name,re.I)
        stem=match.group(1).lower() if match else None
        if stem in profile.get('excluded_extracts',[]):
            excluded.append(str(path));continue
        if stem not in profile['expected_extracts']:
            raise ValueError(f'Unexpected PBF {path.name}; keep only the agreed extract set in this directory')
        if stem in found:
            raise ValueError(f'Multiple snapshots for {stem}; choose one file per extract')
        found[stem]=path
    missing=set(profile['expected_extracts'])-found.keys()
    if missing:
        raise ValueError(f'Missing extracts: {sorted(missing)}')
    sources=[];digests=set()
    for stem,path in sorted(found.items(),key=lambda item:item[1].stat().st_size):
        logging.info('CHECK %s (%s bytes)',path.name,path.stat().st_size)
        local={'path':str(path),'coverage_id':'collection-part','complete':False,'max_age_days':profile.get('max_age_days',45)}
        if source_date:
            local['source_date']=source_date
        source=obtain_source({'input':local},folder)
        import osmium
        with osmium.io.Reader(str(path)) as reader:
            timestamp=reader.header().get('osmosis_replication_timestamp') or None
        if source['sha256'] in digests:
            raise ValueError('Two different extracts have identical contents; verify downloaded files')
        digests.add(source['sha256'])
        checksum=Path(str(path)+'.md5')
        if checksum.exists():
            if checksum.stat().st_size>65536:
                raise ValueError('MD5 sidecar unexpectedly large')
            text=checksum.read_text(encoding='ascii').strip()
            match=re.fullmatch(r'([a-fA-F0-9]{32})(?:\s+\*?[^\r\n]+)?',text)
            if not match:
                raise ValueError(f'Invalid MD5 sidecar: {checksum.name}')
            digest=hashlib.md5()
            with path.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
            if digest.hexdigest()!=match.group(1).lower():
                raise ValueError(f'MD5 mismatch: {path.name}')
        sources.append({**{k:str(v) if isinstance(v,Path) else v for k,v in source.items()},
                        'extract':stem,'max_age_days':profile.get('max_age_days',45),'publisher_md5_checked':checksum.exists(),
                        'replication_timestamp':timestamp})
    dates={s['source_date'] for s in sources}
    if len(dates)!=1:
        raise ValueError(f'Extract snapshot dates differ: {sorted(dates)}; use one agreed snapshot date')
    timestamps={s['replication_timestamp'] for s in sources if s['replication_timestamp']}
    if len(timestamps)>1:
        raise ValueError(f'Extract replication timestamps differ: {sorted(timestamps)}; obtain a consistent snapshot set')
    return sources,excluded


def worker_contract(settings):
    code={name:_hash_file(Path(__file__).parent/name) for name in ['collection_worker.py','extract.py','geography.py','collection_boundaries.py','classification.py']}
    payload={k:settings[k] for k in ['mapping','name_priority','category_rules','chain_rules','telecom_rules']}
    payload.update(code=code,fields=FIELDS,line_policy=settings.get('line_policy','midpoint'),
                   toolchain={k:version(k) for k in ['osmium','shapely']},python=list(sys.version_info[:2]))
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def worker_for(source,settings,config,output):
    key=hashlib.sha256((source['sha256']+Path(source['path']).name+worker_contract(settings)).encode()).hexdigest()
    cache=output/'cache'/key
    pointer=cache/'current.json'
    if pointer.exists():
        try:
            result=json.loads(pointer.read_text(encoding='utf-8'))
            job=Path(result['job'])
            valid=_hash_file(job/'registry.sqlite')==result['registry_sha256']
            if result['candidate_sha256']:
                valid=valid and _hash_file(job/'candidate.geojson')==result['candidate_sha256']
            if valid:
                logging.info('REUSE %s',source['extract'])
                return result,True
        except (OSError,ValueError,KeyError):
            pass
    job=cache/('attempt-'+uuid.uuid4().hex[:12])
    job.mkdir(parents=True)
    atomic_json(job/'source.json',source)
    logging.info('WORKER %s; log=%s',source['extract'],job/'worker.log')
    with (job/'console.txt').open('w',encoding='utf-8') as stdout:
        process=subprocess.Popen([sys.executable,'-m','poi_osm.collection_worker','--source',str(job/'source.json'),
                                  '--job',str(job),'--config',str(config.resolve())],cwd=REPO,stdout=stdout)
        try:
            code=process.wait()
        except BaseException:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()
            raise
    if code:
        raise RuntimeError(f'PBF worker failed: {source["path"]}; inspect {job / "worker.log"}')
    result=json.loads((job/'result.json').read_text(encoding='utf-8'))
    # Child exit releases libosmium's Windows handles before cleanup.
    for index in (job/'indexes').glob('*.idx'):cleanup_index(index)
    atomic_json(pointer,result)
    return result,False


def run(args):
    settings=load_settings(args.config)
    profile=profile_from(args.profile)
    if profile.get('coverage_id')!=settings['coverage_id']:
        raise ValueError('Collection profile and settings coverage_id differ')
    registry=json.loads(Path(settings['regions']).read_text(encoding='utf-8'))
    if registry['coverage_id']!=settings['coverage_id']:
        raise ValueError('Region registry coverage differs from collection')
    output=args.output.resolve()
    if args.check:
        sources,excluded=inspect_sources(args.folder,profile,args.source_date)
        print(json.dumps({'status':'INPUT_CHECKED','sources':sources,'excluded_files':excluded,
                          'source_bytes':sum(s['size_bytes'] for s in sources),
                          'boundaries_reviewed':settings.get('boundaries_reviewed',False),
                          'complete_snapshot_confirmed':profile.get('complete_snapshot_confirmed',False),
                          'note':'File names and dates do not certify geographic completeness or boundary provenance.'},ensure_ascii=False,indent=2))
        return 0
    output.mkdir(parents=True,exist_ok=True)
    with process_lock(output/'state'/'process.lock'):
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
        (output/'logs').mkdir(exist_ok=True)
        logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s',
                            handlers=[logging.StreamHandler(),logging.FileHandler(output/'logs'/f'{run_id}.log',encoding='utf-8')],force=True)
        stage=output/'work'/run_id;stage.mkdir(parents=True)
        report={'status':'RUNNING','production':args.production,'preview_only':not args.production,
                'run_id':run_id,'boundaries_reviewed':False,'complete_coverage_verified':False}
        atomic_json(stage/'summary.json',report)
        store=None;started=time.monotonic()
        try:
            approved_index=None
            if args.production:
                if not (settings.get('coverage_confirmed') is True and settings.get('boundaries_reviewed') is True and profile.get('complete_snapshot_confirmed') is True):
                    raise ValueError('Production requires reviewed boundaries, confirmed coverage and complete_snapshot_confirmed=true; run preview first')
                approved_index=RegionIndex(Path(settings['boundaries']),Path(settings['regions']))
            sources,excluded=inspect_sources(args.folder,profile,args.source_date)
            report.update(sources=sources,excluded_files=excluded,source_date=sources[0]['source_date'],
                          source_bytes=sum(s['size_bytes'] for s in sources),free_disk_bytes=shutil.disk_usage(output).free)
            atomic_json(stage/'summary.json',report)
            logging.info('SOURCES %s; bytes=%s; free disk=%s',len(sources),report['source_bytes'],report['free_disk_bytes'])
            contract=contract_hash(settings,registry,_hash_file(Path(settings['boundaries']))) if approved_index else None
            if contract:
                contract=hashlib.sha256((contract+json.dumps({'expected':profile['expected_extracts'],'excluded':profile.get('excluded_extracts',[])},sort_keys=True)).encode()).hexdigest()
            state=output/'state'/'last_success.json'
            previous=json.loads(state.read_text(encoding='utf-8')) if args.production and state.exists() else None
            if previous and previous['contract_hash']!=contract:
                raise ValueError('Coverage, filters or boundaries changed; choose a separate output directory for a new baseline')
            if previous and report['source_date']<previous['source_date']:
                raise ValueError('Collection snapshot is older than previous successful snapshot')
            jobs=[];reused=0
            for number,source in enumerate(sources,1):
                logging.info('PART %s/%s: %s',number,len(sources),source['extract'])
                result,cached=worker_for(source,settings,args.config,output)
                jobs.append(result);reused+=cached
                atomic_json(stage/'progress.json',{'completed_extracts':number,'total_extracts':len(sources),'cached_extracts':reused})
            candidate_index,boundary_report=combine_boundaries(jobs,registry,stage)
            region_index=approved_index or candidate_index
            report['boundary_report']=boundary_report
            report['cached_extracts']=reused
            report['worker_jobs']=[job['job'] for job in jobs]
            unresolved=sum(j['parsing'].get('unresolved_objects',0) for j in jobs)
            if args.production and unresolved:
                raise ValueError(f'Unresolved POI geometries: {unresolved}; production publication refused')
            source={'source_date':report['source_date'],'path':'local-collection'}
            store=Registry(stage/'registry.sqlite',settings['mapping'],datetime.now(timezone.utc).date().isoformat(),source,settings['batch_size'])
            store.db.execute('CREATE TABLE overlap_objects(osm_type TEXT,osm_id TEXT,signature TEXT,source_file TEXT,PRIMARY KEY(osm_type,osm_id))')
            duplicates=0
            with (stage/'review.csv').open('w',encoding='utf-8-sig',newline='') as stream:
                review_fields=['osm_type','osm_id','tag','lon','lat','review_reason','detail','source_file']
                writer=csv.DictWriter(stream,fieldnames=review_fields,delimiter=';',extrasaction='ignore');writer.writeheader()
                for job in jobs:
                    job_root=Path(job['job'])
                    with (job_root/'review.csv').open(encoding='utf-8-sig',newline='') as reviews:
                        for row in csv.DictReader(reviews,delimiter=';'):writer.writerow({**row,'source_file':Path(job['source']['path']).name})
                    logging.info('MERGE %s',job['source']['path'])
                    with closing(sqlite3.connect((job_root/'registry.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
                        cursor=db.execute('SELECT '+','.join(FIELDS)+',fingerprint FROM poi ORDER BY osm_type,osm_id,tag')
                        source_rows=(dict(zip(FIELDS+['fingerprint'],values)) for values in cursor)
                        for key,group in groupby(source_rows,key=lambda row:(row['osm_type'],row['osm_id'])):
                            rows=list(group)
                            signature=hashlib.sha256(json.dumps([r['fingerprint'] for r in rows]).encode()).hexdigest()
                            old=store.db.execute('SELECT signature,source_file FROM overlap_objects WHERE osm_type=? AND osm_id=?',key).fetchone()
                            if old and old[0]!=signature:
                                raise ValueError(f'Conflicting overlapping object {key}: {old[1]} vs {job["source"]["path"]}')
                            if not old:
                                store.db.execute('INSERT INTO overlap_objects VALUES(?,?,?,?)',(*key,signature,job['source']['path']))
                            for row in rows:
                                region=region_index.lookup(row['lon'],row['lat'])
                                row.update(region_code=region['region_code'] if region else '',region_name=region['region_name'] if region else '',
                                           region_id=str(region.get('region_id','')) if region else '',country='Россия' if region else None)
                                if not region:
                                    writer.writerow({**row,'review_reason':getattr(region_index,'last_problem',None) or 'unknown_region'})
                                before=store.db.total_changes
                                store.insert(row,source_file=row['source_file'])
                                duplicates+=store.db.total_changes==before
            if args.production:
                store.compare(Path(previous['registry']) if previous else None)
            else:
                store.db.execute("UPDATE poi SET record_status='PREVIEW'");store.db.commit()
            metrics=store.metrics()
            if args.production:
                check_quality(metrics,previous['metrics'] if previous else None,settings['quality'])
            elif not metrics['total']:
                raise ValueError('No matching POIs found; refusing an empty collection result')
            report.update(metrics=metrics,duplicate_category_rows_removed=duplicates,unresolved_objects=unresolved,
                          parsing={k:sum(job['parsing'].get(k,0) for job in jobs) for k in
                                   ['processed_nodes','processed_ways','processed_relations','processed_osm_objects','poi_records','unresolved_objects','skipped_poi_records']})
            objects=store.export(stage,[r['region_code'] for r in registry['regions']],settings.get('write_parquet',True),selection_rules=settings['primary_rules'])
            store.close();store=None
            regional=split_regions(stage/'poi_teradata.csv',stage/'regions',registry,profile.get('max_rows_per_region_file',50000))
            incomplete=bool(unresolved or (not approved_index and boundary_report['missing_region_codes'])
                            or metrics['unassigned_fraction']>settings['quality']['max_unassigned_fraction'])
            status='SUCCESS' if args.production else ('COLLECTION_PREVIEW_INCOMPLETE' if incomplete else 'COLLECTION_PREVIEW_SUCCESS')
            destination=output/('runs' if args.production else 'preview_runs')/run_id
            report.update(status=status,output=str(destination),registry=str(destination/'registry.sqlite'),
                          object_export=objects,regional_export=regional,contract_hash=contract,
                          boundaries_reviewed=bool(approved_index),complete_coverage_verified=bool(args.production),
                          elapsed_seconds=round(time.monotonic()-started,3))
            atomic_json(stage/'summary.json',report);atomic_json(stage/'manifest.json',report)
            destination.parent.mkdir(parents=True,exist_ok=True)
            stage.replace(destination)
            if args.production:atomic_json(state,report)
            logging.info('%s: %s Teradata objects; %s regional files; output=%s',status,objects['teradata_objects'],len(regional['files']),destination)
            print(json.dumps(report,ensure_ascii=False,indent=2))
            return 0
        except BaseException as error:
            if store:store.close()
            report.update(status='CANCELLED' if isinstance(error,KeyboardInterrupt) else 'FAILED',error=str(error))
            # stage can have moved if only the final state pointer failed.
            if stage.exists():atomic_json(stage/'summary.json',report)
            logging.exception('FAILED; previous production pointer retained; completed workers are reusable')
            raise


def main(argv=None):
    for stream in (sys.stdout,sys.stderr):
        if stream and hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='backslashreplace')
    cli=argparse.ArgumentParser(description='Local federal-district collection with resumable workers and regional CSV parts')
    cli.add_argument('folder',type=Path)
    cli.add_argument('--output',type=Path,default=Path('russia_output'))
    cli.add_argument('--config',type=Path,default=CONFIG/'settings.json')
    cli.add_argument('--profile',type=Path,default=CONFIG/'collection.json')
    cli.add_argument('--source-date',help='Verified date for headerless PBFs; never inferred from names')
    cli.add_argument('--check',action='store_true')
    cli.add_argument('--production',action='store_true',help='Requires reviewed boundaries and explicit complete-snapshot confirmation')
    args=cli.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s',force=True)
    try:return run(args)
    except KeyboardInterrupt:
        print('Stopped; completed workers and previous production snapshot retained.',file=sys.stderr);return 130
    except Exception as error:
        print(f'ERROR: {error}',file=sys.stderr);return 1


if __name__=='__main__':
    raise SystemExit(main())
