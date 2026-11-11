import hashlib
import json

import osmium
import pytest
from shapely.geometry import shape

from poi_osm.boundary_recovery import recover


def fixture_sources(tmp_path, *, omit_way=False, omit_node=False, different_way=False, open_ring=False):
    paths = [tmp_path / 'part-a.osm.pbf', tmp_path / 'part-b.osm.pbf']
    tags = {'type':'boundary', 'boundary':'administrative', 'admin_level':'4', 'ISO3166-2':'RU-KIR', 'name':'Synthetic test only'}
    members = [('w',10,'outer'), ('w',11,'outer'), ('w',12,'outer'), ('w',13,'outer'), ('r',999,'subarea'), ('n',888,'admin_centre')]
    for index, path in enumerate(paths):
        with osmium.SimpleWriter(str(path)) as writer:
            for ident, xy in [(1,(0,0)), (2,(10,0)), (3,(10,10)), (4,(0,10))]:
                if (index == 0 and ident in {1,2}) or (index == 1 and ident in {3,4}):
                    if not (omit_node and ident == 4):
                        writer.add_node(osmium.osm.mutable.Node(id=ident, location=xy))
            edges = [(10,[1,2]),(11,[2,3])] if index == 0 else [(12,[3,4]),(13,[4,1])]
            for ident, refs in edges:
                if not (omit_way and ident == 13):
                    writer.add_way(osmium.osm.mutable.Way(id=ident, nodes=[4,2] if open_ring and ident == 13 else refs))
            if different_way and index == 1:
                writer.add_way(osmium.osm.mutable.Way(id=10, nodes=[1,3]))
            writer.add_relation(osmium.osm.mutable.Relation(id=115100, tags=tags, members=members))
    sources = [{'path':str(p), 'sha256':hashlib.sha256(p.read_bytes()).hexdigest(), 'extract':p.stem} for p in paths]
    (tmp_path / 'summary.json').write_text(json.dumps({'sources':sources,
        'boundary_report':{'missing_region_codes':['RU-KIR']}}), encoding='utf-8')
    return paths


def test_recover_across_clipped_extracts_and_ignore_subarea_metadata(tmp_path):
    fixture_sources(tmp_path)
    target, report = recover(tmp_path, tmp_path / 'checks')
    assert report['status'] == 'RECOVERED_CANDIDATES'
    assert report['recovered_region_codes'] == ['RU-KIR']
    assert report['regions']['RU-KIR']['missing_way_count'] == 0
    assert report['regions']['RU-KIR']['missing_node_count'] == 0
    document = json.loads((target / 'candidate.geojson').read_text(encoding='utf-8'))
    assert shape(document['features'][0]['geometry']).area == 100
    assert document['review_required'] is True
    assert len(document['source_sha256']) == 2
    assert not list(target.glob('*.idx'))


@pytest.mark.parametrize('option,field,ident', [('omit_way','missing_way_ids',13), ('omit_node','missing_node_ids',4)])
def test_missing_dependencies_are_reported_without_inventing_boundary(tmp_path, option, field, ident):
    fixture_sources(tmp_path, **{option:True})
    target, report = recover(tmp_path, tmp_path / 'checks')
    assert report['status'] == 'RECOVERY_INCOMPLETE'
    assert ident in report['regions']['RU-KIR'][field]
    assert report['regions']['RU-KIR']['status'] == 'INCOMPLETE_SOURCE_GEOMETRY'
    assert not json.loads((target / 'candidate.geojson').read_text())['features']


def test_conflicting_way_versions_refused(tmp_path):
    fixture_sources(tmp_path, different_way=True)
    with pytest.raises(ValueError, match='Conflicting way/10'):
        recover(tmp_path, tmp_path / 'checks')


def test_wrong_source_hash_refused(tmp_path):
    paths = fixture_sources(tmp_path)
    paths[0].write_bytes(b'changed')
    with pytest.raises(ValueError, match='Source changed'):
        recover(tmp_path, tmp_path / 'checks')


def test_all_dependencies_present_but_ring_is_open(tmp_path):
    fixture_sources(tmp_path, open_ring=True)
    _, report = recover(tmp_path, tmp_path / 'checks')
    item = report['regions']['RU-KIR']
    assert report['status'] == 'RECOVERY_INCOMPLETE'
    assert item['missing_way_count'] == item['missing_node_count'] == 0
    assert item['open_end_node_count'] == 2
    assert item['status'] == 'OPEN_BOUNDARY_RINGS'
