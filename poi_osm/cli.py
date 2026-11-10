from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import contract_hash, load_settings
from .runtime import atomic_json, process_lock, cleanup_index
from .storage import Registry, check_quality
from .classification import from_settings


def parser():
    cli = argparse.ArgumentParser(description="Streaming OSM POI export with atomic snapshot comparison")
    cli.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config/settings.json")
    cli.add_argument("--mode", choices=["full", "incremental"], default="full")
    cli.add_argument("--region", help="Exact region name, ISO code or registry region_id")
    cli.add_argument("--check", action="store_true", help="Read-only configuration, source and state checks")
    cli.add_argument("--force-download", action="store_true")
    cli.add_argument("--prepare-boundaries", type=Path, metavar="GEOJSON", help="Extract candidate admin4 boundaries for review; do not publish POI")
    cli.add_argument("--duplicates", action="store_true", help="Report candidate geographic duplicates; never delete automatically")
    return cli


def _select_region(regions, query):
    if not query:
        return None
    matches = [r for r in regions if query in {r["region_code"], r["region_name"], r.get("region_id")}]
    if len(matches) != 1:
        raise ValueError(f"Region is missing or ambiguous: {query}")
    return matches[0]["region_code"]


def _read_state(path):
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not Path(value["registry"]).is_file():
        raise ValueError("Previous successful registry is missing; restore it before updating")
    return value


