"""One row per (osm_type, osm_id), preserving all category assignments."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import uuid
from datetime import date, datetime, timezone
from itertools import groupby
from pathlib import Path

from .storage import FIELDS, BOOLEAN_FIELDS
from .selection import PRIMARY_FIELDS, TERADATA_FIELDS, PrimarySelector

OBJECT_FIELDS = FIELDS + ["osm_uid", "tag_count", "classifications"] + PRIMARY_FIELDS
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
    cursor = db.execute(f"SELECT {selection} FROM poi WHERE record_status!='DELETED' ORDER BY osm_type,osm_id,tag")
    rows = (dict(zip(FIELDS, row)) for row in cursor)
    for _, group in groupby(rows, key=lambda r: (r["osm_type"], r["osm_id"])):
        yield collapse(group)


def export_objects(db, output: Path, *, parquet=True, batch_size=5000, region_codes=None, selection_rules=None):
    """Export bounded batches. Existing files are protected from overwrite."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from contextlib import ExitStack
    selector = PrimarySelector(selection_rules)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    targets = [output / "poi.csv", output / "poi_teradata.csv", output / "objects"] + ([output / "poi_objects.parquet", output / "poi_teradata.parquet"] if parquet else [])
    if any(p.exists() for p in targets):
        raise FileExistsError("Object export already exists; choose a new output directory")
    (output / "objects").mkdir()
    dates = {"update_date", "source_date", "first_seen_date", "last_seen_date"}
    def arrow_schema(fields):
        return pa.schema([(k, pa.float64() if k in {"lon", "lat"} else pa.date32() if k in dates else
                          pa.bool_() if k in BOOLEAN_FIELDS else pa.int64() if k == "tag_count" else pa.string()) for k in fields])
    schema, td_schema = arrow_schema(OBJECT_FIELDS), arrow_schema(TERADATA_FIELDS)
    total = assigned = category_rows = 0
    counts = {}
    with ExitStack() as stack:
        def csv_writer(path, fields=OBJECT_FIELDS):
            stream = stack.enter_context(path.open("x", encoding="utf-8-sig", newline=""))
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";")
            writer.writeheader()
            return writer
        all_writer = csv_writer(output / "poi.csv")
        td_csv = csv_writer(output / "poi_teradata.csv", TERADATA_FIELDS)
        codes = list(dict.fromkeys((region_codes or []) + [""]))
        if any(not all(c.isalnum() or c in "-_" for c in code) for code in codes):
            raise ValueError("Unsafe region_code for output filename")
        regions = {code: csv_writer(output / "objects" / f"{code or 'unassigned'}.csv") for code in codes}
        writer = stack.enter_context(pq.ParquetWriter(output / "poi_objects.parquet", schema, compression="zstd")) if parquet else None
        td_writer = stack.enter_context(pq.ParquetWriter(output / "poi_teradata.parquet", td_schema, compression="zstd")) if parquet else None
        batch, td_batch = [], []
        def flush():
            if batch and writer:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                td_writer.write_table(pa.Table.from_pylist(td_batch, schema=td_schema))
            batch.clear()
            td_batch.clear()
        def csv_values(row):
            return {k:int(v) if k in BOOLEAN_FIELDS and v is not None else v for k,v in row.items()}
        for row in object_rows(db):
            row = selector.enrich(row)
            td_row = selector.teradata_row(row)
            total += 1
            category_rows += row["tag_count"]
            code = row["region_code"] or ""
            if code not in regions:
                # Registry region codes are filenames elsewhere in the app;
                # reject separators in standalone imports too.
                if not all(c.isalnum() or c in "-_" for c in code):
                    raise ValueError("Unsafe region_code for output filename")
                regions[code] = csv_writer(output / "objects" / f"{code}.csv")
            csv_row = csv_values(row)
            all_writer.writerow(csv_row)
            regions[code].writerow(csv_row)
            td_csv.writerow(csv_values(td_row))
            counts[code] = counts.get(code, 0) + 1
            assigned += bool(code)
            if writer:
                batch.append({k: date.fromisoformat(v) if k in dates and v else v for k, v in row.items()})
                td_batch.append({k: date.fromisoformat(v) if k in dates and v else v for k,v in td_row.items()})
                if len(batch) >= batch_size:
                    flush()
        flush()
    for filename, fields in [("poi.csv",OBJECT_FIELDS), ("poi_teradata.csv",TERADATA_FIELDS)]:
        with (output / filename).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter=";")
            if reader.fieldnames != fields or sum(1 for _ in reader) != total:
                raise ValueError(f"CSV count/schema verification failed: {filename}")
    if parquet:
        for filename, expected in [("poi_objects.parquet",schema), ("poi_teradata.parquet",td_schema)]:
            with pq.ParquetFile(output / filename) as saved:
                if saved.schema_arrow != expected or sum(b.num_rows for b in saved.iter_batches(batch_size=batch_size)) != total:
                    raise ValueError(f"Parquet verification failed: {filename}")
    return {"unique_objects": total, "category_rows": category_rows, "extra_category_rows": category_rows - total,
            "assigned_objects": assigned, "by_region_objects": counts, "primary_rules_sha256": selector.fingerprint}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    cli = argparse.ArgumentParser(description="Build one-row-per-object exports from an existing SQLite registry, without reading PBF")
    cli.add_argument("input", type=Path, help="Existing preview/run directory or its .sqlite file")
    cli.add_argument("--priority-config", type=Path, help="Custom primary_selection.json; original registry stays unchanged")
    args = cli.parse_args(argv)
    path = args.input.resolve()
    if path.is_dir():
        path = path / ("preview.sqlite" if (path / "preview.sqlite").exists() else "registry.sqlite")
    output = path.parent / ("unique-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
    try:
        from contextlib import closing
        source_manifest = path.parent / "summary.json"
        if not source_manifest.exists():
            source_manifest = path.parent / "manifest.json"
        source_info = json.loads(source_manifest.read_text(encoding="utf-8")) if source_manifest.exists() else {}
        source_status = source_info.get("status", "UNKNOWN")
        if source_status not in {"UNKNOWN", "PREVIEW_SUCCESS", "PREVIEW_INCOMPLETE", "SUCCESS"}:
            raise ValueError("Source run did not complete; do not export its partial registry")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            rules = json.loads(args.priority_config.read_text(encoding="utf-8")) if args.priority_config else None
            result = export_objects(db, output, selection_rules=rules)
        report = {"output": str(output), "source_registry": str(path), "source_status": source_status,
                  "format": "one_row_per_osm_object", **result}
        from .runtime import atomic_json
        atomic_json(output / "summary.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
