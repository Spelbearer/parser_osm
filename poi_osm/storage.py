from __future__ import annotations

import csv
import hashlib
import json
import math
import sqlite3
from datetime import date
from pathlib import Path

from shapely import from_wkt

FIELDS = ["tag", "osm_id", "point_name", "lon", "lat", "wkt", "region_name", "region_code",
          "osm_type", "update_date", "osm_key", "osm_value", "source_date", "first_seen_date",
          "last_seen_date", "record_status", "source_file", "region_id", "country", "address"]
CONTENT = [k for k in FIELDS if k not in {"update_date", "source_date", "first_seen_date",
                                        "last_seen_date", "record_status", "source_file"}]
KEY = ("osm_type", "osm_id", "tag")


class Registry:
    def __init__(self, path: Path, mapping: dict, date: str, source: dict, batch_size: int = 5000):
        self.db = sqlite3.connect(path, uri=True)
        self.db.execute("PRAGMA cache_size=-32768")
        self.db.execute("PRAGMA temp_store=FILE")
        columns = ",".join(f'{k} {"REAL" if k in {"lon", "lat"} else "TEXT"}' for k in FIELDS)
        self.db.execute(f"CREATE TABLE poi ({columns},fingerprint TEXT NOT NULL, PRIMARY KEY(osm_type,osm_id,tag))")
        self.db.execute("CREATE TABLE changes(osm_type TEXT,osm_id TEXT,tag TEXT,status TEXT,PRIMARY KEY(osm_type,osm_id,tag))")
        self.mapping, self.date, self.source = mapping, date, source
        self.batch_size, self.pending = batch_size, 0

    def insert(self, row: dict):
        row = dict(row)
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
                   source_file=Path(self.source["path"]).name, first_seen_date=self.date,
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
        return {"total": total, "nameless_fraction": nameless / total if total else 0,
                "unassigned_fraction": unassigned / total if total else 0,
                "by_region": regions, "changes": changes}

    def export(self, run: Path, region_codes: list[str], parquet: bool):
        (run / "full").mkdir()
        (run / "delta").mkdir()
        for code in region_codes + [""]:
            self._csv(run / "full" / f"{code or 'unassigned'}.csv",
                      "SELECT " + ",".join(FIELDS) + " FROM poi WHERE record_status!='DELETED' AND COALESCE(region_code,'')=? ORDER BY osm_type,osm_id,tag", (code,))
        for status, filename in [("NEW", "new_poi"), ("UPDATED", "updated_poi"), ("DELETED", "deleted_poi")]:
            self._csv(run / "delta" / (filename + ".csv"), "SELECT " + ",".join("p." + k for k in FIELDS) +
                      " FROM poi p JOIN changes c USING(osm_type,osm_id,tag) WHERE c.status=? ORDER BY p.osm_type,p.osm_id,p.tag", (status,))
        if parquet:
            import pyarrow as pa
            import pyarrow.parquet as pq
            dates = {"update_date", "source_date", "first_seen_date", "last_seen_date"}
            schema = pa.schema([(k, pa.float64() if k in {"lon", "lat"} else pa.date32() if k in dates else pa.string()) for k in FIELDS])
            cursor = self.db.execute("SELECT " + ",".join(FIELDS) + " FROM poi WHERE record_status!='DELETED' AND COALESCE(region_code,'')!=''")
            count = 0
            with pq.ParquetWriter(run / "all_russia.parquet", schema, compression="zstd") as writer:
                while rows := cursor.fetchmany(self.batch_size):
                    records = [{k: date.fromisoformat(v) if k in dates and v else v for k, v in zip(FIELDS, row)} for row in rows]
                    writer.write_table(pa.Table.from_pylist(records, schema=schema))
                    count += len(rows)
            saved = pq.ParquetFile(run / "all_russia.parquet")
            decoded_count = sum(batch.num_rows for batch in saved.iter_batches(batch_size=self.batch_size))
            if saved.metadata.num_rows != count or decoded_count != count or saved.schema_arrow != schema:
                raise ValueError("Parquet verification failed")

    def _csv(self, path: Path, query: str, args: tuple):
        count = 0
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter=";")
            writer.writerow(FIELDS)
            cursor = self.db.execute(query, args)
            while rows := cursor.fetchmany(self.batch_size):
                writer.writerows(rows)
                count += len(rows)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, delimiter=";")
            if next(reader) != FIELDS or sum(1 for row in reader if len(row) == len(FIELDS)) != count:
                raise ValueError(f"CSV verification failed: {path}")

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
