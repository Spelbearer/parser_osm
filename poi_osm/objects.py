"""One row per (osm_type, osm_id), preserving all category assignments."""
from __future__ import annotations

import csv
import json
from datetime import date
from itertools import groupby
from pathlib import Path

from .selection import TERADATA_FIELDS, PrimarySelector
from .storage import BOOLEAN_FIELDS, FIELDS

MULTI_FIELDS = {"tag", "osm_key", "osm_value", "category", "subcategory", "telecom_type", "telecom_evidence", "chain_evidence"}
INVARIANTS = {"point_name", "lon", "lat", "wkt", "region_name", "region_code", "region_id", "country", "address",
              "brand", "operator", "source_date", "source_file"}
ASSIGNMENT_FIELDS = ["tag", "osm_key", "osm_value", "category", "subcategory", "is_telecom", "telecom_type",
                     "telecom_evidence", "chain_id", "chain_name", "chain_status", "chain_evidence",
                     "record_status", "first_seen_date", "last_seen_date"]


def collapse(rows):
    rows = list(rows)  # At most the configured category count for one object.
    result = dict(rows[0])
    for field in INVARIANTS:
        if any(r[field] != result[field] for r in rows):
            raise ValueError(f"Conflicting object field {field}: {result['osm_type']}/{result['osm_id']}")
    for field in MULTI_FIELDS:
        result[field] = " | ".join(sorted({str(r[field]) for r in rows if r[field] not in (None, "")})) or None
    result["is_telecom"] = True if any(r["is_telecom"] for r in rows) else (False if all(r["is_telecom"] is not None for r in rows) else None)
    chain_ids = {r["chain_id"] for r in rows if r["chain_id"]}
    if len(chain_ids) > 1 or any(r["chain_status"] == "conflict" for r in rows):
        result.update(is_chain=None, chain_id=None, chain_name=None, chain_status="conflict")
    elif chain_ids:
        chain = next(r for r in rows if r["chain_id"])
        result.update(is_chain=True, chain_id=chain["chain_id"], chain_name=chain["chain_name"], chain_status="identified")
    else:
        result.update(is_chain=None, chain_id=None, chain_name=None, chain_status="not_identified")
    statuses = {r["record_status"] for r in rows}
    result["record_status"] = next(iter(statuses)) if len(statuses) == 1 else "UPDATED"
    result["first_seen_date"] = min((r["first_seen_date"] for r in rows if r["first_seen_date"]), default=None)
    for field in ("last_seen_date", "update_date"):
        result[field] = max((r[field] for r in rows if r[field]), default=None)
    result.update(osm_uid=f"{result['osm_type']}/{result['osm_id']}", tag_count=len(rows),
                  classifications=json.dumps([{k: bool(r[k]) if k in BOOLEAN_FIELDS and r[k] is not None else r[k]
                                                for k in ASSIGNMENT_FIELDS} for r in rows], ensure_ascii=False))
    return result


def object_rows(db):
    columns = {r[1] for r in db.execute("PRAGMA table_info(poi)")}
    if not {"osm_type", "osm_id", "tag", "lon", "lat", "wkt", "record_status"} <= columns:
        raise ValueError("Input is not a compatible POI registry")
    selection = ",".join(k if k in columns else "NULL AS " + k for k in FIELDS)
    has_changes = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='changes'").fetchone()
    removed = "EXISTS(SELECT 1 FROM changes c WHERE c.osm_type=poi.osm_type AND c.osm_id=poi.osm_id AND c.status='DELETED')" if has_changes else "0"
    cursor = db.execute(f"SELECT {selection},{removed} FROM poi WHERE record_status!='DELETED' ORDER BY osm_type,osm_id,tag")
    rows = (dict(zip([*FIELDS, "_removed_assignment"], row)) for row in cursor)
    for _, group in groupby(rows, key=lambda r: (r["osm_type"], r["osm_id"])):
        result = collapse(group)
        # A surviving point also changes when one of its categories disappears.
        if result.pop('_removed_assignment', False):
            result['record_status'] = 'UPDATED'
        yield result


def export_objects(db, output: Path, *, parquet=True, batch_size=5000, selection_rules=None):
    """Write the canonical warehouse source; all original assignments stay in SQLite."""
    from contextlib import ExitStack
    selector = PrimarySelector(selection_rules)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    targets = [output / "poi_teradata.csv"] + ([output / "poi_teradata.parquet"] if parquet else [])
    if any(path.exists() for path in targets):
        raise FileExistsError("Object export already exists; choose a new output directory")
    dates = {"update_date", "source_date", "first_seen_date", "last_seen_date"}
    total = assigned = category_rows = td_total = 0
    excluded, counts = {}, {}
    with ExitStack() as stack:
        stream = stack.enter_context((output / "poi_teradata.csv").open("x", encoding="utf-8-sig", newline=""))
        csv_writer = csv.DictWriter(stream, fieldnames=TERADATA_FIELDS, delimiter=";")
        csv_writer.writeheader()
        parquet_writer = None
        if parquet:
            import pyarrow as pa
            import pyarrow.parquet as pq
            schema = pa.schema([(key, pa.float64() if key in {"lon", "lat"} else pa.date32() if key in dates else
                                 pa.bool_() if key in BOOLEAN_FIELDS else pa.string()) for key in TERADATA_FIELDS])
            parquet_writer = stack.enter_context(pq.ParquetWriter(output / "poi_teradata.parquet", schema, compression="zstd"))
        batch = []
        for row in object_rows(db):
            total += 1
            category_rows += row["tag_count"]
            code = row["region_code"] or ""
            counts[code] = counts.get(code, 0) + 1
            assigned += bool(code)
            if row["osm_type"] not in selector.osm_types:
                excluded[row["osm_type"]] = excluded.get(row["osm_type"], 0) + 1
                continue
            selected = selector.teradata_row(selector.enrich(row))
            csv_writer.writerow({key: int(value) if key in BOOLEAN_FIELDS and value is not None else value
                                 for key, value in selected.items()})
            td_total += 1
            if parquet_writer:
                batch.append({key: date.fromisoformat(value) if key in dates and value else
                              bool(value) if key in BOOLEAN_FIELDS and value is not None else value
                              for key, value in selected.items()})
                if len(batch) >= batch_size:
                    parquet_writer.write_table(pa.Table.from_pylist(batch, schema=schema)); batch.clear()
        if batch:
            parquet_writer.write_table(pa.Table.from_pylist(batch, schema=schema))
    with (output / "poi_teradata.csv").open(encoding="utf-8-sig", newline="") as stream:
        check = csv.DictReader(stream, delimiter=";")
        if check.fieldnames != TERADATA_FIELDS or sum(1 for _ in check) != td_total:
            raise ValueError("Warehouse CSV count/schema verification failed")
    if parquet:
        with pq.ParquetFile(output / "poi_teradata.parquet") as check:
            if check.schema_arrow != schema or sum(batch.num_rows for batch in check.iter_batches(batch_size=batch_size)) != td_total:
                raise ValueError("Warehouse Parquet verification failed")
    return {"unique_objects": total, "category_rows": category_rows, "extra_category_rows": category_rows - total,
            "assigned_objects": assigned, "by_region_objects": counts, "primary_rules_sha256": selector.fingerprint,
            "teradata_objects": td_total, "teradata_osm_types": list(selector.osm_types), "teradata_excluded_by_type": excluded}
