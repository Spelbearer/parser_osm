"""Report probable duplicates without modifying the registry or making decisions."""
from __future__ import annotations

import csv
import math
import sqlite3
from pathlib import Path


def distance(lon1, lat1, lon2, lat2):
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = rlat2 - rlat1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 6371008.8 * 2 * math.asin(min(1.0, math.sqrt(a)))


def report_duplicates(db: sqlite3.Connection, path: Path, radius_m: float = 30):
    if not 0 < radius_m <= 1000:
        raise ValueError("duplicate_distance_m must be between 0 and 1000")
    db.create_function("normalized", 1, lambda s: " ".join(s.casefold().split()) if s else None, deterministic=True)
    db.execute("CREATE INDEX IF NOT EXISTS by_duplicate_name ON poi(tag,normalized(point_name))")
    cursor = db.execute("""SELECT osm_type,osm_id,tag,point_name,lon,lat,address FROM poi
        WHERE record_status!='DELETED' AND point_name IS NOT NULL ORDER BY tag,normalized(point_name),osm_type,osm_id""")
    # Nested cursors keep memory bounded even for very large chains with the same name.
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["object_a", "object_b", "tag", "point_name", "distance_m", "address_matches", "decision"])
        for typ, ident, tag, name, lon, lat, address in cursor:
            delta_lat = radius_m / 110000
            delta_lon = min(180, radius_m / (110000 * max(0.00001, math.cos(math.radians(lat)))))
            peers = db.execute("""SELECT osm_type,osm_id,lon,lat,address FROM poi WHERE tag=?
                AND normalized(point_name)=normalized(?) AND record_status!='DELETED'
                AND (osm_type,osm_id)>(?,?) AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?""",
                (tag, name, typ, ident, lat - delta_lat, lat + delta_lat, lon - delta_lon, lon + delta_lon))
            for btype, bid, blon, blat, baddr in peers:
                meters = distance(lon, lat, blon, blat)
                if meters <= radius_m:
                    writer.writerow([f"{typ}/{ident}", f"{btype}/{bid}", tag, name, round(meters, 2),
                                     bool(address and baddr and address != "{}" and baddr != "{}" and address.casefold() == baddr.casefold()), "REVIEW"])
    db.commit()
