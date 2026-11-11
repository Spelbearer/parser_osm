"""Inspect administrative relation tags without building indexes or parsing POIs."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import uuid

import osmium

LOG = logging.getLogger(__name__)
EXTRACTS = {"RU-KIR": "volga-fed-district", "RU-KYA": "siberian-fed-district", "RU-TYU": "ural-fed-district"}
NAME_HINTS = ("кировск", "красноярск", "тюмен", "kirov", "krasnoyarsk", "tyumen")


class Relations(osmium.SimpleHandler):
    def __init__(self, missing):
        super().__init__()
        self.missing = set(missing)
        self.records = []
        self.processed = 0

    def relation(self, relation):
        self.processed += 1
        tags = dict(relation.tags)
        names = " ".join(value for key, value in tags.items() if key == "name" or key.startswith("name:")).casefold()
        code = tags.get("ISO3166-2", "")
        if (tags.get("admin_level") == "4" or code in self.missing
                or any(hint in names for hint in NAME_HINTS)):
            self.records.append({"relation_id": relation.id, "tags": tags,
                                 "members_by_type": dict(Counter(m.type for m in relation.members)),
                                 "member_count": len(relation.members)})


def diagnose(run, output):
    summary = json.loads((Path(run) / "summary.json").read_text(encoding="utf-8-sig"))
    missing = summary.get("boundary_report", {}).get("missing_region_codes", [])
    selected = {EXTRACTS[code] for code in missing if code in EXTRACTS}
    scan_all = any(code not in EXTRACTS for code in missing)
    result = {"run": str(run), "missing_region_codes": missing, "sources": [],
              "limitations": "Relation metadata only: no polygon assembly, way-only boundaries or coverage approval. Missing tags and incomplete relation geometry require separate investigation."}
    for source in summary["sources"]:
        if not missing or (not scan_all and source["extract"] not in selected):
            continue
        path = Path(source["path"])
        LOG.info("Checking source SHA-256: %s", path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != source["sha256"]:
            raise ValueError(f"Source changed since collection: {path}")
        LOG.info("Reading administrative relation tags: %s", path)
        handler = Relations(missing)
        with osmium.io.Reader(str(path), osmium.osm.osm_entity_bits.RELATION) as reader:
            osmium.apply(reader, handler)
        result["sources"].append({"path": str(path), "sha256": digest.hexdigest(),
                                  "relations_processed": handler.processed, "relations": handler.records})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = output / f"boundary-diagnostics-{stamp}-{uuid.uuid4().hex[:8]}.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return target, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    path, result = diagnose(args.run, args.output_dir)
    print(json.dumps({"report": str(path), "sources_scanned": len(result["sources"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
