import csv
import json
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from poi_osm.teradata_load import (
    COLUMNS,
    SOURCE,
    batches,
    convert,
    execute,
    password_for,
    sync_snapshot,
    validate,
)


def row(**changes):
    value = {name: '' for name in SOURCE.values()}
    value.update(osm_type='way', osm_id='123', point_name='Телефоны', lon='37.123456', lat='55.123454',
                 region_name='Москва', REGION_NAME_EDW='Москва', is_telecom='1', is_chain='0',
                 update_date='2026-11-12', record_status='UNCHANGED')
    return {**value, **changes}


def save(path, records):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(SOURCE.values()), delimiter=';')
        writer.writeheader()
        writer.writerows(records)


class Cursor:
    def __init__(self,con):
        self.con=con
        self.description=[(c,) for c in COLUMNS]
        self.raw=con.db.cursor()
        self.closed=False
        self.result=None

    def execute(self,sql):
        self.con.statements.append(sql)
        self.result=None
        if sql.startswith('SET SESSION'): return
        sql=sql.replace('LOCKING TABLE '+self.con.table+' FOR WRITE ','')
        self.raw.execute(sql.replace(self.con.table,'target'))
        if 'COUNT(DISTINCT' in sql and self.con.wrong_count: self.result=(0,0)

    def executemany(self,sql,rows):
        self.con.statements.append(sql)
        self.con.batch_sizes.append(len(rows))
        if self.con.fail_insert: raise RuntimeError('driver error password=p@ss')
        clean=lambda value:str(value) if isinstance(value,(Decimal,date)) else value
        self.raw.executemany(sql.replace(self.con.table,'target'),[tuple(clean(v) for v in row) for row in rows])

    def fetchone(self):
        return self.result if self.result is not None else self.raw.fetchone()

    def fetchmany(self,size):
        return self.raw.fetchmany(size)

    def close(self):
        self.raw.close();self.closed=True


class Connection:
    table='SBX_OPERATION.DAK_POI_ISH'
    def __init__(self,*,fail_insert=False,wrong_count=False,initial=()):
        self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE target('+','.join('"'+c+'" '+('INTEGER' if c in {'OSM_ID','IS_TELECOM'} else 'TEXT') for c in COLUMNS)+')')
        for values in initial:
            self.db.execute('INSERT INTO target VALUES('+','.join('?' for _ in COLUMNS)+')',
                            [str(v) if isinstance(v,(Decimal,date)) else v for v in values])
        self.db.commit()
        self.committed=0;self.rolled_back=0;self.closed=False
        self.statements=[];self.batch_sizes=[]
        self.fail_insert=fail_insert;self.wrong_count=wrong_count
        self.cur=Cursor(self)

    @property
    def pending(self):
        return self.db.execute('SELECT * FROM target ORDER BY OSM_ID').fetchall()

    def cursor(self):
        if self.cur.closed: self.cur=Cursor(self)
        return self.cur
    def commit(self): self.db.commit();self.committed+=1
    def rollback(self): self.db.rollback();self.rolled_back+=1
    def close(self): self.closed=True


def test_conversion_matches_target_ddl():
    values = dict(zip(COLUMNS, convert(row())))
    assert values['OSM_TYPE_ID'] == 'way/123'
    assert values['OSM_ID'] == 123
    assert values['LON'] == Decimal('37.12346')
    assert values['LAT'] == Decimal('55.12345')
    assert values['IS_TELECOM'] == 1 and values['IS_CHAIN'] == '0'
    assert values['UPDATE_DATE'] == date(2026, 11, 12)
    assert values['CHAIN_NAME'] is None
    assert values['REGION_NAME_EDW'] == 'Москва'


@pytest.mark.parametrize('changes', [dict(point_name='я'*256), dict(point_name='😀'*128),
    dict(osm_id='9223372036854775808'), dict(osm_type='relation'), dict(lat='NaN'),
    dict(lon='181'), dict(is_chain='yes'), dict(update_date='2026-02-30'), dict(record_status='DELETED')])
def test_invalid_values_are_rejected(changes):
    with pytest.raises(ValueError):
        convert(row(**changes))


