"""Small region-specific warehouse CSVs for desktop inspection."""
import csv
import re
from pathlib import Path


def split_regions(source,output,registry,max_rows=50000):
    if not isinstance(max_rows,int) or isinstance(max_rows,bool) or not 1<=max_rows<=1000000:
        raise ValueError('max_rows_per_region_file must be between 1 and 1000000')
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    known={r['region_code']:r['region_name'] for r in registry['regions']}
    handles={}; counts={}; parts={}; files=[]
    with Path(source).open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream,delimiter=';'); fields=reader.fieldnames
        if not fields or 'region_code' not in fields:
            raise ValueError('Source CSV has no region_code')
        def open_part(code,name):
            if code and not all(c.isalnum() or c in '-_' for c in code):
                raise ValueError('Unsafe region code')
            part=parts.get(code,0)+1; parts[code]=part
            label=re.sub(r'[^\w .-]','_',name).rstrip(' .')[:120] or 'Без_региона'
            path=output/f"{code or 'unassigned'}_{label}_{part:03d}.csv"
            handle=path.open('x',encoding='utf-8-sig',newline='')
            writer=csv.DictWriter(handle,fieldnames=fields,delimiter=';');writer.writeheader()
            info={'region_code':code,'region_name':name,'file':str(path.relative_to(output.parent)),'rows':0}
            files.append(info);handles[code]=(handle,writer,info)
        try:
            for row in reader:
                code=row['region_code'] or ''
                name=known.get(code,row.get('region_name') or 'Без региона')
                if code not in handles:
                    open_part(code,name)
                if handles[code][2]['rows']>=max_rows:
                    handles[code][0].close();open_part(code,name)
                handles[code][1].writerow(row);handles[code][2]['rows']+=1
                counts[code]=counts.get(code,0)+1
            for code,name in sorted(known.items()):
                if code not in handles:
                    open_part(code,name)
        finally:
            for handle,_,_ in handles.values():
                handle.close()
    for info in files:
        with (output.parent/info['file']).open(encoding='utf-8-sig',newline='') as stream:
            check=csv.DictReader(stream,delimiter=';')
            if check.fieldnames!=fields or sum(1 for _ in check)!=info['rows']:
                raise ValueError('Regional CSV verification failed')
    with (output.parent/'regions_index.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['region_code','region_name','file','rows'],delimiter=';')
        writer.writeheader();writer.writerows(sorted(files,key=lambda f:(f['region_code'],f['file'])))
    return {'max_rows_per_file':max_rows,'total_rows':sum(counts.values()),'by_region':counts,'files':files}
