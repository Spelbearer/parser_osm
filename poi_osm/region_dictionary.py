"""Output-only region labels keyed by ISO code; geographic state stays canonical."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from collections import Counter
from pathlib import Path


class RegionDictionary:
    def __init__(self, path, registry):
        self.path = Path(path)
        payload = self.path.read_bytes()
        document = json.loads(payload.decode("utf-8-sig"))
        self.column = document.get("target_column")
        if self.column not in {"EDW", "CEM", "REGION_NAME_POI"}:
            raise ValueError("Region dictionary target_column must be EDW, CEM or REGION_NAME_POI")
        records = document.get("regions")
        if not isinstance(records, list) or not records:
            raise ValueError("Region dictionary must contain region records")
        self.records = {}
        for record in records:
            code = record.get("region_code")
            label = record.get(self.column)
            if (not isinstance(code, str) or not code or code in self.records
                    or not isinstance(label, str) or not label.strip()):
                raise ValueError("Region dictionary requires unique nonempty codes and target labels")
            self.records[code] = {**record, self.column: label.strip()}
        expected = {r["region_code"] for r in registry["regions"]}
        applicable = expected & self.records.keys()
        if applicable and document.get("coverage_id") != registry.get("coverage_id"):
            raise ValueError("Region dictionary coverage differs from the geographic registry")
        if document.get("coverage_id") == registry.get("coverage_id"):
            if set(self.records) != expected:
                raise ValueError("Region dictionary does not cover the configured region registry exactly")
        self.metadata = {"source": document.get("source"), "target_column": self.column,
                         "dictionary_sha256": hashlib.sha256(payload).hexdigest(), "records": len(self.records), "applicable_regions": len(applicable),
                         "flag_used_as_filter": False}

    def label(self, code, original):
        return self.records[code][self.column] if code in self.records else original

    def registry(self, original):
        return {**original, "regions": [{**r, "region_name": self.label(r["region_code"], r["region_name"])}
                                        for r in original["regions"]]}

    def apply_csv(self, path):
        path = Path(path)
        temporary = path.with_suffix(path.suffix + ".mapping.tmp")
        counts = Counter(mapped_objects=0, renamed_objects=0, unassigned_objects=0)
        unmapped = Counter()
        try:
            with path.open(encoding="utf-8-sig", newline="") as incoming, temporary.open("x", encoding="utf-8-sig", newline="") as outgoing:
                reader = csv.DictReader(incoming, delimiter=";")
                if not {"region_code", "region_name"} <= set(reader.fieldnames or []):
                    raise ValueError("Region mapping requires the full source with ISO region_code")
                writer = csv.DictWriter(outgoing, fieldnames=reader.fieldnames, delimiter=";")
                writer.writeheader()
                for row in reader:
                    if None in row or any(v is None for v in row.values()):
                        raise ValueError("Malformed CSV row during region mapping")
                    code, original = row["region_code"], row["region_name"]
                    if code in self.records:
                        row["region_name"] = self.label(code, original)
                        counts["mapped_objects"] += 1
                        counts["renamed_objects"] += row["region_name"] != original
                    elif code:
                        unmapped[code] += 1
                    else:
                        counts["unassigned_objects"] += 1
                    writer.writerow(row)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return {**self.metadata, **counts, "unmapped_codes": dict(unmapped)}

    def write_index(self, path, registry):
        fields = ["region_code", "source_region_name", "region_name", "REGION_NAME_POI", "EDW", "CEM",
                  "MACROREGION_ID", "MACROREGION_NAME", "OPERATION_MR_FOLDER", "FLAG"]
        with Path(path).open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";")
            writer.writeheader()
            for region in registry["regions"]:
                code = region["region_code"]
                row = {key: self.records.get(code, {}).get(key) for key in fields}
                row.update(region_code=code, source_region_name=region["region_name"],
                           region_name=self.label(code, region["region_name"]))
                writer.writerow(row)
