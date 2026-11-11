"""Save a production configuration after the user's boundary and coverage review."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
from pathlib import Path
import uuid

from .classification import CONFIG
from .collection import profile_from
from .config import load_settings
from .geography import RegionIndex
from .runtime import atomic_json
from .sources import _hash_file
from .teradata_export import Projection


def prepare(run, output_root, config, profile_path, export_profile=None):
    run = Path(run).resolve()
    info = json.loads((run/'summary.json').read_text(encoding='utf-8-sig'))
    if info.get('status') != 'COLLECTION_PREVIEW_SUCCESS':
        raise ValueError('Prepare from a successful collection preview')
    settings = load_settings(Path(config))
    profile = profile_from(Path(profile_path))
    projection = json.loads(Path(export_profile or CONFIG/'teradata_export.json').read_text(encoding='utf-8-sig'))
    Projection(projection)
    registry = json.loads(Path(settings['regions']).read_text(encoding='utf-8'))
    if (profile['coverage_id'] != settings['coverage_id'] or registry['coverage_id'] != settings['coverage_id']
            or {s['extract'] for s in info['sources']} != set(profile['expected_extracts'])):
        raise ValueError('Reviewed snapshot and configuration coverage differ')
    boundary = info['boundary_report']
    if boundary['missing_region_codes'] or boundary['conflicting_region_codes'] or boundary['invalid_boundary_codes'] or info['unresolved_objects']:
        raise ValueError('Reviewed preview still has unresolved boundaries or POI geometry')
    source = run/'candidate_boundaries.geojson'
    RegionIndex(source,Path(settings['regions']))
    output = Path(output_root).resolve()/('approved-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8])
    output.mkdir(parents=True,exist_ok=False)
    document = json.loads(source.read_text(encoding='utf-8'))
    document.update(review_required=False,reviewed=True,review_basis='User confirmed boundary and geographic coverage review',
                    reviewed_at=datetime.now(timezone.utc).isoformat(),reviewed_source_sha256=_hash_file(source))
    atomic_json(output/'regions.geojson',document)
    # Root is absolute so all existing mappings and classifier paths keep their meaning.
    original = json.loads(Path(config).read_text(encoding='utf-8'))
    original.update(project_root=settings['root'],boundaries=str(output/'regions.geojson'),
                    coverage_confirmed=True,boundaries_reviewed=True)
    profile['complete_snapshot_confirmed'] = True
    atomic_json(output/'settings.json',original)
    atomic_json(output/'collection.json',profile)
    atomic_json(output/'teradata_export.json',projection)
    # Validate the saved files before showing commands that use them.
    saved = load_settings(output/'settings.json')
    RegionIndex(Path(saved['boundaries']),Path(saved['regions']))
    parents = [str(Path(s['path']).resolve().parent) for s in info['sources']]
    input_folder = os.path.commonpath(parents)
    report = {'status':'PRODUCTION_CONFIGURATION_PREPARED','reviewed_run':str(run),
              'settings':str(output/'settings.json'),'profile':str(output/'collection.json'),
              'boundaries':str(output/'regions.geojson'),'input_folder':input_folder,
              'collection_output':str(run.parent.parent),'boundary_source_sha256':_hash_file(source),
              'note':'No production extraction, publication or database loading was executed.'}
    def quote(value): return "'"+str(value).replace("'","''")+"'"
    command = ('& '+quote(sys.executable)+' -m poi_osm.collection '+quote(input_folder)
               +' --output '+quote(run.parent.parent)+' --config '+quote(output/'settings.json')
               +' --profile '+quote(output/'collection.json')+' --production --require-cache')
    script = ("$ErrorActionPreference = 'Stop'\nSet-Location "+quote(Path(__file__).resolve().parents[1])+'\n$runJson = '+command
              +'\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n'
              +'$published = ($runJson -join [Environment]::NewLine) | ConvertFrom-Json\n'
              +"Write-Host ('Production output: ' + $published.output)\n"
              +'& '+quote(sys.executable)+' -m poi_osm.teradata_export $published.output --output '
              +quote(run.parent.parent/'teradata')+' --profile '+quote(output/'teradata_export.json')
              +'\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n')
    (output/'run_production.ps1').write_text(script,encoding='utf-8-sig')
    report['production_script'] = str(output/'run_production.ps1')
    report['teradata_profile'] = str(output/'teradata_export.json')
    atomic_json(output/'approval.json',report)
    return output,report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--config',type=Path,default=CONFIG/'settings.json')
    parser.add_argument('--profile',type=Path,default=CONFIG/'collection.json')
    parser.add_argument('--teradata-profile',type=Path,default=CONFIG/'teradata_export.json')
    parser.add_argument('--run',dest='execute',action='store_true',help='Run production from existing checkpoints and then create the reduced warehouse package')
    args = parser.parse_args(argv)
    _,report = prepare(args.run,args.output,args.config,args.profile,args.teradata_profile)
    if args.execute:
        result = subprocess.run([sys.executable,'-m','poi_osm.collection',report['input_folder'],
                  '--output',report['collection_output'],'--config',report['settings'],
                  '--profile',report['profile'],'--production','--require-cache'],
                  cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,
                  text=True,encoding='utf-8',check=True)
        published = json.loads(result.stdout)
        from .teradata_export import export
        _,warehouse = export(published['output'],Path(report['collection_output'])/'teradata',report['teradata_profile'])
        report['status'] = 'PRODUCTION_AND_EXPORT_SUCCESS'
        report['note'] = 'Production files and reduced package created; database loading is a separate step.'
        report['production_run'] = published['output']
        report['warehouse_export'] = warehouse
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
