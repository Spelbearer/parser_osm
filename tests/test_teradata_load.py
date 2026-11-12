import csv
import json
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
    replace_snapshot,
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
    def __init__(self, con):
        self.con = con
        self.description = [(c,) for c in COLUMNS]
        self.result = (0,)

    def execute(self, sql):
        self.con.statements.append(sql)
        if sql.startswith('DELETE'):
            self.con.pending = []
        elif 'COUNT(DISTINCT' in sql:
            self.result = (len(self.con.pending), len({r[2] for r in self.con.pending}))
            if self.con.wrong_count:
                self.result = (0, 0)

    def executemany(self, sql, rows):
        self.con.batch_sizes.append(len(rows))
        if self.con.fail_insert:
            raise RuntimeError('driver error password=p@ss')
        self.con.pending.extend(rows)

    def fetchone(self):
        return self.result

    def close(self):
        pass


class Connection:
    def __init__(self, *, fail_insert=False, wrong_count=False):
        self.pending = [('old',)]
        self.committed = 0
        self.rolled_back = 0
        self.closed = False
        self.statements = []
        self.batch_sizes = []
        self.fail_insert = fail_insert
        self.wrong_count = wrong_count
        self.cur = Cursor(self)

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


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
    dict(lon='181'), dict(is_chain='yes'), dict(update_date='2026-02-30')])
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

    replace_snapshot(source, 10001, connect=connect, password='p@ss')
    assert con.autocommit is False and options['logmech'] == 'LDAP' and options['tmode'] == 'ANSI'
    assert options['host'] == 'td2800.corp.tele2.ru'
    assert con.batch_sizes == [10000, 1]
    assert con.committed == 1 and con.rolled_back == 0 and con.closed
    assert sum(sql.startswith('DELETE') for sql in con.statements) == 1


@pytest.mark.parametrize('failure', ['insert', 'count', 'schema'])
def test_failure_rolls_back_and_masks_password(tmp_path, failure):
    source = tmp_path/'poi.csv'; save(source, [row()])
    con = Connection(fail_insert=failure == 'insert', wrong_count=failure == 'count')
    if failure == 'schema':
        con.cur.description = [('OSM_ID',)]
    with pytest.raises(RuntimeError) as error:
        replace_snapshot(source, 1, connect=lambda **kw: con, password='p@ss')
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
        replace_snapshot(source, 1, connect=lambda **kw: con, password='p@ss')
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


def test_extended_unicode_is_preserved_and_audited(tmp_path):
    from poi_osm.teradata_load import audit_unicode
    name = 'Ремонт телефонов 📱'
    source = tmp_path/'poi.csv'; save(source, [row(point_name=name), row(osm_id='124')])
    original = source.read_bytes()
    output = tmp_path/'unicode.csv'
    report = audit_unicode(source, output)
    assert report['rows_with_extended_unicode'] == 1
    assert report['extended_characters'] == 1
    with output.open(encoding='utf-8-sig', newline='') as stream:
        entries = list(csv.DictReader(stream, delimiter=';'))
    assert entries[0]['osm_type_id'] == 'way/123' and entries[0]['column'] == 'POINT_NAME'
    assert entries[0]['codepoints'] == 'U+1F4F1'
    con = Connection()
    options = {}

    def connect(**kwargs):
        options.update(kwargs)
        return con

    replace_snapshot(source, 2, connect=connect, password='p@ss')
    assert options['charset'] == 'UTF8'
    assert con.statements[0] == 'SET SESSION CHARACTER SET UNICODE PASS THROUGH ON'
    assert con.pending[0][COLUMNS.index('POINT_NAME')] == name
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
        replace_snapshot(source, 1, connect=lambda **kw: con, password='p@ss')
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
        replace_snapshot(source, 1, connect=lambda **kw: con, password='p@ss')
    assert 'Batch object rows 1-1' in str(error.value)
    assert 'driver batch details: row 1: invalid character' in str(error.value)
    assert 'p@ss' not in str(error.value)
    assert con.rolled_back == 1


@pytest.mark.parametrize('name', ['bad\x00name', 'bad\ud800name'])
def test_invalid_unicode_rejected_before_database(name):
    with pytest.raises(ValueError, match='Invalid Unicode scalar or NUL: POINT_NAME'):
        convert(row(point_name=name))
