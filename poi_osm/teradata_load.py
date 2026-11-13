"""Synchronize only changed objects from the approved POI snapshot into Teradata."""
from __future__ import annotations

import csv
import getpass
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, quote_plus

from .runtime import atomic_json, process_lock

LOG = logging.getLogger(__name__)
TABLE = 'SBX_OPERATION.DAK_POI_ISH'
HOST = 'td2800.corp.tele2.ru'
BATCH_SIZE = 10_000
COLUMNS = ['OSM_TYPE', 'OSM_ID', 'OSM_TYPE_ID', 'POINT_NAME', 'LON', 'LAT', 'WKT',
           'REGION_NAME', 'REGION_NAME_EDW', 'PRIMARY_CATEGORY', 'PRIMARY_SUBCATEGORY',
           'PRIMARY_OSM_KEY', 'PRIMARY_OSM_VALUE', 'IS_TELECOM', 'TELECOM_TYPE',
           'IS_CHAIN', 'CHAIN_NAME', 'UPDATE_DATE', 'RECORD_STATUS']
SOURCE = {c: c.lower() for c in COLUMNS if c != 'OSM_TYPE_ID'}
SOURCE['REGION_NAME_EDW'] = 'REGION_NAME_EDW'


def convert(row):
    if None in row or any(value is None for value in row.values()):
        raise ValueError('Malformed CSV row')
    values = {column: row[source] or None for column, source in SOURCE.items()}
    kind, ident = values['OSM_TYPE'], values['OSM_ID']
    if kind not in {'node', 'way'} or not ident or not ident.isascii() or not ident.isdigit() or not 0 < int(ident) <= 9223372036854775807:
        raise ValueError('Invalid node/way BIGINT identity')
    values['OSM_ID'] = int(ident)
    values['OSM_TYPE_ID'] = f'{kind}/{int(ident)}'
    for column, bound in [('LON', 180), ('LAT', 90)]:
        try:
            number = Decimal(values[column] or '')
            if not number.is_finite() or abs(number) > bound:
                raise ValueError('Invalid coordinate: ' + column)
            values[column] = number.quantize(Decimal('0.00001'), rounding=ROUND_HALF_UP)
        except InvalidOperation as error:
            raise ValueError('Invalid coordinate: ' + column) from error
    for column in ['IS_TELECOM', 'IS_CHAIN']:
        if values[column] not in {'0', '1', None}:
            raise ValueError('Invalid boolean: ' + column)
    values['IS_TELECOM'] = int(values['IS_TELECOM']) if values['IS_TELECOM'] is not None else None
    if values['RECORD_STATUS'] not in {'NEW','UPDATED','UNCHANGED'}:
        raise ValueError('Active snapshot has an invalid RECORD_STATUS')
    values['UPDATE_DATE'] = date.fromisoformat(values['UPDATE_DATE']) if values['UPDATE_DATE'] else None
    for column, value in values.items():
        if isinstance(value, str) and any(ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError('Invalid Unicode scalar or NUL: ' + column)
        limit = 255 if column == 'POINT_NAME' else 100
        if isinstance(value, str) and len(value.encode('utf-16-le')) // 2 > limit:
            raise ValueError(f'VARCHAR({limit}) exceeded: {column}')
    return tuple(values[c] for c in COLUMNS)


def batches(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter=';')
        if not set(SOURCE.values()) <= set(reader.fieldnames or []):
            raise ValueError('Missing POI export columns; regenerate using --files')
        batch = []
        for row in reader:
            try:
                batch.append(convert(row))
            except ValueError as error:
                raise ValueError(f'CSV line {reader.line_num}, {row.get("osm_type", "?")}/{row.get("osm_id", "?")}: {error}') from error
            if len(batch) == BATCH_SIZE:
                yield batch
                batch = []
        if batch:
            yield batch


def validate(path, keys_path):
    total = 0
    with closing(sqlite3.connect(keys_path)) as db:
        db.execute('CREATE TABLE keys(id TEXT PRIMARY KEY) WITHOUT ROWID')
        for batch in batches(path):
            for values in batch:
                try:
                    db.execute('INSERT INTO keys VALUES (?)', (values[2],))
                except sqlite3.IntegrityError as error:
                    raise ValueError('Duplicate OSM_TYPE_ID: ' + values[2]) from error
                total += 1
        db.commit()
    if total == 0:
        raise ValueError('Empty snapshot is not allowed')
    return total



TEXT_FIELDS = {'POINT_NAME', 'REGION_NAME', 'REGION_NAME_EDW', 'PRIMARY_CATEGORY',
               'PRIMARY_SUBCATEGORY', 'PRIMARY_OSM_KEY', 'PRIMARY_OSM_VALUE', 'TELECOM_TYPE', 'CHAIN_NAME'}


def unsupported(char):
    code = ord(char)
    return code > 0xFFFF or code == 0 or 0xD800 <= code <= 0xDFFF or 0xFDD0 <= code <= 0xFDEF or code in {0xFFFE, 0xFFFF}


def prepare_snapshot(path, prepared, output):
    """Normalize text only in the upload copy and report every changed field."""
    temporary = output.with_suffix('.csv.tmp')
    counts = {'changed_objects': 0, 'changed_fields': 0, 'replaced_characters': 0,
              'point_name_fallbacks': 0, 'missing_subcategory_fallbacks': 0}
    try:
        with path.open(encoding='utf-8-sig', newline='') as source, prepared.open('x', encoding='utf-8-sig', newline='') as cleaned, temporary.open('w', encoding='utf-8-sig', newline='') as audit:
            reader = csv.DictReader(source, delimiter=';')
            if not set(SOURCE.values()) <= set(reader.fieldnames or []):
                raise ValueError('Missing POI export columns; regenerate using --files')
            writer = csv.DictWriter(cleaned, fieldnames=reader.fieldnames, delimiter=';')
            writer.writeheader()
            report = csv.writer(audit, delimiter=';')
            report.writerow(['object_row', 'osm_type_id', 'column', 'codepoints', 'characters', 'action', 'original', 'loaded'])
            for number, row in enumerate(reader, 1):
                if None in row or any(value is None for value in row.values()):
                    raise ValueError(f'Malformed CSV row {number}')
                changes = []
                for column, name in SOURCE.items():
                    if column not in TEXT_FIELDS:
                        continue
                    original = row[name]
                    invalid = [char for char in original if unsupported(char)]
                    if not invalid:
                        continue
                    invalid = [char for char in original if unsupported(char) or char in '\u200d\ufe0e\ufe0f']
                    row[name] = ''.join(' ' if unsupported(char) or char in '\u200d\ufe0e\ufe0f' else char for char in original).strip()
                    changes.append((column, name, original, invalid))
                for column, name, original, invalid in changes:
                    action = 'replace_with_space'
                    if column == 'POINT_NAME' and not row[name]:
                        row[name] = row['primary_subcategory'].strip()
                        action = 'primary_subcategory_fallback' if row[name] else 'null_missing_subcategory'
                        counts['point_name_fallbacks'] += bool(row[name])
                        counts['missing_subcategory_fallbacks'] += not bool(row[name])
                    report.writerow([number, row['osm_type'] + '/' + row['osm_id'], column,
                                     ','.join(f'U+{ord(char):04X}' for char in sorted(set(invalid))), len(invalid),
                                     action, original, row[name]])
                    counts['changed_fields'] += 1
                    counts['replaced_characters'] += len(invalid)
                counts['changed_objects'] += bool(changes)
                writer.writerow(row)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {**counts, 'policy': 'replace_unsupported_with_space_then_subcategory', 'report': str(output)}


def password_for(user):
    password = os.environ.get('POI_TD_PASSWORD')
    if not password:
        import keyring
        password = keyring.get_password('teradata', user)
    if not password:
        raise ValueError('Set POI_TD_PASSWORD or save the password in keyring service teradata for the current Windows user')
    return password


def encoded(values):
    """Stable representation of the actual 19-column Teradata payload."""
    normalized = list(values)
    for index,column in enumerate(COLUMNS):
        value = normalized[index]
        if value is None:
            continue
        if column in {'LON','LAT'}:
            value = format(Decimal(value).quantize(Decimal('0.00001')) + Decimal(0), '.5f')
        elif column in {'OSM_ID','IS_TELECOM'}:
            value = int(value)
        elif column == 'UPDATE_DATE':
            value = value.date().isoformat() if isinstance(value,datetime) else value.isoformat() if isinstance(value,date) else date.fromisoformat(str(value)).isoformat()
        else:
            value = str(value)
        normalized[index] = value
    return normalized


def fingerprint(values):
    # Source NEW -> UNCHANGED is not a point data change and must not cause DML.
    content = encoded(values)[:-1]
    return hashlib.sha256(json.dumps(content,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def decoded(payload, action):
    values = json.loads(payload)
    for index,column in enumerate(COLUMNS):
        if values[index] is not None:
            if column in {'LON','LAT'}: values[index] = Decimal(values[index])
            elif column == 'UPDATE_DATE': values[index] = date.fromisoformat(values[index])
    values[-1] = action
    return tuple(values)


def desired_snapshot(plan,path,total):
    plan.execute('CREATE TABLE desired(id TEXT PRIMARY KEY,fingerprint TEXT,payload TEXT) WITHOUT ROWID')
    count = 0
    for batch in batches(path):
        for values in batch:
            try:
                plan.execute('INSERT INTO desired VALUES(?,?,?)',(values[2],fingerprint(values),json.dumps(encoded(values),ensure_ascii=False)))
            except sqlite3.IntegrityError as error:
                raise ValueError('Duplicate source identity: '+values[2]) from error
            count += 1
    if not count or count != total:
        raise ValueError('Incomplete or empty desired snapshot')
    plan.commit()


def read_target(plan,cur,columns):
    plan.execute('CREATE TABLE actual(id TEXT PRIMARY KEY,fingerprint TEXT,status TEXT) WITHOUT ROWID')
    cur.execute(f'SELECT {columns} FROM {TABLE}')
    count = 0
    while True:
        batch = cur.fetchmany(BATCH_SIZE)
        if not batch: break
        for values in batch:
            if len(values)!=len(COLUMNS): raise ValueError('Unexpected target row shape')
            kind,ident,uid = values[:3]
            if kind not in {'node','way','relation'} or ident is None or not 0 < int(ident) <= 9223372036854775807 or uid != f'{kind}/{int(ident)}':
                raise ValueError('Invalid target OSM identity; repair target keys before synchronization')
            try:
                plan.execute('INSERT INTO actual VALUES(?,?,?)',(uid,fingerprint(values),values[-1]))
            except sqlite3.IntegrityError as error:
                raise ValueError('Duplicate OSM_TYPE_ID in target: '+uid) from error
            count += 1
        if count % 100000 == 0: LOG.info('Teradata: %d target rows compared',count)
    plan.commit()
    return count


def plan_delta(plan,total,audit_path):
    plan.execute('CREATE TABLE delta(id TEXT PRIMARY KEY,action TEXT,payload TEXT) WITHOUT ROWID')
    plan.execute("""INSERT INTO delta SELECT d.id,CASE WHEN a.id IS NULL THEN 'NEW' ELSE 'UPDATED' END,d.payload
        FROM desired d LEFT JOIN actual a ON a.id=d.id WHERE a.id IS NULL OR a.fingerprint!=d.fingerprint""")
    plan.execute("""INSERT INTO delta SELECT a.id,'DELETED',NULL FROM actual a
        LEFT JOIN desired d ON d.id=a.id WHERE d.id IS NULL""")
    stats = {action:0 for action in ('NEW','UPDATED','DELETED','UNCHANGED')}
    stats.update(dict(plan.execute('SELECT action,COUNT(*) FROM delta GROUP BY action')))
    stats['UNCHANGED'] = total-stats['NEW']-stats['UPDATED']
    if audit_path:
        temporary = audit_path.with_suffix('.csv.tmp')
        try:
            with temporary.open('w',encoding='utf-8-sig',newline='') as stream:
                writer = csv.writer(stream,delimiter=';');writer.writerow(['osm_type_id','action','source_record_status'])
                for uid,action,payload in plan.execute('SELECT id,action,payload FROM delta ORDER BY action,id'):
                    writer.writerow([uid,action,json.loads(payload)[-1] if payload else ''])
            os.replace(temporary,audit_path)
        finally:
            temporary.unlink(missing_ok=True)
    plan.commit()
    return stats


def dml_batch(con,cur,sql,batch,action):
    try:
        cur.executemany(sql,batch)
    except Exception as error:
        details = ''
        try: details = str(con.nativeSQL('{fn teradata_get_errors}'))
        except Exception: pass
        suffix = f'; driver batch details: {details}' if details else ''
        raise RuntimeError(f'{action} batch of {len(batch)} objects failed: {error}{suffix}') from None


def sync_snapshot(path,total,*,connect=None,password=None,audit_path=None):
    with tempfile.TemporaryDirectory(prefix='poi-td-delta-') as folder, closing(sqlite3.connect(Path(folder)/'plan.sqlite')) as plan:
        plan.execute('PRAGMA temp_store=FILE')
        desired_snapshot(plan,path,total)
        if connect is None:
            import teradatasql
            connect = teradatasql.connect
        user = getpass.getuser()
        password = password if password is not None else password_for(user)
        con = cur = None
        commit_started = False
        committed = False
        try:
            con = connect(host=HOST,user=user,password=password,logmech='LDAP',tmode='ANSI',charset='UTF8')
            con.autocommit = False
            cur = con.cursor()
            cur.execute('SET SESSION CHARACTER SET UNICODE PASS THROUGH ON')
            LOG.info('Teradata: UTF8, Unicode Pass Through enabled')
            cur.execute(f'SELECT * FROM {TABLE} WHERE 1 = 0')
            missing = set(COLUMNS)-{entry[0].upper() for entry in cur.description}
            if missing: raise ValueError('Missing target columns: '+', '.join(sorted(missing)))
            cur.execute(f'LOCKING TABLE {TABLE} FOR WRITE SELECT COUNT(*) FROM {TABLE}')
            initial = int(cur.fetchone()[0])
            columns = ','.join('"'+column+'"' for column in COLUMNS)
            if read_target(plan,cur,columns)!=initial: raise ValueError('Target changed during comparison')
            stats = plan_delta(plan,total,audit_path)
            LOG.info('Teradata delta: NEW=%d UPDATED=%d DELETED=%d UNCHANGED=%d',*(stats[key] for key in ('NEW','UPDATED','DELETED','UNCHANGED')))
            mutable = COLUMNS[3:]
            sqls = {
                'DELETED':f'DELETE FROM {TABLE} WHERE "OSM_TYPE_ID" = ?',
                'UPDATED':f'UPDATE {TABLE} SET '+','.join('"'+field+'" = ?' for field in mutable)+' WHERE "OSM_TYPE_ID" = ?',
                'NEW':f'INSERT INTO {TABLE} ({columns}) VALUES ({",".join("?" for _ in COLUMNS)})'}
            status_resets = plan.execute("SELECT COUNT(*) FROM actual WHERE status IS NULL OR status!='UNCHANGED'").fetchone()[0]
            unchanged_status_resets = plan.execute("""SELECT COUNT(*) FROM actual a JOIN desired d ON d.id=a.id
                WHERE a.fingerprint=d.fingerprint AND (a.status IS NULL OR a.status!='UNCHANGED')""").fetchone()[0]
            if status_resets:
                # Reset old marks on the server; changed rows receive their new marks below.
                cur.execute(f"UPDATE {TABLE} SET \"RECORD_STATUS\" = 'UNCHANGED' WHERE \"RECORD_STATUS\" IS NULL OR \"RECORD_STATUS\" <> 'UNCHANGED'")
            sent = 0
            for action in ('DELETED','UPDATED','NEW'):
                batch = []
                for uid,payload in plan.execute('SELECT id,payload FROM delta WHERE action=? ORDER BY id',(action,)):
                    values = decoded(payload,action) if payload else None
                    batch.append((uid,) if action=='DELETED' else (*values[3:],uid) if action=='UPDATED' else values)
                    if len(batch)==BATCH_SIZE:
                        dml_batch(con,cur,sqls[action],batch,action);sent+=len(batch);batch=[]
                        LOG.info('Teradata: %d changes sent; awaiting COMMIT',sent)
                if batch:
                    dml_batch(con,cur,sqls[action],batch,action);sent+=len(batch)
            LOG.info('Teradata: %d data changes sent; %d existing status marks reset on server',sent,status_resets)
            cur.execute(f'SELECT COUNT(*), COUNT(DISTINCT OSM_TYPE_ID) FROM {TABLE}')
            count,unique = map(int,cur.fetchone())
            if count!=total or unique!=total: raise ValueError(f'Teradata count mismatch: expected={total}, rows={count}, unique={unique}')
            commit_started = True
            con.commit();committed=True
            return {'changes':stats,'rows_sent':sent,'status_resets':status_resets,
                    'unchanged_status_resets':unchanged_status_resets,'target_before':initial,'target_after':count}
        except BaseException as error:
            if con is not None and not committed:
                try: con.rollback();LOG.info('Teradata: ROLLBACK completed')
                except Exception: LOG.error('Rollback could not be confirmed; check Teradata before retrying')
            if isinstance(error,(KeyboardInterrupt,SystemExit)): raise
            message = str(error)
            for secret in {password,quote(password,safe=''),quote_plus(password)}:
                if secret: message=message.replace(secret,'***')
            if commit_started: message='COMMIT outcome unconfirmed; check the table before retrying. '+message
            raise RuntimeError('Teradata synchronization failed: '+message) from None
        finally:
            for resource in (cur,con):
                if resource is not None:
                    try: resource.close()
                    except Exception: LOG.warning('Could not close Teradata resource')


def execute(root, *, check_only=False, connect=None, password=None):
    root = Path(root).resolve()
    result = root / 'result'
    analysis = result / 'analysis'
    source = result / 'poi_teradata.csv'
    report_path = analysis / 'teradata_load.json'
    with process_lock(root / '.product.lock'):
        (root / 'logs').mkdir(exist_ok=True)
        logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s',
                            handlers=[logging.StreamHandler(), logging.FileHandler(root / 'logs/teradata.log', mode='w', encoding='utf-8')], force=True)
        summary = json.loads((analysis / 'summary.json').read_text(encoding='utf-8-sig'))
        if summary.get('status') != 'PRODUCT_SUCCESS' or not summary.get('production_ready') or not summary.get('file_checks_ok'):
            raise ValueError('A verified PRODUCT_SUCCESS result is required')
        # Work on a private snapshot; validate all rows before opening a connection.
        with tempfile.TemporaryDirectory(prefix='poi-td-') as temporary:
            snapshot = Path(temporary) / 'poi.csv'
            shutil.copyfile(source, snapshot)
            try:
                prepared = Path(temporary) / 'prepared.csv'
                unicode_audit = prepare_snapshot(snapshot, prepared, analysis / 'teradata_unicode.csv')
                total = validate(prepared, Path(temporary) / 'keys.sqlite')
                if total != summary['total_objects']:
                    raise ValueError('CSV row count differs from product summary')
                LOG.info('Unicode preparation: %d objects, %d fields changed; %d name fallbacks',
                         unicode_audit['changed_objects'], unicode_audit['changed_fields'], unicode_audit['point_name_fallbacks'])
                with snapshot.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                with prepared.open('rb') as stream:
                    prepared_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                delta = None
                if not check_only:
                    delta = sync_snapshot(prepared,total,connect=connect,password=password,audit_path=analysis/'teradata_delta.csv')
                report = {'status': 'TERADATA_CHECK_SUCCESS' if check_only else 'TERADATA_LOAD_SUCCESS',
                          'table': TABLE, 'host': HOST, 'rows': total, 'source_sha256': digest,
                          'source_product_run_id': summary.get('product_run_id'), 'columns': COLUMNS,
                          'prepared_csv_sha256': prepared_digest,
                          'session_charset': 'UTF8', 'unicode_pass_through_requested': True, 'unicode_pass_through_enabled': not check_only, 'unicode_audit': unicode_audit,
                          'batch_size': BATCH_SIZE, 'mode': 'incremental_sync', 'delta':delta,
                          'timestamp': datetime.now(timezone.utc).isoformat()}
            except Exception as error:
                atomic_json(report_path, {'status': 'TERADATA_FAILED', 'table': TABLE, 'error': str(error)})
                raise
            atomic_json(report_path, report)
            return report
