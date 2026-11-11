"""One PBF per child process so native file-map handles cannot accumulate."""
import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .classification import from_settings
from .config import load_settings
from .extract import extract_pois
from .geography import UnassignedIndex, prepare_boundaries
from .runtime import atomic_json, configure_utf8
from .sources import _hash_file, obtain_source
from .storage import Registry


def main(argv=None):
    configure_utf8()
    for stream in (sys.stdout,sys.stderr):
        if stream and hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='backslashreplace')
    cli=argparse.ArgumentParser()
    cli.add_argument('--source',type=Path,required=True)
    cli.add_argument('--job',type=Path,required=True)
    cli.add_argument('--config',type=Path,required=True)
    args=cli.parse_args(argv)
    job=args.job.resolve()
    job.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s',
                        handlers=[logging.StreamHandler(),logging.FileHandler(job/'worker.log',encoding='utf-8')],force=True)
    store=None
    try:
        settings=load_settings(args.config)
        spec=json.loads(args.source.read_text(encoding='utf-8'))
        local={k:v for k,v in spec.items() if k in {'path','sha256','source_date','max_age_days'}}
        local.update(coverage_id='collection-part',complete=False)
        source=obtain_source({'input':local},job)
        boundary_error=None
        try:
            logging.info('BOUNDARIES %s',source['path'])
            prepare_boundaries(source['path'],job/'candidate.geojson',job/'work')
        except (ValueError,RuntimeError) as error:
            boundary_error=str(error)
            logging.warning('Candidate boundary extraction failed: %s',error)
        store=Registry(job/'registry.sqlite',settings['mapping'],datetime.now(timezone.utc).date().isoformat(),source,settings['batch_size'])
        with (job/'review.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=['osm_type','osm_id','tag','review_reason','detail'],delimiter=';',extrasaction='ignore')
            writer.writeheader()
            def review(row):
                if row.get('review_reason') not in {'unknown_region','outside_configured_regions','no_usable_preview_boundary'}:
                    writer.writerow(row)
            logging.info('PARSING %s',source['path'])
            parsing=extract_pois(source['path'],settings['mapping'],settings['name_priority'],UnassignedIndex(),
                                store.insert,review,job/'work',settings.get('line_policy','midpoint'),
                                strict_geometry=False,classifier=from_settings(settings))
        store.db.commit()
        store.close()
        store=None
        candidate=job/'candidate.geojson'
        result={'job':str(job),'source':{k:str(v) if isinstance(v,Path) else v for k,v in source.items()},
                'parsing':parsing,'boundary_error':boundary_error,'registry_sha256':_hash_file(job/'registry.sqlite'),
                'candidate_sha256':_hash_file(candidate) if candidate.exists() else None}
        atomic_json(job/'result.json',result)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except Exception as error:
        if store:
            store.close()
        atomic_json(job/'failed.json',{'error':str(error)})
        logging.exception('Worker failed; files retained at %s',job)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
