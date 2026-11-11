"""Create a reduced warehouse package without changing the internal run or registry."""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import uuid

from .classification import CONFIG
from .region_files import split_regions
from .runtime import atomic_json
from .selection import TERADATA_FIELDS
from .sources import _hash_file

LOG = logging.getLogger(__name__)


class Projection:
    def __init__(self, profile):
        self.mode = profile.get('osm_id_format')
        self.fields = profile.get('fields')
        if self.mode not in {'typed', 'numeric_with_type'}:
            raise ValueError('osm_id_format must be typed or numeric_with_type')
        if (not isinstance(self.fields, list) or not self.fields or any(f not in TERADATA_FIELDS for f in self.fields)
                or len(self.fields) != len(set(self.fields)) or 'osm_id' not in self.fields):
            raise ValueError('fields must be unique warehouse columns including osm_id')
        if self.mode == 'numeric_with_type' and 'osm_type' not in self.fields:
            raise ValueError('Numeric osm_id requires osm_type to preserve unique object identities')

    def apply(self, source):
        kind, ident = source['osm_type'], source['osm_id']
        if kind not in {'node','way','relation'} or not ident.isascii() or not ident.isdigit() or int(ident) <= 0:
            raise ValueError('Invalid source OSM identity')
        if self.mode == 'numeric_with_type' and int(ident) > 9223372036854775807:
            raise ValueError('OSM ID exceeds signed BIGINT range')
        result = {f:source[f] for f in self.fields}
        if self.mode == 'typed':
            result['osm_id'] = f'{kind}/{ident}'
        return result


def export(run, output_root, profile_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    run = Path(run).resolve()
    info = json.loads((run/'summary.json').read_text(encoding='utf-8-sig'))
    if info.get('status') not in {'SUCCESS','COLLECTION_PREVIEW_SUCCESS'}:
        raise ValueError('Use a completed successful collection run')
    profile = json.loads(Path(profile_path).read_text(encoding='utf-8-sig'))
    projection = Projection(profile)
    source = run/'poi_teradata.csv'
    source_hash = _hash_file(source)
    regions_file = run/('approved_regions.json' if (run/'approved_regions.json').exists() else 'candidate_regions.json')
    registry = json.loads(regions_file.read_text(encoding='utf-8'))
    output = Path(output_root)/('teradata-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8])
    output.mkdir(parents=True,exist_ok=False)
    atomic_json(output/'profile.json',profile)
    atomic_json(output/'summary.json',{'status':'RUNNING','source_run':str(run)})
    dates = {'update_date','source_date','first_seen_date','last_seen_date'}
    booleans = {'is_telecom','is_chain'}
    schema = pa.schema([(f, pa.float64() if f in {'lon','lat'} else pa.date32() if f in dates else
                         pa.bool_() if f in booleans else pa.int64() if f == 'osm_id' and projection.mode == 'numeric_with_type' else pa.string()) for f in projection.fields])
    keys_path = output/'keys.sqlite'
    db = sqlite3.connect(keys_path)
    db.execute('CREATE TABLE keys(kind TEXT,id TEXT,PRIMARY KEY(kind,id)) WITHOUT ROWID')
    total = 0
    batch = []
    try:
        with source.open(encoding='utf-8-sig',newline='') as stream, (output/'poi_teradata.csv').open('x',encoding='utf-8-sig',newline='') as saved, pq.ParquetWriter(output/'poi_teradata.parquet',schema,compression='zstd') as parquet:
            reader = csv.DictReader(stream,delimiter=';')
            if not set(TERADATA_FIELDS) <= set(reader.fieldnames or []):
                raise ValueError('Source warehouse CSV is missing full internal columns')
            writer = csv.DictWriter(saved,fieldnames=projection.fields,delimiter=';'); writer.writeheader()
            for row in reader:
                selected = projection.apply(row)
                try:
                    db.execute('INSERT INTO keys VALUES(?,?)',(row['osm_type'],row['osm_id']))
                except sqlite3.IntegrityError as error:
                    raise ValueError(f'Duplicate source identity {row["osm_type"]}/{row["osm_id"]}') from error
                writer.writerow(selected)
                record = {}
                for f,value in selected.items():
                    if f in booleans and value not in {'','0','1'}:
                        raise ValueError(f'Invalid boolean value in {f}')
                    record[f] = (None if value == '' else int(value) if f == 'osm_id' and projection.mode == 'numeric_with_type' else float(value) if f in {'lon','lat'} else date.fromisoformat(value)
                                 if f in dates else bool(int(value)) if f in booleans else value)
                batch.append(record); total += 1
                if len(batch) >= 5000:
                    parquet.write_table(pa.Table.from_pylist(batch,schema=schema)); batch.clear(); db.commit()
                if total % 100000 == 0: LOG.info('Projected %s objects',total)
            if batch: parquet.write_table(pa.Table.from_pylist(batch,schema=schema))
        expected = info['object_export']['teradata_objects']
        if total != expected:
            raise ValueError(f'Source count differs from summary: {total} vs {expected}')
        with (output/'poi_teradata.csv').open(encoding='utf-8-sig',newline='') as stream:
            check = csv.DictReader(stream,delimiter=';')
            if check.fieldnames != projection.fields or sum(1 for _ in check) != total:
                raise ValueError('Projected CSV verification failed')
        with pq.ParquetFile(output/'poi_teradata.parquet') as check:
            if check.schema_arrow != schema or sum(b.num_rows for b in check.iter_batches()) != total:
                raise ValueError('Projected Parquet verification failed')
        regional = split_regions(source,output/'regions',registry,
                                 info['regional_export']['max_rows_per_file'],fieldnames=projection.fields,transform=projection.apply)
        if regional['total_rows'] != total or regional['by_region'] != info['regional_export']['by_region']:
            raise ValueError('Regional projection counts differ from source')
        if _hash_file(source) != source_hash:
            raise ValueError('Source CSV changed during projection')
        report = {'status':'TERADATA_EXPORT_SUCCESS','source_status':info['status'],'source_run':str(run),
                  'output':str(output),'source_csv_sha256':source_hash,'columns':projection.fields,
                  'osm_id_format':projection.mode,'total_rows':total,'unique_objects':total,'file_checks_ok':True,
                  'production':bool(info.get('production')),'boundaries_reviewed':bool(info.get('boundaries_reviewed')),
                  'production_ready':bool(info.get('production') and info.get('boundaries_reviewed') and info.get('complete_coverage_verified')),
                  'regional_export':regional,'excluded_fields':[f for f in TERADATA_FIELDS if f not in projection.fields]}
        atomic_json(output/'summary.json',report)
        return output,report
    except BaseException as error:
        atomic_json(output/'summary.json',{'status':'FAILED','source_run':str(run),'error':str(error)})
        raise
    finally:
        db.close(); keys_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--profile',type=Path,default=CONFIG/'teradata_export.json')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s')
    output,report = export(args.run,args.output,args.profile)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
