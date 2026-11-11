"""Shared synthetic PBF and registry helpers; no production data."""
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import osmium
from shapely.geometry import Polygon, mapping

from poi_osm.classification import default_classifier
from poi_osm.geography import RegionIndex
from poi_osm.storage import FIELDS

MAPPING = {"bakery":["shop"], "hotel":["tourism"]}

def write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def configure(root, **updates):
    path = root / "config/settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings.update(updates)
    write_json(path, settings)


def csv_rows(path):
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream, delimiter=";"))


def part(path,code,west,east,extra_id,*,shared_tag='bakery',include_extra=True,timestamp=None,shared_tags=None):
    path.unlink(missing_ok=True)
    header=osmium.io.Header()
    header.set('osmosis_replication_timestamp',timestamp or datetime.now(timezone.utc).date().isoformat()+'T00:00:00Z')
    with osmium.SimpleWriter(str(path),header=header) as writer:
        writer.add_node(osmium.osm.mutable.Node(id=1,location=(1,1),tags=shared_tags or {('tourism' if shared_tag=='hotel' else 'shop'):shared_tag,'name':'Общий'}))
        if include_extra:
            writer.add_node(osmium.osm.mutable.Node(id=extra_id,location=(west+2,2),tags={'shop':'bakery','name':code}))
        for n,xy in enumerate([(west,0),(east,0),(east,10),(west,10)],100):
            writer.add_node(osmium.osm.mutable.Node(id=n,location=xy))
        writer.add_way(osmium.osm.mutable.Way(id=100,nodes=[100,101,102,103,100],tags={
            'boundary':'administrative','admin_level':'4','ISO3166-2':code}))


def run_collection(root,*args,ok=True):
    result=invoke_collection(root,*args)
    assert (result.returncode==0)==ok,result.stdout+result.stderr
    return json.loads(result.stdout) if ok else result


def invoke_collection(root,*args):
    return subprocess.run([sys.executable,'-m','poi_osm.collection',str(root/'inputs'),'--config',str(root/'config/settings.json'),
                           '--profile',str(root/'profile.json'),'--output',str(root/'results'),*args],
                          cwd=Path(__file__).resolve().parents[1],capture_output=True,encoding='utf-8',timeout=90)


def supplement(root, sources, *, code='RU-BB'):
    path=root/'supplement.geojson'
    write_json(path, {'type':'FeatureCollection','source':'synthetic recovery','review_required':True,
                     'source_sha256':{s['extract']:s['sha256'] for s in sources},
                     'features':[{'type':'Feature','properties':{'region_code':code},
                                  'geometry':{'type':'Polygon','coordinates':[[[10,0],[20,0],[20,10],[10,10],[10,0]]]}}]})
    return path


def row(object_id="1", tag="bakery", name="Булочная; №1", code="RU-TST", osm_type="node"):
    return {"osm_id": object_id, "osm_type": osm_type, "tag": tag, "point_name": name,
            "lon": 1.0, "lat": 2.0, "wkt": "POINT (1 2)", "region_name": "Тест",
            "region_code": code, "region_id": "test", "country": "Россия", "address": "{}",
            "osm_key": MAPPING[tag][0], "osm_value": tag}


def full_row(tag='bakery', **updates):
    value = {k:None for k in FIELDS}
    value.update(row(tag=tag))
    value.update(default_classifier().classify({'name':value['point_name']},tag))
    value.update(update_date='2026-11-11',first_seen_date='2026-11-11',last_seen_date='2026-11-11',record_status='NEW')
    value.update(updates)
    return value


def boundaries(tmp_path):
    registry = tmp_path / "regions.json"
    registry.write_text(json.dumps({"source": "test fixture", "coverage_id": "test",
                                   "regions": [{"region_code": "RU-TST", "region_name": "Тест"}]}))
    geojson = tmp_path / "regions.geojson"
    geojson.write_text(json.dumps({"type": "FeatureCollection", "source": "test fixture",
                                  "coverage_id": "test", "features": [{"type": "Feature",
                                  "properties": {"region_code": "RU-TST"},
                                  "geometry": mapping(Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]))}]}))
    return RegionIndex(geojson, registry), geojson, registry


def confirm_collection(root):
    configure(root,boundaries_reviewed=True,coverage_confirmed=True)
    profile=json.loads((root/"profile.json").read_text())
    write_json(root/"profile.json",{**profile,"complete_snapshot_confirmed":True})
