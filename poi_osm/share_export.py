"""Publish a circle-only ZIP with macroregion/EDW layer names to a destination folder."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shutil
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .installation import portable_product, product_config
from .kml import NS
from .region_dictionary import RegionDictionary
from .runtime import atomic_json, process_lock
from .sources import _hash_file

DEFAULT_DIRECTORY = r'\\corp.tele2.ru\operations_MR\Operations_All\Потенциал_рынка'
PREFIX = 'Данные_из_OSM_по_интересующим_тегам_Выгрузка_на_'
MOSCOW = timezone(timedelta(hours=3))
LOG = logging.getLogger(__name__)


def settings(value=None, base=None):
    value = value or {}
    if not isinstance(value,dict) or not isinstance(value.get('enabled',False),bool):
        raise ValueError('publication.enabled must be boolean')
    directory = value.get('directory',DEFAULT_DIRECTORY)
    if not isinstance(directory,str) or not directory.strip(): raise ValueError('publication.directory is required')
    column = value.get('region_column','EDW')
    if column not in {'EDW','CEM','REGION_NAME_POI'}: raise ValueError('Invalid publication.region_column')
    if not Path(directory).is_absolute() and not PureWindowsPath(directory).is_absolute() and base:
        directory = str(Path(base)/directory)
    return {**value,'enabled':value.get('enabled',False),'directory':directory,'region_column':column}


def slug(value):
    result = re.sub(r'[^\w.\-]+','_',str(value),flags=re.UNICODE).strip(' ._')
    if not result: raise ValueError('Empty macroregion or regional layer label')
    return result[:100]


def rename_document(incoming,outgoing,title):
    """Stream KML children, replacing the document title without editing circles."""
    outgoing.write(f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="{NS}"><Document>'.encode())
    label=ET.Element('{'+NS+'}name');label.text=title
    outgoing.write(ET.tostring(label,encoding='utf-8'))
    depth=0;document=None;features=0
    for event,node in ET.iterparse(incoming,events=('start','end')):
        if event=='start':
            depth+=1
            if depth==2:
                if node.tag!='{'+NS+'}Document': raise ValueError('Unsupported KML structure')
                document=node
        else:
            if depth==3:
                if node.tag!='{'+NS+'}name': outgoing.write(ET.tostring(node,encoding='utf-8'))
                features+=node.tag=='{'+NS+'}Placemark'
                document.remove(node);node.clear()
            depth-=1
    if document is None: raise ValueError('KML document is missing')
    outgoing.write(b'</Document></kml>')
    return features


def package(source,target,dictionary,column,product_run_id,source_date=None):
    with zipfile.ZipFile(source) as original:
        if original.testzip() is not None: raise ValueError('Source KML ZIP is corrupt')
        report=json.loads(original.read('summary.json'))
        if report.get('status')!='KML_EXPORT_SUCCESS' or not report.get('file_checks_ok'):
            raise ValueError('Use a verified successful KML archive')
        selected=[entry for entry in report['files'] if entry['kind']=='circles']
        planned=[]
        for entry in selected:
            path=PurePosixPath(entry['file'])
            if path.is_absolute() or '..' in path.parts or path.suffix!='.kml': raise ValueError('Unsafe KML ZIP entry')
            record=dictionary.records.get(entry['region_code'])
            if not record: raise ValueError('Missing region dictionary record: '+entry['region_code'])
            macro=record.get('MACROREGION_NAME')
            region=record.get(column)
            if not macro or not region: raise ValueError('Missing macroregion/region label: '+entry['region_code'])
            planned.append({**entry,'macroregion':macro,'label':region,'base':slug(macro)+'_('+slug(region)+')'})
        codes={entry['base']:{e['region_code'] for e in planned if e['base']==entry['base']} for entry in planned}
        parts=Counter((entry['base'],entry['region_code']) for entry in planned)
        numbers=Counter();files=[]
        with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
            for entry in planned:
                key=(entry['base'],entry['region_code']);numbers[key]+=1
                filename=entry['base']
                if len(codes[entry['base']])>1: filename+='_'+entry['region_code']
                if parts[key]>1: filename+=f'_{numbers[key]:03d}'
                filename+='.kml'
                with original.open(entry['file']) as incoming,archive.open(filename,'w',force_zip64=True) as outgoing:
                    count=rename_document(incoming,outgoing,filename[:-4])
                if count!=entry['features']: raise ValueError('Published layer count differs from source')
                files.append({'file':filename,'region_code':entry['region_code'],'region_name':entry['label'],
                              'macroregion':entry['macroregion'],'circles':count})
            index=io.StringIO(newline='')
            writer=csv.DictWriter(index,fieldnames=['file','region_code','region_name','macroregion','circles'],delimiter=';')
            writer.writeheader();writer.writerows(files)
            archive.writestr('regions_index.csv',index.getvalue().encode('utf-8-sig'))
            packaged={'status':'KML_SHARE_PACKAGE','source_product_run_id':product_run_id,
                      'source_date':source_date,'source_zip_sha256':_hash_file(source),'region_column':column,
                      'dictionary_sha256':dictionary.metadata['dictionary_sha256'],'files':files,
                      'circles':sum(entry['circles'] for entry in files),'license':report.get('license','ODbL-1.0; © OpenStreetMap contributors')}
            archive.writestr('summary.json',json.dumps(packaged,ensure_ascii=False,indent=2))
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None or len(set(archive.namelist()))!=len(files)+2:
            raise ValueError('Named publication ZIP verification failed')
    return packaged


def publish_layers(root,options,*,now=None):
    root=Path(root).resolve();options=settings(options)
    destination=options['directory']
    if os.name!='nt' and PureWindowsPath(destination).is_absolute():
        raise ValueError('UNC/Windows publication requires Windows; use a mounted local path on other systems')
    report_path=root/'result/analysis/publication.json'
    with process_lock(root/'.product.lock'):
        portable_product(root)
        config=product_config(root)
        summary=json.loads((root/'result/analysis/summary.json').read_text(encoding='utf-8-sig'))
        if summary.get('status')!='PRODUCT_SUCCESS' or not summary.get('file_checks_ok') or not summary.get('production_ready'):
            raise ValueError('A successful local product result is required before publication')
        source=root/'result/poi_layers.zip'
        dictionary=RegionDictionary(root/config['region_dictionary'],json.loads((root/'boundaries/regions.json').read_text(encoding='utf-8-sig')))
        work=root/'.work'/('publication-'+uuid.uuid4().hex);work.mkdir(parents=True)
        atomic_json(work/'owner.json',{'root':str(root),'child_pid':None})
        remote_temporary=None
        try:
            published_at=(now or datetime.now(MOSCOW)).astimezone(MOSCOW)
            stamp=published_at.strftime('%Y_%m_%d')
            filename=PREFIX+stamp+'_с_poi.zip'
            named=work/filename
            packaged=package(source,named,dictionary,options['region_column'],summary.get('product_run_id'),summary.get('source_date'))
            directory=Path(destination)
            if directory.resolve().is_relative_to(root/'result') or directory.resolve().is_relative_to(root/'.work'):
                raise ValueError('Publication destination must be outside generated result/work directories')
            directory.mkdir(parents=True,exist_ok=True)
            final=directory/filename
            remote_temporary=directory/('.poi-osm-'+uuid.uuid4().hex+'.part')
            shutil.copyfile(named,remote_temporary)
            digest=_hash_file(named)
            if _hash_file(remote_temporary)!=digest: raise ValueError('Network ZIP checksum mismatch')
            with zipfile.ZipFile(remote_temporary) as verified:
                if verified.testzip() is not None: raise ValueError('Network ZIP CRC mismatch')
            remote_temporary.replace(final)
            result={'status':'KML_PUBLISH_SUCCESS','archive':str(final),'sha256':digest,
                    'source_product_run_id':summary.get('product_run_id'),'circles':packaged['circles'],
                    'layers':len(packaged['files']),'region_column':options['region_column'],
                    'published_at':published_at.isoformat()}
            atomic_json(report_path,result)
            return result
        except Exception as error:
            atomic_json(report_path,{'status':'KML_PUBLISH_FAILED','error':str(error),'local_archive':str(source)})
            raise
        finally:
            if remote_temporary is not None:
                try: remote_temporary.unlink(missing_ok=True)
                except OSError: LOG.warning('Could not remove remote staging file: %s',remote_temporary)
            shutil.rmtree(work)
            try: (root/'.work').rmdir()
            except OSError: pass
