"""Deterministic, auditable business categories and configurable chain hints."""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config"
PHONE_WORDS = re.compile(r"(?:ремонт\w*\s+(?:мобильн\w*\s+)?(?:телефон\w*|смартфон\w*|iphone)|(?:телефон\w*|смартфон\w*|iphone)\s+ремонт\w*|салон\s+связи|ремонт\s+сотов\w*)", re.I)


def normalize(value):
    return re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")).strip()


class Classifier:
    def __init__(self, categories: dict, chains: dict, telecom: dict | None = None):
        if not isinstance(categories.get("tags"), dict) or not isinstance(chains.get("chains"), list):
            raise ValueError("Invalid category or chain dictionary")
        self.categories, self.chains = categories, chains
        self.telecom = telecom if telecom is not None else json.loads((CONFIG / "telecom.json").read_text(encoding="utf-8"))
        if not isinstance(self.telecom.get("category"), str) or not self.telecom["category"]:
            raise ValueError("Telecom category must be a nonempty string")
        for group in ("direct_tags", "indirect_tags"):
            values = self.telecom.get(group)
            if not isinstance(values, dict) or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in values.items()):
                raise ValueError(f"Telecom {group} must map tag values to nonempty labels")
        if self.telecom["direct_tags"].keys() & self.telecom["indirect_tags"].keys():
            raise ValueError("Telecom direct and indirect tags must not overlap")
        self.phone_context = self.telecom["direct_tags"].keys() | self.telecom["indirect_tags"].keys()
        self.phone_labels = {"phone_repair":"Ремонт телефонов", "salon":"Салон связи", "office":"Офис связи"}
        labels = self.telecom.get("labels", {})
        if not isinstance(labels, dict) or any(not isinstance(v, str) or not v for v in labels.values()):
            raise ValueError("Telecom labels must be nonempty strings")
        self.phone_labels.update(labels)
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
        if tag in self.phone_context:
            if structured_repair:
                telecom_type = self.telecom["direct_tags"].get(tag, self.phone_labels["phone_repair"]) if tag == "mobile_phone_repair" else self.phone_labels["phone_repair"]
                evidence = "osm_tags"
            elif PHONE_WORDS.search(text):
                telecom_type, evidence = self.phone_labels["phone_repair" if "ремонт" in text else "salon"], "name_hint"
            elif tag in self.telecom["direct_tags"]:
                telecom_type = self.phone_labels["office"] if tags.get("office") == tag else self.telecom["direct_tags"][tag]
                evidence = "osm_tags"
            else:
                telecom_type, evidence = self.telecom["indirect_tags"][tag], "configured_indirect"
        result.update(is_telecom=telecom_type is not None, telecom_type=telecom_type, telecom_evidence=evidence,
                      brand=tags.get("brand"), operator=tags.get("operator"))
        if telecom_type:
            result.update(category=self.telecom["category"], subcategory=telecom_type)
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
                    if chain.get("telecom_operator") and (not telecom_type or evidence == "configured_indirect") and tag in self.phone_context:
                        result.update(is_telecom=True, telecom_type=self.phone_labels["salon"], telecom_evidence=f"chain_{confidence}",
                                      category=self.telecom["category"], subcategory=self.phone_labels["salon"])
                break
        else:
            result.update(is_chain=None, chain_id=None, chain_name=None, chain_status="not_identified", chain_evidence=None)
        return result


@lru_cache(maxsize=1)
def default_classifier():
    return Classifier(json.loads((CONFIG / "categories.json").read_text(encoding="utf-8")),
                      json.loads((CONFIG / "chains.json").read_text(encoding="utf-8")),
                      json.loads((CONFIG / "telecom.json").read_text(encoding="utf-8")))


def from_settings(settings):
    return Classifier(settings["category_rules"], settings["chain_rules"], settings["telecom_rules"])
