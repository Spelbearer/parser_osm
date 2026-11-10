"""Deterministic, auditable business categories and configurable chain hints."""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config"
PHONE_TAGS = {"mobile_phone", "mobile_phone_repair", "telecommunication", "telecom"}
PHONE_CONTEXT = PHONE_TAGS | {"electronics", "electronics_repair", "computer", "repair"}
PHONE_WORDS = re.compile(r"(?:ремонт\w*\s+(?:мобильн\w*\s+)?(?:телефон\w*|смартфон\w*|iphone)|(?:телефон\w*|смартфон\w*|iphone)\s+ремонт\w*|салон\s+связи|ремонт\s+сотов\w*)", re.I)


def normalize(value):
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")).strip()


class Classifier:
    def __init__(self, categories: dict, chains: dict):
        if not isinstance(categories.get("tags"), dict) or not isinstance(chains.get("chains"), list):
            raise ValueError("Invalid category or chain dictionary")
        self.categories, self.chains = categories, chains
        for tag, definition in categories["tags"].items():
            if not isinstance(definition, dict) or any(not isinstance(definition.get(k), str) or not definition[k] for k in ("category", "subcategory")):
                raise ValueError(f"Category/subcategory missing for {tag}")
        self.rules = []
        seen = set()
        for chain in chains["chains"]:
            if not chain.get("id") or not chain.get("name") or chain["id"] in seen:
                raise ValueError("Chain IDs/names must be nonempty and IDs unique")
            seen.add(chain["id"])
            if not isinstance(chain.get("aliases"), list) or any(not isinstance(a, str) for a in chain["aliases"]):
                raise ValueError("Chain aliases must be a list of strings")
            aliases = [normalize(a) for a in chain["aliases"]]
            if not aliases or any(not a for a in aliases):
                raise ValueError("Chain aliases must be nonempty")
            self.rules.append((chain, [re.compile(r"(?<!\w)" + re.escape(a) + r"(?!\w)") for a in aliases]))

    def classify(self, tags: dict, tag: str) -> dict:
        definition = self.categories["tags"].get(tag, {"category": "Прочее", "subcategory": tag})
        result = dict(definition)
        text = normalize(" ".join(tags.get(k, "") for k in ("name", "name:ru", "official_name")))
        structured_repair = (tags.get("mobile_phone:repair") in {"yes", "only"}
                             or tags.get("service:mobile_phone:repair") in {"yes", "only"}
                             or tags.get("repair") in {"mobile_phone", "phone", "smartphone"}
                             or tag == "mobile_phone_repair")
        telecom_type = None
        evidence = None
        if tag in PHONE_CONTEXT:
            if structured_repair:
                telecom_type, evidence = "Ремонт телефонов", "osm_tags"
            elif PHONE_WORDS.search(text):
                telecom_type, evidence = ("Ремонт телефонов" if "ремонт" in text else "Салон связи"), "name_hint"
            elif tag in PHONE_TAGS:
                telecom_type = "Офис связи" if tags.get("office") == tag else "Салон связи и мобильные телефоны"
                evidence = "osm_tags"
        result.update(is_telecom=telecom_type is not None, telecom_type=telecom_type, telecom_evidence=evidence,
                      brand=tags.get("brand"), operator=tags.get("operator"))
        if telecom_type:
            result.update(category="Телефония и связь", subcategory=telecom_type)
        # Structured brand/operator evidence has priority over names. Multiple
        # brands at one level remain a conflict rather than an arbitrary pick.
        for keys, confidence in [(('brand',), 'brand'), (('operator',), 'operator'),
                                 (('name', 'name:ru', 'official_name'), 'name_hint')]:
            hits = {}
            for key in keys:
                value = normalize(tags.get(key))
                for chain, patterns in self.rules:
                    if confidence == "name_hint" and chain.get("name_tags") and tag not in chain["name_tags"]:
                        continue
                    if any(pattern.search(value) for pattern in patterns):
                        hits[chain["id"]] = (chain, key)
            if hits:
                if len(hits) != 1:
                    result.update(is_chain=None, chain_id=None, chain_name=None, chain_status="conflict",
                                  chain_evidence=",".join(sorted(hits)))
                else:
                    chain, key = next(iter(hits.values()))
                    result.update(is_chain=True, chain_id=chain["id"], chain_name=chain["name"],
                                  chain_status="identified", chain_evidence=f"{confidence}:{key}")
                    if chain.get("telecom_operator") and not telecom_type and tag in PHONE_CONTEXT:
                        result.update(is_telecom=True, telecom_type="Салон связи", telecom_evidence=f"chain_{confidence}",
                                      category="Телефония и связь", subcategory="Салон связи")
                break
        else:
            result.update(is_chain=None, chain_id=None, chain_name=None, chain_status="not_identified", chain_evidence=None)
        return result


@lru_cache(maxsize=1)
def default_classifier():
    return Classifier(json.loads((CONFIG / "categories.json").read_text(encoding="utf-8")),
                      json.loads((CONFIG / "chains.json").read_text(encoding="utf-8")))


def from_settings(settings):
    return Classifier(settings["category_rules"], settings["chain_rules"])
