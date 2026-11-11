from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path

from shapely import from_wkt

from .classification import default_classifier

FIELDS = ["tag", "osm_id", "point_name", "lon", "lat", "wkt", "region_name", "region_code",
          "osm_type", "update_date", "osm_key", "osm_value", "source_date", "first_seen_date",
          "last_seen_date", "record_status", "source_file", "region_id", "country", "address",
          "category", "subcategory", "is_telecom", "telecom_type", "telecom_evidence",
          "brand", "operator", "is_chain", "chain_id", "chain_name", "chain_status", "chain_evidence"]
BOOLEAN_FIELDS = {"is_telecom", "is_chain"}
CONTENT = [k for k in FIELDS if k not in {"update_date", "source_date", "first_seen_date",
                                        "last_seen_date", "record_status", "source_file"}]
KEY = ("osm_type", "osm_id", "tag")


class Registry:
    def __init__(self, path: Path, mapping: dict, date: str, source: dict, batch_size: int = 5000):
        self.db = sqlite3.connect(path, uri=True)
        self.db.execute("PRAGMA cache_size=-32768")
        self.db.execute("PRAGMA temp_store=FILE")
        columns = ",".join(f'{k} {"REAL" if k in {"lon", "lat"} else "INTEGER" if k in BOOLEAN_FIELDS else "TEXT"}' for k in FIELDS)
        self.db.execute(f"CREATE TABLE poi ({columns},fingerprint TEXT NOT NULL, PRIMARY KEY(osm_type,osm_id,tag))")
        self.db.execute("CREATE TABLE changes(osm_type TEXT,osm_id TEXT,tag TEXT,status TEXT,PRIMARY KEY(osm_type,osm_id,tag))")
        self.mapping, self.date, self.source = mapping, date, source
        self.batch_size, self.pending = batch_size, 0

    def insert(self, row: dict, *, source_file: str | None = None):
        row = dict(row)
        if "category" not in row:
            row.update(default_classifier().classify({row.get("osm_key", ""): row.get("osm_value", ""),
                                                     "name": row.get("point_name") or ""}, row.get("tag", "")))
        if row.get("osm_type") not in {"node", "way", "relation"} or not str(row.get("osm_id", "")).isdigit():
            raise ValueError("Invalid OSM identity")
        if row.get("osm_key") not in self.mapping.get(row.get("tag"), []) or row.get("osm_value") != row["tag"]:
            raise ValueError("POI does not match configured category")
        lon, lat = row["lon"], row["lat"]
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("Coordinates outside EPSG:4326")
        geom = from_wkt(row["wkt"])
        if geom.geom_type != "Point" or not geom.is_valid or abs(geom.x - lon) > 1e-7 or abs(geom.y - lat) > 1e-7:
            raise ValueError("Invalid or inconsistent WKT")
        row.update(update_date=self.date, source_date=self.source["source_date"],
                   source_file=source_file or Path(self.source["path"]).name, first_seen_date=self.date,
                   last_seen_date=self.date, record_status="NEW")
        fingerprint = hashlib.sha256(json.dumps({k: row.get(k) for k in CONTENT}, sort_keys=True,
                                                 ensure_ascii=False).encode()).hexdigest()
        values = [row.get(k) for k in FIELDS] + [fingerprint]
        try:
            self.db.execute(f'INSERT INTO poi VALUES ({",".join("?" for _ in values)})', values)
        except sqlite3.IntegrityError:
            old = self.db.execute("SELECT fingerprint FROM poi WHERE osm_type=? AND osm_id=? AND tag=?",
                                  [row[k] for k in KEY]).fetchone()
            if not old or old[0] != fingerprint:
                raise ValueError(f"Conflicting technical duplicate: {[row[k] for k in KEY]}")
        self.pending += 1
        if self.pending >= self.batch_size:
            self.db.commit()
            self.pending = 0

    def compare(self, previous: Path | None):
        self.db.commit()
        if previous:
            # read-only URI prevents accidentally creating/replacing a missing previous registry.
            self.db.execute("ATTACH DATABASE ? AS old", (previous.resolve().as_uri() + "?mode=ro",))
            old_columns = {r[1] for r in self.db.execute("PRAGMA old.table_info(poi)")}
            if not set(FIELDS) <= old_columns:
                raise ValueError("Previous registry uses an older schema. Use a new project_root for a new baseline; preserve previous outputs.")
            match = "o.osm_type=poi.osm_type AND o.osm_id=poi.osm_id AND o.tag=poi.tag"
            self.db.execute(f"""UPDATE poi SET
                first_seen_date=COALESCE((SELECT o.first_seen_date FROM old.poi o WHERE {match}),first_seen_date),
                record_status=CASE
                  WHEN NOT EXISTS(SELECT 1 FROM old.poi o WHERE {match} AND o.record_status!='DELETED') THEN 'NEW'
                  WHEN fingerprint=(SELECT o.fingerprint FROM old.poi o WHERE {match}) THEN 'UNCHANGED'
                  ELSE 'UPDATED' END""")
            self.db.execute("INSERT INTO changes SELECT osm_type,osm_id,tag,record_status FROM poi WHERE record_status!='UNCHANGED'")
            self.db.execute("""INSERT INTO changes SELECT o.osm_type,o.osm_id,o.tag,'DELETED' FROM old.poi o
                WHERE o.record_status!='DELETED' AND NOT EXISTS
                (SELECT 1 FROM poi p WHERE p.osm_type=o.osm_type AND p.osm_id=o.osm_id AND p.tag=o.tag)""")
            columns = ",".join(FIELDS + ["fingerprint"])
            selected = ",".join("?" if k == "update_date" else "'DELETED'" if k == "record_status" else f"o.{k}"
                                for k in FIELDS + ["fingerprint"])
            self.db.execute(f"""INSERT INTO poi({columns}) SELECT {selected} FROM old.poi o WHERE NOT EXISTS
                (SELECT 1 FROM poi p WHERE p.osm_type=o.osm_type AND p.osm_id=o.osm_id AND p.tag=o.tag)""", (self.date,))
        else:
            self.db.execute("INSERT INTO changes SELECT osm_type,osm_id,tag,'NEW' FROM poi")
        self.db.commit()

    def metrics(self) -> dict:
        total, nameless, unassigned = self.db.execute("""SELECT COUNT(*),COALESCE(SUM(point_name IS NULL),0),
            COALESCE(SUM(region_code IS NULL OR region_code=''),0) FROM poi WHERE record_status!='DELETED'""").fetchone()
        regions = dict(self.db.execute("SELECT COALESCE(region_code,''),COUNT(*) FROM poi WHERE record_status!='DELETED' GROUP BY region_code"))
        changes = dict(self.db.execute("SELECT status,COUNT(*) FROM changes GROUP BY status"))
        changes["UNCHANGED"] = self.db.execute("SELECT COUNT(*) FROM poi WHERE record_status='UNCHANGED'").fetchone()[0]
        unique = self.db.execute("SELECT COUNT(*) FROM (SELECT osm_type,osm_id FROM poi WHERE record_status!='DELETED' GROUP BY osm_type,osm_id)").fetchone()[0]
        return {"total": total, "nameless_fraction": nameless / total if total else 0,
                "category_rows": total, "unique_objects": unique, "extra_category_rows": total - unique,
                "unassigned_fraction": unassigned / total if total else 0,
                "by_region": regions, "changes": changes,
                "by_category": dict(self.db.execute("SELECT category,COUNT(*) FROM poi WHERE record_status!='DELETED' GROUP BY category")),
                "telecom_total": self.db.execute("SELECT COUNT(*) FROM poi WHERE record_status!='DELETED' AND is_telecom=1").fetchone()[0],
                "by_chain": dict(self.db.execute("SELECT chain_name,COUNT(*) FROM poi WHERE record_status!='DELETED' AND is_chain=1 GROUP BY chain_name")),
                "chain_not_identified_total": self.db.execute("SELECT COUNT(*) FROM poi WHERE record_status!='DELETED' AND chain_status='not_identified'").fetchone()[0],
                "chain_conflict_total": self.db.execute("SELECT COUNT(*) FROM poi WHERE record_status!='DELETED' AND chain_status='conflict'").fetchone()[0]}

    def close(self):
        self.db.close()


def check_quality(current: dict, previous: dict | None, quality: dict):
    if not current["total"] and not quality.get("allow_empty", False):
        raise ValueError("Empty snapshot rejected")
    if current["unassigned_fraction"] > quality["max_unassigned_fraction"]:
        raise ValueError("Too many objects without an unambiguous region; see review.csv")
    if previous:
        old_total = previous["total"]
        if old_total and current["total"] < old_total * (1 - quality["max_count_drop_fraction"]):
            raise ValueError("Anomalous total count decrease; previous publication retained")
        for code, old in previous["by_region"].items():
            if old and current["by_region"].get(code, 0) < old * (1 - quality["max_region_drop_fraction"]):
                raise ValueError(f"Anomalous count decrease in {code}; previous publication retained")