def run(args):
    from .extract import extract_pois
    from .geography import RegionIndex, prepare_boundaries
    from .sources import check_sources, obtain_source

    settings = load_settings(args.config)
    root = Path(settings["root"])
    registry_config = json.loads(Path(settings["regions"]).read_text(encoding="utf-8"))
    if registry_config["coverage_id"] != settings["coverage_id"]:
        raise ValueError("Region registry and settings coverage_id differ")
    code = _select_region(registry_config["regions"], args.region)
    scope = root / "state" / "scopes" / code if code else root / "state"
    state_path = scope / "last_success.json"
    if args.check:
        errors = []
        try:
            RegionIndex(Path(settings["boundaries"]), Path(settings["regions"]))
        except Exception as e:
            errors.append(f"boundaries: {e}")
        if not settings.get("coverage_confirmed") or not settings.get("boundaries_reviewed"):
            errors.append("Geographic coverage/boundary review has not been confirmed")
        sources = check_sources(settings)
        if not any(s.get("ok") for s in sources):
            errors.append("No source passed availability checks")
        configured_sources = [settings["input"]] if settings.get("input") else settings.get("sources", [])
        if not any(s.get("complete") is True and s.get("coverage_id") == settings["coverage_id"] for s in configured_sources):
            errors.append("No complete source declares the configured coverage_id")
        state = _read_state(state_path)
        for directory in [root, root / "source", root / "work", root / "output", scope, root / "logs"]:
            parent = directory
            while not parent.exists():
                parent = parent.parent
            if not os.access(parent, os.W_OK):
                errors.append(f"Not writable: {directory}")
        print(json.dumps({"sources": sources, "last_success": state, "errors": errors}, ensure_ascii=False, indent=2))
        return 1 if errors else 0

    root.mkdir(parents=True, exist_ok=True)
    with process_lock(root / "state" / "process.lock"):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        (root / "logs").mkdir(exist_ok=True)
        handlers = [logging.FileHandler(root / "logs" / (run_id + ".log"), encoding="utf-8")]
        if sys.stderr:
            handlers.append(logging.StreamHandler())
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", handlers=handlers, force=True)
        started = time.monotonic()
        logging.info("START mode=%s region=%s", args.mode, code or "ALL")
        for stale_index in (root / "work" / "temporary" / "indexes").glob("*.idx"):
            cleanup_index(stale_index)
        if args.mode == "incremental":
            logging.info("INCREMENTAL uses complete-snapshot comparison; replication diffs are not applied")
        stage = root / "work" / "temporary" / run_id
        stage.mkdir(parents=True)
        atomic_json(stage / "progress.json", {"stage": "download", "run_id": run_id})
        store = None
        old_db = None
        try:
            if not args.prepare_boundaries:
                if not settings.get("coverage_confirmed") or not settings.get("boundaries_reviewed"):
                    raise ValueError("Review geographic scope and boundaries, then set coverage_confirmed/boundaries_reviewed")
                boundary_path = Path(settings["boundaries"])
                region_index = RegionIndex(boundary_path, Path(settings["regions"]))
                contract = contract_hash(settings, registry_config, hashlib.sha256(boundary_path.read_bytes()).hexdigest())
                previous = _read_state(state_path)
                if previous and previous["contract_hash"] != contract:
                    raise ValueError("Filters, names or boundaries changed: use a separate project_root for a new baseline")
            source = obtain_source(settings, root / "source", force=args.force_download)
            logging.info("SOURCE %s date=%s bytes=%s", source.get("source_url"), source["source_date"], source["size_bytes"])
            if args.prepare_boundaries:
                args.prepare_boundaries.parent.mkdir(parents=True, exist_ok=True)
                prepare_boundaries(Path(source["path"]), args.prepare_boundaries, stage)
                logging.info("Candidate boundaries saved to %s; independent review required", args.prepare_boundaries)
                return 0
            if source.get("coverage_id") != settings["coverage_id"] or not source.get("complete"):
                raise ValueError("Source must be an explicitly declared complete snapshot of the agreed coverage")
            if previous and source["source_date"] < previous["source_date"]:
                raise ValueError("Snapshot is older than the previous successful version")
            atomic_json(stage / "progress.json", {"stage": "parse", "run_id": run_id})
            date = datetime.now(timezone.utc).date().isoformat()
            store = Registry(stage / "registry.sqlite", settings["mapping"], date, source, settings["batch_size"])
            if code and previous:
                old_db = sqlite3.connect(Path(previous["registry"]).resolve().as_uri() + "?mode=ro", uri=True)
            unresolved_previous = 0
            review_columns = ["osm_type", "osm_id", "tag", "lon", "lat", "reason", "detail"]
            with (stage / "review.csv").open("w", encoding="utf-8-sig", newline="") as stream:
                review_writer = csv.DictWriter(stream, fieldnames=review_columns, delimiter=";", extrasaction="ignore")
                review_writer.writeheader()
                def review(row):
                    review_writer.writerow({**row, "reason": row.get("review_reason", row.get("reason", "unknown_region"))})
                def emit(row):
                    nonlocal unresolved_previous
                    if old_db and not row.get("region_code") and old_db.execute(
                        "SELECT 1 FROM poi WHERE osm_type=? AND osm_id=? AND tag=? AND record_status!='DELETED'",
                        (row["osm_type"], row["osm_id"], row["tag"])).fetchone():
                        unresolved_previous += 1
                    if not code or row.get("region_code") == code:
                        store.insert(row)
                parsing = extract_pois(Path(source["path"]), settings["mapping"], settings["name_priority"],
                                       region_index, emit, review, stage, settings.get("line_policy", "midpoint"), classifier=from_settings(settings))
            if old_db:
                old_db.close()
                old_db = None
            if unresolved_previous:
                raise ValueError("Previously assigned regional POI now have unresolved regions; deletion is unsafe; see review.csv")
            logging.info("PARSING %s", json.dumps(parsing, ensure_ascii=False))
            atomic_json(stage / "progress.json", {"stage": "compare", "run_id": run_id, "parsing": parsing})
            store.compare(Path(previous["registry"]) if previous else None)
            metrics = store.metrics()
            atomic_json(stage / "quality.json", metrics)
            check_quality(metrics, previous["metrics"] if previous else None, settings["quality"])
            store.export(stage, [code] if code else [r["region_code"] for r in registry_config["regions"]], settings.get("write_parquet", True))
            if args.duplicates:
                from .duplicates import report_duplicates
                report_duplicates(store.db, stage / "duplicate_candidates.csv", settings.get("duplicate_distance_m", 30))
            store.close()
            store = None
            destination = root / "output" / "runs" / run_id
            destination.parent.mkdir(parents=True, exist_ok=True)
            manifest = {"run_id": run_id, "mode": args.mode, "scope": code or "ALL", "status": "SUCCESS",
                        "contract_hash": contract, "source_date": source["source_date"],
                        "source_sha256": source["sha256"], "source_url": source.get("source_url"),
                        "source_bytes": source["size_bytes"], "metrics": metrics, "parsing": parsing,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "output": str(destination), "registry": str(destination / "registry.sqlite")}
            atomic_json(stage / "manifest.json", manifest)
            atomic_json(stage / "progress.json", {"stage": "complete", "run_id": run_id})
            os.replace(stage, destination)
            scope.mkdir(parents=True, exist_ok=True)
            atomic_json(state_path, manifest)
            logging.info("SUCCESS %s output=%s elapsed=%.2fs", json.dumps(metrics, ensure_ascii=False), destination, time.monotonic() - started)
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 0
        except Exception:
            if old_db:
                old_db.close()
            if store:
                store.close()
            logging.exception("FAILED; previous successful publication unchanged; work retained at %s", stage)
            raise


def main(argv=None):
    # Pipes on Windows otherwise use a local code page, while callers decode
    # JSON and diagnostics as UTF-8. pythonw legitimately has no streams.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parser().parse_args(argv)
    try:
        return run(args)
    except Exception as error:
        if sys.stderr:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
