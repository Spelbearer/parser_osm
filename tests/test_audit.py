import csv
import json
from pathlib import Path

import pytest
from helpers import run_collection

from poi_osm.audit import audit
from poi_osm.region_files import split_regions


def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter=';')
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def completed_run(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    rows = [dict(osm_type='node', osm_id='1', lon='1', lat='2', wkt='POINT (1 2)', region_code='RU-AA',
                 primary_tag='mobile_phone', all_tags='["mobile_phone", "repair"]', is_telecom='1', point_name='Магазин;\nтелефонов'),
            dict(osm_type='way', osm_id='1', lon='3', lat='4', wkt='POINT (3 4)', region_code='',
                 primary_tag='bakery', all_tags='["bakery"]', is_telecom='0', point_name='Пекарня')]
    write_csv(run / 'poi_teradata.csv', rows, list(rows[0]))
    regional = split_regions(run / 'poi_teradata.csv', run / 'regions',
                             {'regions': [{'region_code':'RU-AA', 'region_name':'Регион А'},
                                          {'region_code':'RU-BB', 'region_name':'Регион Б'}]}, 1)
    summary = {'status':'COLLECTION_PREVIEW_INCOMPLETE', 'production':False,
               'boundary_report':{'missing_region_codes':['RU-BB']}, 'unresolved_objects':0,
               'object_export':{'teradata_objects':2, 'teradata_osm_types':['node','way']},
               'regional_export':regional}
    # Files produced on Windows must also be understood by Linux audits.
    with (run / 'regions_index.csv').open(encoding='utf-8-sig', newline='') as stream:
        index = list(csv.DictReader(stream, delimiter=';'))
    for row in index:
        row['file'] = row['file'].replace('/', '\\')
    write_csv(run / 'regions_index.csv', index, list(index[0]))
    (run / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
    return run, rows


def test_valid_incomplete_run_and_composite_keys(completed_run, tmp_path):
    run, _ = completed_run
    path, report = audit(run, tmp_path / 'checks')
    assert path.is_file()
    assert report['file_checks_ok']
    assert not report['production_ready']
    assert report['counts']['unique_keys'] == 2  # node/1 and way/1 differ
    assert report['counts']['telecom_objects'] == 1
    assert report['counts']['unassigned'] == 1
    assert report['counts']['empty_region_files'] == 1
    assert report['geography']['missing_region_codes'] == ['RU-BB']
    assert not list((tmp_path / 'checks').glob('audit-*/keys.sqlite'))


def test_duplicate_and_broken_geometry_detected(completed_run, tmp_path):
    run, rows = completed_run
    rows[0]['wkt'] = 'POINT (90 80)'
    rows[0]['all_tags'] = '["repair"]'
    write_csv(run / 'poi_teradata.csv', rows + [rows[0]], list(rows[0]))
    _, report = audit(run, tmp_path / 'checks')
    assert not report['file_checks_ok']
    assert report['errors']['duplicate_osm_key'] == 1
    assert report['errors']['invalid_coordinates_or_wkt'] == 2
    assert report['errors']['invalid_tag_selection'] == 2
    assert report['errors']['summary_row_count'] == 1
    assert report['errors']['region_row_differs_from_main'] == 1


def test_region_copies_must_contain_each_object_once(completed_run, tmp_path):
    run, rows = completed_run
    part = next((run / 'regions').glob('RU-AA*'))
    write_csv(part, [rows[1], rows[1]], list(rows[0]))
    _, report = audit(run, tmp_path / 'checks')
    assert report['errors']['objects_missing_from_parts'] == 1
    assert report['errors']['objects_repeated_in_parts'] == 1
    assert report['errors']['region_code_disagreement'] == 2
    assert report['errors']['region_row_count'] == 1


def test_audit_actual_collection_export(collection, tmp_path):
    summary = run_collection(collection)
    _, report = audit(Path(summary['output']), tmp_path / 'checks')
    assert report['file_checks_ok'], report['errors']
    assert report['counts']['rows'] == summary['object_export']['teradata_objects']
    assert report['counts']['region_files'] == len(summary['regional_export']['files'])
    assert not report['production_ready']
