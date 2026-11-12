"""Deterministic compact POI zones with membership-based duplicate suppression."""
from __future__ import annotations

import hashlib
import logging
from collections import Counter, defaultdict

import numpy as np

LOG = logging.getLogger(__name__)


def mean_center(lons, lats, ids):
    longitude, latitude = np.radians(lons[ids]), np.radians(lats[ids])
    x = np.mean(np.cos(latitude) * np.cos(longitude))
    y = np.mean(np.cos(latitude) * np.sin(longitude))
    z = np.mean(np.sin(latitude))
    return float(np.degrees(np.arctan2(y, x))), float(np.degrees(np.arctan2(z, np.hypot(x, y))))


def select_zones(db, tree, lons, lats, name_ids, telecom, chains, config, neighbors, geod, stats):
    """Generate local candidates, then retain richest nonredundant groups.

    This is a bounded local search, not a globally optimal disk-cover solver.
    Every final membership is checked against the actual WGS84 center.
    """
    db.execute('CREATE TABLE zone_seeds(signature BLOB PRIMARY KEY) WITHOUT ROWID')
    db.execute('CREATE TABLE zone_candidates(signature BLOB PRIMARY KEY, lon REAL, lat REAL, names INTEGER, total INTEGER, telecom INTEGER, members BLOB) WITHOUT ROWID')
    radius, minimum = config['radius_m'], config['min_count']

    def metric(ids):
        return len(np.unique(name_ids[ids])) if config['count_mode'] == 'distinct_names' else len(ids)

    for seq, lon, lat in db.execute('SELECT MIN(seq),lon,lat FROM points GROUP BY lon,lat ORDER BY MIN(seq)'):
        stats['candidate_centers'] += 1
        if stats['candidate_centers'] % 5000 == 0:
            db.commit()
            LOG.info('Checked %d POI seeds for visit zones', stats['candidate_centers'])
        nearby = neighbors(tree, lon, lat, lons, lats, radius * 2)
        if metric(nearby) < minimum:
            continue
        # A second start between POIs finds groups whose center is not a shop.
        for center in [(lon, lat), mean_center(lons, lats, nearby)]:
            ids = neighbors(tree, *center, lons, lats, radius)
            if metric(ids) < minimum:
                continue
            signature = hashlib.sha256(ids.astype('<i8').tobytes()).digest()
            if not db.execute('INSERT OR IGNORE INTO zone_seeds VALUES (?)', (signature,)).rowcount:
                continue
            for _ in range(4):
                proposed = mean_center(lons, lats, ids)
                updated = neighbors(tree, *proposed, lons, lats, radius)
                if metric(updated) < minimum:
                    break
                center = proposed
                if np.array_equal(updated, ids):
                    ids = updated
                    break
                ids = updated
            # The final mean may lose a boundary member; keep a verified center
            # rather than declaring that member part of an oversized group.
            proposed = mean_center(lons, lats, ids)
            verified = neighbors(tree, *proposed, lons, lats, radius)
            if np.array_equal(verified, ids):
                center = proposed
            signature = hashlib.sha256(ids.astype('<i8').tobytes()).digest()
            names = len(np.unique(name_ids[ids]))
            db.execute('INSERT OR IGNORE INTO zone_candidates VALUES (?,?,?,?,?,?,?)',
                       (signature, *center, names, len(ids), int(telecom[ids].sum()), ids.astype('<i8').tobytes()))
    db.commit()
    stats['zone_candidates'] = db.execute('SELECT COUNT(*) FROM zone_candidates').fetchone()[0]
    stats['duplicate_centers_removed'] = len(lons) - stats['candidate_centers']
    accepted_by_point = defaultdict(list)
    sizes = []
    order = 'names DESC,total DESC' if config['count_mode'] == 'distinct_names' else 'total DESC,names DESC'
    query = 'SELECT signature,lon,lat,names,total,telecom,members FROM zone_candidates ORDER BY ' + order + ',telecom DESC,lon,lat,signature'
    for signature, lon, lat, names, total, telecom_count, blob in db.execute(query):
        ids = np.frombuffer(blob, dtype='<i8')
        overlaps = Counter(zone for ident in ids for zone in accepted_by_point[int(ident)])
        if any(overlap / min(total, sizes[zone]) >= config['zone_overlap_threshold'] for zone, overlap in overlaps.items()):
            stats['near_duplicate_zones_removed'] += 1
            continue
        zone_id = len(sizes)
        sizes.append(total)
        for ident in ids:
            accepted_by_point[int(ident)].append(zone_id)
        _, _, distances = geod.inv(np.full(total, lon), np.full(total, lat), lons[ids], lats[ids])
        if not np.all(np.asarray(distances) < radius):
            raise ValueError('Visit zone contains a point outside its configured radius')
        representative = int(ids[int(np.argmin(distances))])
        yield {'zone_id': 'zone-' + signature.hex()[:16], 'center_lon': lon, 'center_lat': lat,
               'radius_m': radius, 'distinct_names': names, 'poi_count': total,
               'telecom_count': telecom_count, 'chain_count': int(chains[ids].sum()),
               'representative': representative, 'members': ids}
