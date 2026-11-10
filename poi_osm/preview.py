"""Inspect a local PBF without claiming complete coverage or updating state."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import load_settings
from .extract import extract_pois
from .geography import RegionIndex, prepare_boundaries
from .runtime import atomic_json
from .sources import obtain_source
from .storage import Registry, FIELDS
from .classification import from_settings


class UnassignedIndex:
    last_problem = "no_usable_preview_boundary"

    def lookup(self, lon, lat):
        return None


def run(args):
    settings = load_settings(args.config)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    output = args.output.resolve() / run_id
    output.mkdir(parents=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(output / "preview.log", encoding="utf-8")], force=True)
    manifest = {"status": "RUNNING", "preview_only": True, "boundaries_reviewed": False,
                "complete_coverage_verified": False, "output": str(output)}
    atomic_json(output / "summary.json", manifest)
    store = None
    try:
        local = {"path": str(args.pbf.resolve()), "coverage_id": "local-preview-unreviewed", "complete": False}
        if args.source_date:
            local["source_date"] = args.source_date
        if args.sha256:
            local["sha256"] = args.sha256
        logging.info("Reading local PBF metadata and calculating SHA-256: %s", local["path"])
        source = obtain_source({"input": local}, output)
        manifest.update(source_date=source["source_date"], source_sha256=source["sha256"],
                        source_bytes=source["size_bytes"], source_file=str(source["path"]))
        logging.info("Extracting candidate administrative boundaries; these are unreviewed OSM geometries")
        index = UnassignedIndex()
        regions = []
        try:
            prepare_boundaries(source["path"], output / "candidate.geojson", output / "work")
            candidate = json.loads((output / "candidate.geojson").read_text(encoding="utf-8"))
            known = {r["region_code"]: r for r in json.loads(Path(settings["regions"]).read_text(encoding="utf-8"))["regions"]}
            excluded = [f["properties"]["region_code"] for f in candidate["features"] if f["properties"]["region_code"] not in known]
            candidate["features"] = [f for f in candidate["features"] if f["properties"]["region_code"] in known]
            regions = [known[f["properties"]["region_code"]] for f in candidate["features"]]
            candidate["coverage_id"] = local["coverage_id"]
            # Only preview-local copies get this identifier. No approved
            # configuration or production boundaries are changed.
            atomic_json(output / "preview_boundaries.geojson", candidate)
            atomic_json(output / "preview_regions.json", {"source": "Unreviewed OSM polygons from this local PBF",
                        "coverage_id": local["coverage_id"], "regions": regions})
            index = RegionIndex(output / "preview_boundaries.geojson", output / "preview_regions.json")
            manifest["excluded_boundary_codes"] = excluded
        except (ValueError, RuntimeError) as error:
            logging.warning("Candidate boundaries unavailable: %s. POIs will be exported without region assignment.", error)
            manifest["boundary_error"] = str(error)
            regions = []
        manifest["candidate_region_codes"] = [r["region_code"] for r in regions]
        logging.info("Parsing node/way/relation POIs; candidate regions: %s", manifest["candidate_region_codes"])
        store = Registry(output / "preview.sqlite", settings["mapping"], datetime.now(timezone.utc).date().isoformat(),
                         source, settings["batch_size"])
        with (output / "review.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["osm_type", "osm_id", "tag", "lon", "lat", "review_reason", "detail"],
                                    delimiter=";", extrasaction="ignore")
            writer.writeheader()
            parsing = extract_pois(source["path"], settings["mapping"], settings["name_priority"], index,
                                   store.insert, writer.writerow, output / "work", settings.get("line_policy", "midpoint"), strict_geometry=False, classifier=from_settings(settings))
        # Preview has no previous baseline and therefore no change statuses.
        store.db.execute("UPDATE poi SET record_status='PREVIEW'")
        store.db.commit()
        metrics = store.metrics()
        metrics.pop("changes", None)
        categories = dict(store.db.execute("SELECT tag,COUNT(*) FROM poi GROUP BY tag ORDER BY tag"))
        store._csv(output / "poi_by_tag.csv", "SELECT " + ",".join(FIELDS) + " FROM poi ORDER BY osm_type,osm_id,tag", ())
        store.export(output, [r["region_code"] for r in regions], parquet=True)
        (output / "all_russia.parquet").rename(output / "poi_assigned.parquet")
        store.close()
        store = None
        # No production quality gate: this report is explicitly incomplete,
        # unreviewed, and cannot declare objects deleted or advance state.
        status = "PREVIEW_INCOMPLETE" if parsing.get("unresolved_objects") else "PREVIEW_SUCCESS"
        manifest.update(status=status, metrics=metrics, by_category=categories, parsing=parsing,
                        warning="Unreviewed boundaries and unverified completeness. Not a production publication.")
        atomic_json(output / "summary.json", manifest)
        logging.info("%s: %s unique objects; %s category rows; unresolved=%s; output=%s", status, metrics["unique_objects"], metrics["total"], parsing.get("unresolved_objects", 0), output)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        if store:
            store.close()
        manifest.update(status="FAILED", error=str(error))
        atomic_json(output / "summary.json", manifest)
        logging.exception("Preview failed; partial files retained at %s", output)
        return 1


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    cli = argparse.ArgumentParser(description="Local PBF preview; no production state, deletions or downloads")
    cli.add_argument("pbf", type=Path)
    cli.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config/settings.json")
    cli.add_argument("--output", type=Path, default=Path("preview"))
    cli.add_argument("--source-date", help="Actual snapshot date YYYY-MM-DD, only if absent from PBF header")
    cli.add_argument("--sha256", help="Optional trusted SHA-256 to verify local input")
    return run(cli.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