def test_duplicate_and_empty_snapshots_rejected(tmp_path):
    source = tmp_path/'poi.csv'
    save(source, [row(), row()])
    with pytest.raises(ValueError, match='Duplicate'):
        validate(source, tmp_path/'duplicates.sqlite')
    save(source, [])
    with pytest.raises(ValueError, match='Empty'):
        validate(source, tmp_path/'empty.sqlite')


def test_single_transaction_batches_and_count_verification(tmp_path):
    source = tmp_path/'poi.csv'
    save(source, [row(osm_id=str(n+1)) for n in range(10001)])
    con = Connection()
    options = {}

    def connect(**kwargs):
        options.update(kwargs)
        return con

    sync_snapshot(source, 10001, connect=connect, password='p@ss')
    assert con.autocommit is False and options['logmech'] == 'LDAP' and options['tmode'] == 'ANSI'
    assert options['host'] == 'td2800.corp.tele2.ru'
    assert con.batch_sizes == [10000, 1]
    assert con.committed == 1 and con.rolled_back == 0 and con.closed
    assert not any(sql.startswith('DELETE') for sql in con.statements)


@pytest.mark.parametrize('failure', ['insert', 'count', 'schema'])
def test_failure_rolls_back_and_masks_password(tmp_path, failure):
    source = tmp_path/'poi.csv'; save(source, [row()])
    con = Connection(fail_insert=failure == 'insert', wrong_count=failure == 'count')
    if failure == 'schema':
        con.cur.description = [('OSM_ID',)]
    with pytest.raises(RuntimeError) as error:
        sync_snapshot(source, 1, connect=lambda **kw: con, password='p@ss')
    assert 'p@ss' not in str(error.value)
    assert con.committed == 0 and con.rolled_back == 1 and con.closed
    if failure == 'schema':
        assert not any(s.startswith('DELETE') for s in con.statements)


def test_check_only_and_invalid_source_never_connect(tmp_path):
    result = tmp_path/'result'; (result/'analysis').mkdir(parents=True)
    source = result/'poi_teradata.csv'; save(source, [row()])
    (result/'analysis/summary.json').write_text(json.dumps({'status':'PRODUCT_SUCCESS', 'production_ready':True,
                                                         'file_checks_ok':True, 'total_objects':1}))

    def forbidden(**kw):
        pytest.fail('Unexpected connection')

    report = execute(tmp_path, check_only=True, connect=forbidden)
    assert report['status'] == 'TERADATA_CHECK_SUCCESS' and report['rows'] == 1
    assert report['unicode_pass_through_requested'] and not report['unicode_pass_through_enabled']
    save(source, [row(point_name='x'*256)])
    with pytest.raises(ValueError, match='POINT_NAME'):
        execute(tmp_path, connect=forbidden)
    assert source.exists()
    assert json.loads((result/'analysis/teradata_load.json').read_text())['status'] == 'TERADATA_FAILED'


def test_environment_password_has_priority(monkeypatch):
    monkeypatch.setenv('POI_TD_PASSWORD', 'example')
    assert password_for('user') == 'example'


def test_missing_columns(tmp_path):
    source = tmp_path/'bad.csv'; source.write_text('osm_id\n123\n')
    with pytest.raises(ValueError, match='Missing'):
        list(batches(source))


def test_commit_failure_reports_uncertain_outcome(tmp_path):
    source = tmp_path/'poi.csv'; save(source, [row()])
    con = Connection()

    def fail_commit():
        raise OSError('network lost')

    con.commit = fail_commit
    with pytest.raises(RuntimeError, match='COMMIT outcome unconfirmed'):
        sync_snapshot(source, 1, connect=lambda **kw: con, password='p@ss')
    assert con.rolled_back == 1 and con.closed


def test_execute_load_reports_success_and_keeps_source(tmp_path):
    result = tmp_path/'result'; (result/'analysis').mkdir(parents=True)
    source = result/'poi_teradata.csv'; save(source, [row()])
    saved = source.read_bytes()
    (result/'analysis/summary.json').write_text(json.dumps({'status':'PRODUCT_SUCCESS', 'production_ready':True,
                                                         'file_checks_ok':True, 'total_objects':1}))
    con = Connection()
    report = execute(tmp_path, connect=lambda **kw: con, password='p@ss')
    assert report['status'] == 'TERADATA_LOAD_SUCCESS' and con.committed == 1
    assert report['unicode_pass_through_enabled']
    assert source.read_bytes() == saved
    assert json.loads((result/'analysis/teradata_load.json').read_text()) == report


