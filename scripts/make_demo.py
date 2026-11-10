"""Create a real tiny PBF and deliberately synthetic region for offline smoke runs."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import osmium


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    repo = Path(__file__).resolve().parents[1]
    demo = repo / "demo"
    # Repeating this command never resets a successful registry.
    demo.mkdir(exist_ok=True)
    pbf = demo / "example.osm.pbf"
    if pbf.exists():
        print(f"Demo already exists: {demo}; keeping files unchanged")
        return 0
    header = osmium.io.Header()
    today = datetime.now(timezone.utc).date().isoformat()
    header.set("osmosis_replication_timestamp", today + "T00:00:00Z")
    with osmium.SimpleWriter(str(pbf), header=header) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1, location=(37.60, 55.70), tags={"shop": "supermarket", "name": 'Магазин; "пример"'}))
        writer.add_node(osmium.osm.mutable.Node(id=2, location=(37.61, 55.71), tags={"leisure": "fitness_centre"}))
        for ident, point in [(10, (37.62,55.72)), (11,(37.63,55.72)), (12,(37.63,55.73)), (13,(37.62,55.73))]:
            writer.add_node(osmium.osm.mutable.Node(id=ident, location=point))
        writer.add_way(osmium.osm.mutable.Way(id=1, nodes=[10,11,12,13,10], tags={"tourism": "hotel", "name:ru": "Тестовая гостиница"}))
        writer.add_way(osmium.osm.mutable.Way(id=2, nodes=[10,11,12,13,10]))
        writer.add_relation(osmium.osm.mutable.Relation(id=1, members=[("w",2,"outer")], tags={"type": "multipolygon", "shop": "mall", "name": "Тестовый ТЦ"}))
    region = {"region_code": "DEMO", "region_name": "Искусственный тестовый регион", "region_id": "DEMO"}
    write_json(demo / "regions.json", {"source": "Synthetic test fixture, not an administrative boundary", "coverage_id": "demo", "regions": [region]})
    write_json(demo / "boundaries.geojson", {"type": "FeatureCollection", "source": "Synthetic rectangle for tests only", "coverage_id": "demo", "features": [
        {"type": "Feature", "properties": {"region_code": "DEMO"}, "geometry": {"type": "Polygon", "coordinates": [[[37,55],[38,55],[38,56],[37,56],[37,55]]]}}
    ]})
    settings = json.loads((repo / "config/settings.json").read_text(encoding="utf-8"))
    settings.update(project_root=".", coverage_id="demo", coverage_confirmed=True, boundaries_reviewed=True,
                    tag_mapping="../config/tag_mapping.json", regions="regions.json", boundaries="boundaries.geojson",
                    sources=[], input={"path": "example.osm.pbf", "source_date": today,
                    "sha256": hashlib.sha256(pbf.read_bytes()).hexdigest(), "coverage_id": "demo", "complete": True})
    write_json(demo / "settings.json", settings)
    print(f"Created synthetic fixture: {demo}; 2 nodes, 1 area way and 1 multipolygon POI")
    return 0


if __name__ == "__main__":
    sys.exit(main())
