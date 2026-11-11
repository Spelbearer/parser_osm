from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .classification import CONFIG, Classifier
from .selection import PrimarySelector


def load_settings(path: Path) -> dict:
    path = path.resolve()
    settings = json.loads(path.read_text(encoding="utf-8"))
    # Paths in settings always resolve from the settings file, never the shell CWD.
    root = (path.parent / settings.get("project_root", "..")).resolve()
    settings["root"] = str(root)
    for key, default in {"tag_mapping": "config/tag_mapping.json", "regions": "config/regions.json",
                         "boundaries": "boundaries/regions.geojson"}.items():
        settings[key] = str((root / settings.get(key, default)).resolve())
    if settings.get("input"):
        settings["input"]["path"] = str((root / settings["input"]["path"]).resolve())
    settings["mapping"] = json.loads(Path(settings["tag_mapping"]).read_text(encoding="utf-8"))
    for name, filename, target in [("categories", "categories.json", "category_rules"), ("chains", "chains.json", "chain_rules"), ("telecom", "telecom.json", "telecom_rules"), ("primary_selection", "primary_selection.json", "primary_rules")]:
        rule_path = (root / settings[name]).resolve() if settings.get(name) else CONFIG / filename
        settings[target] = json.loads(rule_path.read_text(encoding="utf-8"))
    Classifier(settings["category_rules"], settings["chain_rules"], settings["telecom_rules"])
    PrimarySelector(settings["primary_rules"])
    mapping = settings["mapping"]
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("tag_mapping must be a nonempty category -> keys object")
    for category, keys in mapping.items():
        if not category or not isinstance(keys, list) or any(not isinstance(k, str) or not k for k in keys):
            raise ValueError(f"Invalid mapping for {category}")
    priority = settings.setdefault("name_priority", ["name", "name:ru", "official_name", "brand", "operator"])
    if not priority or any(not isinstance(k, str) for k in priority):
        raise ValueError("name_priority must contain tag keys")
    if settings.get("line_policy", "midpoint") not in {"midpoint", "reject"}:
        raise ValueError("line_policy must be midpoint or reject")
    quality = settings.setdefault("quality", {})
    for k, default in {"max_count_drop_fraction": 0.25, "max_region_drop_fraction": 0.25,
                       "max_unassigned_fraction": 0.01}.items():
        value = quality.setdefault(k, default)
        if not isinstance(value, (int, float)) or not 0 <= value < 1:
            raise ValueError(f"Invalid quality threshold: {k}")
    settings.setdefault("batch_size", 5000)
    if not isinstance(settings["batch_size"], int) or settings["batch_size"] <= 0:
        raise ValueError("batch_size must be positive")
    return settings


def contract_hash(settings: dict, registry: dict, boundary_hash: str) -> str:
    # Refuse comparison across changed filters/borders: such changes are migrations,
    # not evidence of objects disappearing from OSM.
    contract = {"mapping": settings["mapping"], "regions": registry,
                "boundaries": boundary_hash, "name_priority": settings["name_priority"],
                "line_policy": settings.get("line_policy", "midpoint"),
                "coverage_id": settings["coverage_id"]}
    contract.update(schema_version=2, categories=settings.get("category_rules"), chains=settings.get("chain_rules"), telecom=settings.get("telecom_rules"))
    return hashlib.sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
