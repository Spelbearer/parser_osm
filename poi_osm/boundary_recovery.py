"""Recover missing candidate boundaries from matching local collection extracts."""
from __future__ import annotations

import argparse
import json
import logging
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import osmium
from shapely import wkb
from shapely.errors import GEOSException
from shapely.geometry import LineString, mapping
from shapely.prepared import prep

from .geography import split_dateline
from .runtime import atomic_json, configure_utf8
from .sources import _hash_file

LOG = logging.getLogger(__name__)


def recover(run, output):
    run = Path(run)
    summary = json.loads((run / 'summary.json').read_text(encoding='utf-8-sig'))
    missing = set(summary.get('boundary_report', {}).get('missing_region_codes', []))
    if not missing:
        raise ValueError('No missing region codes in this collection summary')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    target = Path(output) / ('boundary-recovery-' + stamp)
    target.mkdir(parents=True, exist_ok=False)
    report = {'status':'RUNNING', 'run':str(run), 'missing_region_codes':sorted(missing),
              'regions':{}, 'review_required':True, 'source_sha256':{}}
    sources = summary['sources']
    for source in sources:
        LOG.info('CHECK %s', source['path'])
        if _hash_file(Path(source['path'])) != source['sha256']:
            raise ValueError(f"Source changed since collection: {source['path']}")
        report['source_sha256'][source['extract']] = source['sha256']

    relations, ways, nodes = {}, {}, {}
    needed_ways, needed_nodes = set(), set()

    class Relations(osmium.SimpleHandler):
        def relation(self, relation):
            tags = dict(relation.tags)
            code = tags.get('ISO3166-2')
            if code not in missing or tags.get('boundary') != 'administrative' or tags.get('admin_level') != '4':
                return
            record = {'id':relation.id, 'tags':tags,
                      'members':[(m.type, m.ref, m.role) for m in relation.members]}
            if code in relations and relations[code] != record:
                raise ValueError(f'Conflicting boundary relation versions for {code}')
            relations[code] = record

    class Ways(osmium.SimpleHandler):
        def way(self, way):
            refs = [n.ref for n in way.nodes]
            if way.id in ways and ways[way.id] != refs:
                raise ValueError(f'Conflicting way/{way.id} versions across extracts')
            ways[way.id] = refs
            needed_nodes.update(refs)

    class Nodes(osmium.SimpleHandler):
        def node(self, node):
            xy = (node.location.lon, node.location.lat)
            if node.id in nodes and nodes[node.id] != xy:
                raise ValueError(f'Conflicting node/{node.id} versions across extracts')
            nodes[node.id] = xy

    def scan(kind, handler, ids=None):
        for index, source in enumerate(sources, 1):
            LOG.info('%s %s/%s: %s', kind.name, index, len(sources), source['path'])
            filters = [osmium.filter.IdFilter(ids)] if ids is not None else [osmium.filter.KeyFilter('ISO3166-2')]
            with osmium.io.Reader(source['path'], kind) as reader:
                osmium.apply(reader, *filters, handler)

    bits = osmium.osm.osm_entity_bits
    scan(bits.RELATION, Relations())
    for code, relation in relations.items():
        # subarea/admin_centre/label members are administrative metadata, not rings.
        unsupported = [(kind, ref, role) for kind, ref, role in relation['members']
                       if role in {'outer','inner',''} and kind != 'w']
        members = [(kind, ref, role) for kind, ref, role in relation['members']
                   if kind == 'w' and role in {'outer','inner',''}]
        report['regions'][code] = {'relation_id':relation['id'], 'geometric_way_count':len(members),
                                   'unsupported_geometric_members':unsupported}
        relation['geometry_members'] = members
        needed_ways.update(ref for _, ref, _ in members)
    if needed_ways:
        scan(bits.WAY, Ways(), needed_ways)
    if needed_nodes:
        scan(bits.NODE, Nodes(), needed_nodes)
    LOG.info('Boundary subset: %s ways, %s nodes', len(ways), len(nodes))
    complete = {}
    for code in sorted(missing):
        item = report['regions'].setdefault(code, {})
        if code not in relations:
            item['status'] = 'RELATION_NOT_FOUND'
            continue
        relation = relations[code]
        absent_ways = sorted({ref for _, ref, _ in relation['geometry_members']} - ways.keys())
        refs = {node for _, ref, _ in relation['geometry_members'] for node in ways.get(ref, [])}
        absent_nodes = sorted(refs - nodes.keys())
        endpoints = {"outer":Counter(), "inner":Counter()}
        for _, ident, role in relation['geometry_members']:
            values = ways.get(ident, [])
            if values and values[0] != values[-1]:
                endpoints["inner" if role == "inner" else "outer"].update((values[0], values[-1]))
        open_ends = sorted({node for values in endpoints.values() for node, degree in values.items() if degree % 2})
        item.update(open_end_node_count=len(open_ends), open_end_node_ids=open_ends[:100])
        item.update(missing_way_count=len(absent_ways), missing_way_ids=absent_ways[:100],
                    missing_node_count=len(absent_nodes), missing_node_ids=absent_nodes[:100])
        if item['unsupported_geometric_members']:
            item['status'] = 'UNSUPPORTED_GEOMETRIC_MEMBERS'
        elif absent_ways or absent_nodes:
            item['status'] = 'INCOMPLETE_SOURCE_GEOMETRY'
        elif open_ends:
            item['status'] = 'OPEN_BOUNDARY_RINGS'
        elif not relation['geometry_members']:
            item['status'] = 'NO_GEOMETRIC_WAYS'
        else:
            complete[relation['id']] = (code, relation)
            item['status'] = 'ASSEMBLY_PENDING'
    subset = target / 'boundary-subset.osm.pbf'
    selected_ways = {ref for _, relation in complete.values() for _, ref, _ in relation['geometry_members']}
    selected_nodes = {ref for ident in selected_ways for ref in ways[ident]}
    with osmium.SimpleWriter(str(subset)) as writer:
        for ident in sorted(selected_nodes):
            writer.add_node(osmium.osm.mutable.Node(id=ident, location=nodes[ident]))
        for ident in sorted(selected_ways):
            writer.add_way(osmium.osm.mutable.Way(id=ident, nodes=ways[ident]))
        for ident, (_, relation) in sorted(complete.items()):
            writer.add_relation(osmium.osm.mutable.Relation(id=ident, tags=relation['tags'], members=relation['geometry_members']))
    features = []
    factory = osmium.geom.WKBFactory()

    class Areas(osmium.SimpleHandler):
        def area(self, area):
            if area.from_way() or area.orig_id() not in complete:
                return
            code, relation = complete[area.orig_id()]
            item = report['regions'][code]
            try:
                raw = wkb.loads(factory.create_multipolygon(area), hex=True)
                boundary = prep(raw.boundary)
                for _, ident, _ in relation['geometry_members']:
                    refs = ways[ident]
                    if len(refs) < 2 or not boundary.covers(LineString([nodes[ref] for ref in refs])):
                        raise ValueError(f'Assembled boundary does not contain all segments of way/{ident}')
                geometry = split_dateline(raw)
                if geometry.is_empty or not geometry.is_valid:
                    raise ValueError('Invalid assembled polygon')
                features.append({'type':'Feature', 'properties':{'region_code':code, 'osm_id':str(relation['id']),
                                 'osm_type':'relation'}, 'geometry':mapping(geometry)})
                item.update(status='RECOVERED_CANDIDATE', bounds=list(geometry.bounds))
            except (RuntimeError, ValueError, GEOSException) as error:
                item.update(status='INVALID_ASSEMBLED_GEOMETRY', error=str(error))

    if complete:
        # The subset contains only boundary nodes, so a small in-memory index is sufficient.
        try:
            Areas().apply_file(str(subset), locations=True, idx='flex_mem')
        except RuntimeError as error:
            for item in report['regions'].values():
                if item['status'] == 'ASSEMBLY_PENDING':
                    item.update(status='ASSEMBLY_ERROR', error=str(error))
    for item in report['regions'].values():
        if item['status'] == 'ASSEMBLY_PENDING':
            item['status'] = 'AREA_NOT_ASSEMBLED'
    report.update(status='RECOVERED_CANDIDATES' if len(features) == len(missing) else 'RECOVERY_INCOMPLETE',
                  recovered_region_codes=sorted(f['properties']['region_code'] for f in features),
                  boundary_way_count=len(ways), boundary_node_count=len(nodes))
    atomic_json(target / 'candidate.geojson', {'type':'FeatureCollection', 'features':features,
                'source':'Unreviewed candidate boundaries assembled from matching local extracts',
                'review_required':True, 'license':'ODbL-1.0; © OpenStreetMap contributors',
                'source_sha256':report['source_sha256']})
    atomic_json(target / 'report.json', report)
    return target, report


def main(argv=None):
    configure_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    output, report = recover(args.run, args.output_dir)
    print(json.dumps({'report':str(output / 'report.json'), 'status':report['status'],
                      'recovered_region_codes':report['recovered_region_codes']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