def test_point_name_255_is_preserved_and_other_limits_stay_100():
    name = 'я' * 255
    values = dict(zip(COLUMNS, convert(row(point_name=name, chain_name='с' * 100))))
    assert values['POINT_NAME'] == name
    with pytest.raises(ValueError, match=r'VARCHAR\(100\) exceeded: CHAIN_NAME'):
        convert(row(chain_name='с' * 101))


def test_extended_unicode_is_cleaned_only_in_upload_and_audited(tmp_path):
    from poi_osm.teradata_load import prepare_snapshot
    name = 'Ремонт телефонов 📱'
    source = tmp_path/'poi.csv'; save(source, [row(point_name=name), row(osm_id='124')])
    original = source.read_bytes()
    output = tmp_path/'unicode.csv'
    prepared = tmp_path/'prepared.csv'
    report = prepare_snapshot(source, prepared, output)
    assert report['changed_objects'] == 1
    assert report['replaced_characters'] == 1
    with output.open(encoding='utf-8-sig', newline='') as stream:
        entries = list(csv.DictReader(stream, delimiter=';'))
    assert entries[0]['osm_type_id'] == 'way/123' and entries[0]['column'] == 'POINT_NAME'
    assert entries[0]['codepoints'] == 'U+1F4F1'
    con = Connection()
    options = {}

    def connect(**kwargs):
        options.update(kwargs)
        return con

    sync_snapshot(prepared, 2, connect=connect, password='p@ss')
    assert options['charset'] == 'UTF8'
    assert con.statements[0] == 'SET SESSION CHARACTER SET UNICODE PASS THROUGH ON'
    assert con.pending[0][COLUMNS.index('POINT_NAME')] == 'Ремонт телефонов'
    assert source.read_bytes() == original


def test_unicode_session_setup_failure_stops_before_delete(tmp_path):
    source = tmp_path/'poi.csv'; save(source, [row()])
    con = Connection()
    original = con.cur.execute

    def execute_sql(sql):
        if sql.startswith('SET SESSION'):
            raise RuntimeError('Unicode Pass Through not available')
        original(sql)

    con.cur.execute = execute_sql
    with pytest.raises(RuntimeError, match='Unicode Pass Through not available'):
        sync_snapshot(source, 1, connect=lambda **kw: con, password='p@ss')
    assert not any(sql.startswith('DELETE') for sql in con.statements)
    assert con.rolled_back == 1 and con.committed == 0


def test_batch_diagnostics_are_read_before_rollback_and_masked(tmp_path):
    source = tmp_path/'poi.csv'; save(source, [row()])
    con = Connection(fail_insert=True)

    def native_sql(sql):
        assert con.rolled_back == 0
        assert sql == '{fn teradata_get_errors}'
        return 'row 1: invalid character p@ss'

    con.nativeSQL = native_sql
    with pytest.raises(RuntimeError) as error:
        sync_snapshot(source, 1, connect=lambda **kw: con, password='p@ss')
    assert 'NEW batch of 1 objects' in str(error.value)
    assert 'driver batch details: row 1: invalid character' in str(error.value)
    assert 'p@ss' not in str(error.value)
    assert con.rolled_back == 1


@pytest.mark.parametrize('name', ['bad\x00name', 'bad\ud800name'])
def test_invalid_unicode_rejected_before_database(name):
    with pytest.raises(ValueError, match='Invalid Unicode scalar or NUL: POINT_NAME'):
        convert(row(point_name=name))


