"""Read-only, bounded-memory checks of warehouse CSV and its regional copies."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import sqlite3
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from shapely import from_wkt

from .runtime import configure_utf8

LOG = logging.getLogger(__name__)


def audit(run, output):
    run, output = Path(run).resolve(), Path(output)
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8-sig"))
    output.mkdir(parents=True, exist_ok=True)
    errors, samples = Counter(), []
    counts = Counter()
    region_counts = Counter()

    def fail(reason, **detail):
        errors[reason] += 1
        if len(samples) < 50:
            samples.append({"reason": reason, **detail})

    def digest(row, fields):
        return hashlib.sha256(json.dumps([row.get(k) for k in fields], ensure_ascii=False,
                                        separators=(",", ":")).encode()).hexdigest()

    # Temporary uniqueness index lives on the requested output disk, never in RAM.
    with tempfile.TemporaryDirectory(prefix="audit-", dir=output) as temporary:
        db = sqlite3.connect(str(Path(temporary) / "keys.sqlite"))
        try:
            db.execute("CREATE TABLE objects(kind TEXT,id TEXT,region TEXT,digest TEXT,parts INTEGER DEFAULT 0,PRIMARY KEY(kind,id)) WITHOUT ROWID")
            with (run / "poi_teradata.csv").open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, delimiter=";")
                fields = reader.fieldnames or []
                required = {"osm_type", "osm_id", "lon", "lat", "wkt", "region_code", "primary_tag", "all_tags", "is_telecom"}
                if not required.issubset(fields) or len(fields) != len(set(fields)):
                    raise ValueError("Missing or repeated columns in poi_teradata.csv")
                allowed_types = summary.get("object_export", {}).get("teradata_osm_types", ["node", "way"])
                for number, row in enumerate(reader, 2):
                    counts["rows"] += 1
                    if counts["rows"] % 100000 == 0:
                        LOG.info("Main CSV: %s rows checked", counts["rows"])
                    kind, ident = row.get("osm_type"), row.get("osm_id")
                    key = (kind, ident)
                    if None in row or any(value is None for value in row.values()):
                        fail("malformed_csv_row", line=number)
                        continue
                    if kind not in allowed_types or not ident.isascii() or not ident.isdigit() or int(ident) <= 0:
                        fail("invalid_osm_key", line=number, key=key)
                    counts["type:" + kind] += 1
                    region_counts[row["region_code"]] += 1
                    counts["unassigned"] += not bool(row["region_code"])
                    counts["telecom_objects"] += row["is_telecom"] == "1"
                    if row["is_telecom"] not in {"0", "1"}:
                        fail("invalid_telecom_flag", line=number)
                    try:
                        lon, lat = float(row["lon"]), float(row["lat"])
                        geometry = from_wkt(row["wkt"])
                        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90
                                and geometry.geom_type == "Point" and not geometry.is_empty
                                and abs(geometry.x - lon) <= 1e-6 and abs(geometry.y - lat) <= 1e-6):
                            raise ValueError("Coordinates disagree")
                    except Exception:
                        fail("invalid_coordinates_or_wkt", line=number, key=key)
                    try:
                        tags = json.loads(row["all_tags"])
                        if not isinstance(tags, list) or not tags or any(not isinstance(t, str) or not t for t in tags) or len(tags) != len(set(tags)) or row["primary_tag"] not in tags:
                            raise ValueError("Invalid primary tag")
                    except (TypeError, ValueError):
                        fail("invalid_tag_selection", line=number, key=key)
                    try:
                        db.execute("INSERT INTO objects(kind,id,region,digest) VALUES(?,?,?,?)",
                                   (kind, ident, row["region_code"], digest(row, fields)))
                    except sqlite3.IntegrityError:
                        fail("duplicate_osm_key", line=number, key=key)
                db.commit()
            expected = summary.get("object_export", {}).get("teradata_objects")
            if counts["rows"] != expected:
                fail("summary_row_count", expected=expected, actual=counts["rows"])
            with (run / "regions_index.csv").open(encoding="utf-8-sig", newline="") as stream:
                index = list(csv.DictReader(stream, delimiter=";"))
            expected_files = summary.get("regional_export", {}).get("files", [])
            def normalize(records):
                return sorted((r["region_code"], r["region_name"], PureWindowsPath(r["file"]).as_posix(), int(r["rows"])) for r in records)
            if normalize(index) != normalize(expected_files):
                fail("summary_region_index_disagreement")
            seen_paths = set()
            limit = summary.get("regional_export", {}).get("max_rows_per_file", 50000)
            for entry in index:
                relative = PureWindowsPath(entry["file"])
                if relative.is_absolute() or relative.drive or ".." in relative.parts:
                    fail("invalid_region_path", file=entry["file"])
                    continue
                path = run.joinpath(*relative.parts).resolve()
                if not path.is_relative_to(run) or path in seen_paths:
                    fail("duplicate_or_external_region_path", file=entry["file"])
                    continue
                seen_paths.add(path)
                counts["region_files"] += 1
                if counts["region_files"] % 10 == 0:
                    LOG.info("Regional CSV: file %s/%s", counts["region_files"], len(index))
                if not path.is_file():
                    fail("missing_region_file", file=entry["file"])
                    continue
                rows = 0
                with path.open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream, delimiter=";")
                    if reader.fieldnames != fields:
                        fail("region_header_disagreement", file=entry["file"])
                        continue
                    for row in reader:
                        rows += 1
                        counts["regional_rows"] += 1
                        key = (row.get("osm_type"), row.get("osm_id"))
                        record = db.execute("SELECT region,digest FROM objects WHERE kind=? AND id=?", key).fetchone()
                        if record is None:
                            fail("region_key_absent_from_main", file=entry["file"], key=key)
                            continue
                        db.execute("UPDATE objects SET parts=parts+1 WHERE kind=? AND id=?", key)
                        if row.get("region_code") != entry["region_code"] or record[0] != entry["region_code"]:
                            fail("region_code_disagreement", file=entry["file"], key=key)
                        if digest(row, fields) != record[1] or None in row:
                            fail("region_row_differs_from_main", file=entry["file"], key=key)
                if rows != int(entry["rows"]) or rows > limit:
                    fail("region_row_count", file=entry["file"], expected=entry["rows"], actual=rows)
                counts["empty_region_files"] += rows == 0
            db.commit()
            regional = summary.get("regional_export", {})
            if counts["regional_rows"] != regional.get("total_rows"):
                fail("summary_regional_row_count", expected=regional.get("total_rows"), actual=counts["regional_rows"])
            if dict(region_counts) != regional.get("by_region"):
                fail("summary_region_totals_disagreement")
            for reason, predicate in (("objects_missing_from_parts", "parts=0"), ("objects_repeated_in_parts", "parts>1")):
                value = db.execute("SELECT COUNT(*) FROM objects WHERE " + predicate).fetchone()[0]
                if value:
                    errors[reason] = value
            counts["unique_keys"] = db.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        finally:
            db.close()
    boundary = summary.get("boundary_report", {})
    report = {
        "run": str(run), "source_status": summary.get("status"),
        "file_checks_ok": not errors,
        "production_ready": bool(not errors and summary.get("production") and summary.get("status") == "SUCCESS"
                                 and summary.get("boundaries_reviewed") and summary.get("complete_coverage_verified")
                                 and not boundary.get("missing_region_codes") and not summary.get("unresolved_objects")),
        "counts": dict(counts), "by_region": dict(region_counts), "errors": dict(errors), "error_samples": samples,
        "geography": {"missing_region_codes": boundary.get("missing_region_codes", []),
                      "conflicting_region_codes": boundary.get("conflicting_region_codes", []),
                      "invalid_boundary_codes": boundary.get("invalid_boundary_codes", {}),
                      "unresolved_objects": summary.get("unresolved_objects")},
        "limitations": ["Checks CSV invariants and regional copies; does not verify real-world POI accuracy or primary-category priority against the source registry.",
                        "Passing file checks does not approve candidate boundaries or incomplete coverage."],
    }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = output / f"audit-{stamp}-{uuid.uuid4().hex[:8]}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target, report


def main(argv=None):
    configure_utf8()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    path, report = audit(args.run, args.output_dir)
    print(json.dumps({"report": str(path), "file_checks_ok": report["file_checks_ok"], "counts": report["counts"]}, ensure_ascii=False))
    return 0 if report["file_checks_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
