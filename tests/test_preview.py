import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import osmium
import pyarrow.parquet as pq
import pytest

from test_pipeline import project, write_json


@pytest.mark.parametrize("with_boundaries", [True, False])
def test_local_preview_does_not_advance_production_state(project, with_boundaries):
    pbf = project / "local.osm.pbf"
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", datetime.now(timezone.utc).date().isoformat() + "T00:00:00Z")
    with osmium.SimpleWriter(str(pbf), header=header) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=10, location=(37,55), tags={"shop":"bakery", "name":"Булочная"}))
        if with_boundaries:
            for i, point in enumerate([(36,54),(38,54),(38,56),(36,56)], 1):
                writer.add_node(osmium.osm.mutable.Node(id=i, location=point))
            writer.add_way(osmium.osm.mutable.Way(id=1,nodes=[1,2,3,4,1],tags={
                "boundary":"administrative", "admin_level":"4", "ISO3166-2":"RU-TST"}))
    write_json(project / "config/regions.json", {"source":"fixture", "coverage_id":"fixture",
        "regions":[{"region_code":"RU-TST","region_name":"Тест"}]})
    output = project / "preview"
    result = subprocess.run([sys.executable,"-m","poi_osm.preview", str(pbf),"--config",str(project / "config/settings.json"),
                             "--output",str(output)],cwd=Path(__file__).resolve().parents[1],capture_output=True,encoding="utf-8",timeout=30)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["status"] == "PREVIEW_SUCCESS"
    assert summary["complete_coverage_verified"] is False
    assert summary["boundaries_reviewed"] is False
    assert summary["metrics"]["total"] == 1
    assert not (project / "state").exists()
    run = Path(summary["output"])
    assert (run / "poi.csv").is_file()
    assert "PREVIEW" in (run / "poi.csv").read_text(encoding="utf-8-sig")
    assert pq.read_table(run / "poi_assigned.parquet").num_rows == int(with_boundaries)
    assert summary["metrics"]["unassigned_fraction"] == (0 if with_boundaries else 1)


def test_preview_corrupt_source_has_failed_manifest(project):
    bad = project / "bad.osm.pbf"
    bad.write_bytes(b"corrupt")
    output = project / "preview"
    result = subprocess.run([sys.executable,"-m","poi_osm.preview",str(bad),"--output",str(output)],
                            cwd=Path(__file__).resolve().parents[1],capture_output=True,encoding="utf-8",timeout=30)
    assert result.returncode != 0
    assert json.loads(next(output.glob("*/summary.json")).read_text(encoding="utf-8"))["status"] == "FAILED"
    assert not (project / "state").exists()