@pytest.mark.parametrize('name,subcategory,expected,action', [
    ('📱📞', 'Ремонт телефонов', 'Ремонт телефонов', 'primary_subcategory_fallback'),
    ('👨\u200d💻\ufe0f', 'Компьютеры', 'Компьютеры', 'primary_subcategory_fallback'),
    ('📱', '', '', 'null_missing_subcategory'),
    ('Теле📱фон', 'Ремонт', 'Теле фон', 'replace_with_space'),
    ('📱', 'Компьютеры 💻', 'Компьютеры', 'primary_subcategory_fallback'),
])
def test_cleaning_and_subcategory_fallback(tmp_path, name, subcategory, expected, action):
    from poi_osm.teradata_load import prepare_snapshot
    source=tmp_path/'source.csv'; save(source, [row(point_name=name, primary_subcategory=subcategory)])
    prepared=tmp_path/'prepared.csv'; audit=tmp_path/'unicode.csv'
    stats=prepare_snapshot(source, prepared, audit)
    with prepared.open(encoding='utf-8-sig', newline='') as stream:
        loaded=list(csv.DictReader(stream, delimiter=';'))
    assert loaded[0]['point_name']==expected
    with audit.open(encoding='utf-8-sig', newline='') as stream:
        changes=list(csv.DictReader(stream, delimiter=';'))
    assert next(change for change in changes if change['column']=='POINT_NAME')['action']==action
    assert stats['changed_objects']==1
    assert list(batches(prepared))[0][0][COLUMNS.index('POINT_NAME')]==(expected or None)


def test_existing_empty_name_and_bmp_text_stay_unchanged(tmp_path):
    from poi_osm.teradata_load import prepare_snapshot
    source=tmp_path/'source.csv'; save(source, [row(point_name='', primary_subcategory='Ремонт'),
                                             row(osm_id='124', point_name='Связь №1 — 東京')])
    prepared=tmp_path/'prepared.csv'
    stats=prepare_snapshot(source, prepared, tmp_path/'unicode.csv')
    assert stats['changed_objects']==0 and stats['point_name_fallbacks']==0
    assert source.read_bytes()==prepared.read_bytes()


def test_execute_upload_uses_subcategory_for_emoji_only_name(tmp_path):
    result=tmp_path/'result'; (result/'analysis').mkdir(parents=True)
    source=result/'poi_teradata.csv'; save(source, [row(point_name='📱', primary_subcategory='Ремонт телефонов')])
    saved=source.read_bytes()
    (result/'analysis/summary.json').write_text(json.dumps({'status':'PRODUCT_SUCCESS', 'production_ready':True,
                                                         'file_checks_ok':True, 'total_objects':1}))
    con=Connection()
    report=execute(tmp_path, connect=lambda **kw: con, password='p@ss')
    assert con.pending[0][COLUMNS.index('POINT_NAME')]=='Ремонт телефонов'
    assert report['unicode_audit']['point_name_fallbacks']==1
    assert report['prepared_csv_sha256']!=report['source_sha256']
    assert source.read_bytes()==saved


def test_incremental_sync_writes_only_delta_and_handles_skipped_source_runs(tmp_path):
    source=tmp_path/'poi.csv'
    save(source,[row(osm_id='1',record_status='UNCHANGED'),
                 row(osm_id='2',point_name='Новое имя',record_status='UNCHANGED'),
                 row(osm_id='3',record_status='NEW'),row(osm_id='4',record_status='UNCHANGED')])
    old=[convert(row(osm_id='1',record_status='NEW')),convert(row(osm_id='2',point_name='Старое имя')),
         convert(row(osm_id='5'))]
    con=Connection(initial=old)
    report=sync_snapshot(source,4,connect=lambda **kw:con,password='p@ss',audit_path=tmp_path/'delta.csv')
    assert report['changes']=={'NEW':2,'UPDATED':1,'DELETED':1,'UNCHANGED':1}
    assert report['rows_sent']==4 and report['status_resets']==1 and report['target_before']==3 and report['target_after']==4
    values={value[2]:dict(zip(COLUMNS,value)) for value in con.pending}
    assert set(values)=={'way/1','way/2','way/3','way/4'}
    assert values['way/1']['RECORD_STATUS']=='UNCHANGED'
    assert values['way/2']['POINT_NAME']=='Новое имя' and values['way/2']['RECORD_STATUS']=='UPDATED'
    assert values['way/4']['RECORD_STATUS']=='NEW'
    assert all('WHERE "OSM_TYPE_ID" = ?' in sql for sql in con.statements if sql.startswith(('UPDATE','DELETE')) and 'SET "RECORD_STATUS"' not in sql)
    with (tmp_path/'delta.csv').open(encoding='utf-8-sig',newline='') as stream:
        audit={entry['osm_type_id']:entry for entry in csv.DictReader(stream,delimiter=';')}
    assert audit['way/2']['source_record_status']=='UNCHANGED' and audit['way/2']['action']=='UPDATED'
    assert 'way/1' not in audit
    con.statements.clear();con.batch_sizes.clear()
    again=sync_snapshot(source,4,connect=lambda **kw:con,password='p@ss')
    assert again['changes']=={'NEW':0,'UPDATED':0,'DELETED':0,'UNCHANGED':4}
    assert again['rows_sent']==0 and again['status_resets']==3
    assert all("""SET "RECORD_STATUS" = 'UNCHANGED'""" in sql for sql in con.statements if sql.startswith('UPDATE'))
    con.statements.clear();con.batch_sizes.clear()
    third=sync_snapshot(source,4,connect=lambda **kw:con,password='p@ss')
    assert third['rows_sent']==0 and not con.batch_sizes


