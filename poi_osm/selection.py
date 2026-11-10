"""Select one auditable primary category per OSM object for warehouse loading."""
from __future__ import annotations

import hashlib
import json

from .classification import CONFIG

PRIMARY_FIELDS = ["primary_tag", "primary_category", "primary_subcategory", "primary_osm_key",
                  "primary_osm_value", "primary_reason", "all_tags"]
TERADATA_FIELDS = ["osm_type", "osm_id", "osm_uid", "point_name", "lon", "lat", "wkt",
                   "region_name", "region_code", "region_id", "country", "address"] + PRIMARY_FIELDS + [
                   "is_telecom", "telecom_type", "telecom_evidence", "brand", "operator", "is_chain",
                   "chain_id", "chain_name", "chain_status", "chain_evidence", "update_date", "source_date",
                   "first_seen_date", "last_seen_date", "record_status", "source_file"]


class PrimarySelector:
    def __init__(self, rules=None):
        self.rules = rules if rules is not None else json.loads((CONFIG / "primary_selection.json").read_text(encoding="utf-8"))
        if not isinstance(self.rules, dict):
            raise ValueError("Primary selection configuration must be a JSON object")
        self.osm_types = self.rules.get("teradata_osm_types", ["node", "way"])
        if not isinstance(self.osm_types, list) or not self.osm_types or any(not isinstance(t, str) or t not in {"node", "way", "relation"} for t in self.osm_types) or len(self.osm_types) != len(set(self.osm_types)):
            raise ValueError("teradata_osm_types must contain unique valid OSM types")
        self.ranks = {}
        for key in ("telecom_type_priority", "telecom_tag_priority", "other_tag_priority"):
            values = self.rules.get(key)
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values) or len(values) != len(set(values)):
                raise ValueError(f"Primary selection {key} must be a list of unique nonempty strings")
            self.ranks[key] = {value: rank for rank, value in enumerate(values)}
        self.fingerprint = hashlib.sha256(json.dumps(self.rules, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def enrich(self, row):
        assignments = json.loads(row["classifications"])
        if not assignments or any(not isinstance(r.get("tag"), str) or not r["tag"] for r in assignments):
            raise ValueError("Cannot select a primary category without source tag assignments")
        telecom = [r for r in assignments if r.get("is_telecom")]
        pool = telecom or assignments
        def rank(r):
            if telecom:
                priority = (self.ranks["telecom_type_priority"].get(r.get("telecom_type"), len(self.ranks["telecom_type_priority"])),
                            self.ranks["telecom_tag_priority"].get(r["tag"], len(self.ranks["telecom_tag_priority"])))
            else:
                priority = (self.ranks["other_tag_priority"].get(r["tag"], len(self.ranks["other_tag_priority"])),)
            return priority + tuple(r.get(k) or "" for k in ("tag", "osm_key", "osm_value", "category", "subcategory"))
        primary = min(pool, key=rank)
        if telecom:
            reason = "telecom_type_priority" if primary.get("telecom_type") in self.ranks["telecom_type_priority"] else (
                     "telecom_tag_priority" if primary["tag"] in self.ranks["telecom_tag_priority"] else "telecom_lexical_fallback")
        else:
            reason = "other_tag_priority" if primary["tag"] in self.ranks["other_tag_priority"] else "other_lexical_fallback"
        result = dict(row)
        result.update(primary_tag=primary["tag"], primary_category=primary.get("category"),
                      primary_subcategory=primary.get("subcategory"), primary_osm_key=primary.get("osm_key"),
                      primary_osm_value=primary.get("osm_value"), primary_reason=reason,
                      all_tags=json.dumps(sorted({r["tag"] for r in assignments}), ensure_ascii=False))
        return result

    def teradata_row(self, enriched):
        # The warehouse gets a single subtype/evidence too; network attribution
        # remains aggregated independently across the whole object.
        result = {key:enriched.get(key) for key in TERADATA_FIELDS}
        selected = next(r for r in json.loads(enriched["classifications"])
                        if r["tag"] == enriched["primary_tag"] and r.get("osm_key") == enriched["primary_osm_key"])
        result.update(telecom_type=selected.get("telecom_type"), telecom_evidence=selected.get("telecom_evidence"))
        return result
