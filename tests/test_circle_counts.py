import csv

import pytest

from poi_osm.circle_counts import FIELDS, annotate, circle_key
from poi_osm.kml import GEOD, neighbors


def write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter=';')
        writer.writeheader()
        writer.writerows(rows)


def test_counts_include_unnamed_points_and_deduplicate_final_circles(tmp_path):
    source, catalog = tmp_path/'poi.csv', tmp_path/'circles.csv'
    x, y, _ = GEOD.fwd(37, 55, 90, 30)
    outside_x, outside_y, _ = GEOD.fwd(37, 55, 90, 1000)
    records = [dict(osm_type='node', osm_id='1', point_name='', lon=37, lat=55),
               dict(osm_type='way', osm_id='1', point_name='Магазин', lon=x, lat=y),
               dict(osm_type='node', osm_id='2', point_name='Вне', lon=outside_x, lat=outside_y)]
    write_csv(source, list(records[0]), records)
    circles = [dict(circle_id=circle_key(lon, lat, 60), lon=lon, lat=lat, radius_m=60)
               for lon, lat in [(37, 55), (37, 55), (x, y)]]
    write_csv(catalog, FIELDS, circles)
    report = annotate(source, catalog, neighbors)
    with source.open(encoding='utf-8-sig') as stream:
        result = list(csv.DictReader(stream, delimiter=';'))
    assert [row['CNT_CIRCLE'] for row in result] == ['2', '2', '0']
    assert [{k: v for k, v in row.items() if k != 'CNT_CIRCLE'} for row in result] == [
        {k: str(v) for k, v in row.items()} for row in records]
    assert report['unique_circles'] == 2 and report['memberships'] == 4
    assert report['points_in_circles'] == 2 and report['max_count'] == 2


def test_original_coordinate_distance_and_dateline(tmp_path):
    source, catalog = tmp_path/'poi.csv', tmp_path/'circles.csv'
    lon, lat = 179.9999, 55
    positions = [GEOD.fwd(lon, lat, 90, distance)[:2] for distance in [59.99, 60.01]]
    write_csv(source, ['lon', 'lat'], [dict(lon=x, lat=y) for x, y in positions])
    write_csv(catalog, FIELDS, [dict(circle_id=circle_key(lon, lat, 60), lon=lon, lat=lat, radius_m=60)])
    annotate(source, catalog, neighbors)
    with source.open(encoding='utf-8-sig') as stream:
        assert [row['CNT_CIRCLE'] for row in csv.DictReader(stream, delimiter=';')] == ['1', '0']


@pytest.mark.parametrize('geometry', [(0, 91, 60), (0, 0, -1), (float('nan'), 0, 60)])
def test_invalid_catalog_keeps_source_unchanged(tmp_path, geometry):
    source, catalog = tmp_path/'poi.csv', tmp_path/'circles.csv'
    write_csv(source, ['lon', 'lat'], [dict(lon=0, lat=0)])
    original = source.read_bytes()
    lon, lat, radius = geometry
    write_csv(catalog, FIELDS, [dict(circle_id=circle_key(lon, lat, radius), lon=lon, lat=lat, radius_m=radius)])
    with pytest.raises(ValueError):
        annotate(source, catalog, neighbors)
    assert source.read_bytes() == original