def test_incremental_failure_rolls_back_delete_and_update_when_insert_fails(tmp_path):
    source=tmp_path/'poi.csv';save(source,[row(osm_id='1',point_name='Новое имя'),row(osm_id='3')])
    con=Connection(initial=[convert(row(osm_id='1',record_status='NEW')),convert(row(osm_id='2',record_status='UPDATED'))])
    before=con.pending
    original=con.cur.executemany
    def fail_insert(sql,values):
        if sql.startswith('INSERT'):raise RuntimeError('insert failed')
        return original(sql,values)
    con.cur.executemany=fail_insert
    with pytest.raises(RuntimeError,match='insert failed'):
        sync_snapshot(source,2,connect=lambda **kw:con,password='p@ss')
    assert con.pending==before
    assert con.rolled_back==1 and con.committed==0
    assert any(sql.startswith('DELETE') for sql in con.statements)
    assert any(sql.startswith('UPDATE') for sql in con.statements)


def test_duplicate_target_identity_is_rejected_before_dml(tmp_path):
    source=tmp_path/'poi.csv';save(source,[row()])
    values=convert(row())
    con=Connection(initial=[values,values])
    with pytest.raises(RuntimeError,match='Duplicate OSM_TYPE_ID in target'):
        sync_snapshot(source,1,connect=lambda **kw:con,password='p@ss')
    assert len(con.pending)==2 and con.rolled_back==1
    assert not any(sql.startswith(('INSERT','UPDATE','DELETE')) for sql in con.statements)


def test_category_change_keeps_object_and_ignores_status_only_transition(tmp_path):
    source=tmp_path/'poi.csv';save(source,[row(primary_subcategory='Ремонт',record_status='UPDATED')])
    con=Connection(initial=[convert(row(primary_subcategory='Электроника',record_status='NEW'))])
    report=sync_snapshot(source,1,connect=lambda **kw:con,password='p@ss')
    assert report['changes']=={'NEW':0,'UPDATED':1,'DELETED':0,'UNCHANGED':0}
    assert con.pending[0][COLUMNS.index('PRIMARY_SUBCATEGORY')]=='Ремонт'
    save(source,[row(primary_subcategory='Ремонт',record_status='UNCHANGED')])
    report=sync_snapshot(source,1,connect=lambda **kw:con,password='p@ss')
    assert report['changes']['UNCHANGED']==1 and report['rows_sent']==0 and report['status_resets']==1



def test_status_only_reset_sends_no_records_and_preserves_change_date(tmp_path):
    source=tmp_path/'poi.csv';save(source,[row(record_status='UNCHANGED')])
    con=Connection(initial=[convert(row(record_status='NEW'))])
    before=con.pending[0]
    report=sync_snapshot(source,1,connect=lambda **kw:con,password='p@ss')
    assert con.pending[0][:-1]==before[:-1]
    assert con.pending[0][-1]=='UNCHANGED'
    assert report['rows_sent']==0 and report['status_resets']==1 and report['unchanged_status_resets']==1
    assert not con.batch_sizes
    updates=[sql for sql in con.statements if sql.startswith('UPDATE')]
    assert len(updates)==1 and updates[0].count(' = ')==1 and 'SET "RECORD_STATUS"' in updates[0]
