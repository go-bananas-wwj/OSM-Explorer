"""Index source multilingual names on geometrically located OSM objects.
Aliases are grouped by osm_type/osm_id, never by a translated name string.
"""
import concurrent.futures,hashlib,json,sys,time
from collections import defaultdict
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
import duckdb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from multilingual import is_name_key,normalize,fold_name
ALIAS_SCHEMA=pa.schema([('entity',pa.string()),('field',pa.string()),('value',pa.string()),('normalized',pa.string()),('folded',pa.string())])
OCC_SCHEMA=pa.schema([('image_id',pa.string()),('entity',pa.string()),('candidate_ids',pa.list_(pa.int32())),('has_visible',pa.bool_())])

def named_record(image_id,obj,lookup,visible):
    tags=dict(obj['tags']);names=[(k,v) for k,v in tags.items() if is_name_key(k) and v and v.strip()]
    if not names:return None,[]
    entity=f"{obj['osm_type']}/{obj['osm_id']}";ids=set()
    for pair in tags.items():ids.update(lookup.get(pair,()))
    occurrence=dict(image_id=image_id,entity=entity,candidate_ids=sorted(ids),has_visible=bool(ids&visible))
    aliases=[dict(entity=entity,field=k,value=v,normalized=normalize(v),folded=fold_name(v)) for k,v in names]
    return occurrence,aliases

def main():
    stage=ROOT/'work/name-index';stage.mkdir(exist_ok=True)
    by_image=defaultdict(list)
    for r in pq.read_table(ROOT/'data/reviews.parquet',columns=['image_id','candidate_id','verdict','tags_json']).to_pylist():by_image[r['image_id']].append(r)
    entries=[f for f in json.loads((ROOT/'work/release_manifest.json').read_text())['files'] if f['path'].endswith('.parquet')]
    def process(entry):
        stem=entry['path'].replace('/','_');a=stage/(stem+'.aliases');o=stage/(stem+'.objects')
        if a.exists() and o.exists():return
        rows=pq.ParquetFile(ROOT/'work/raw'/entry['path']).read(columns=['image_id','osm_annotations.list.element.osm_type','osm_annotations.list.element.osm_id','osm_annotations.list.element.tags']).to_pylist()
        aliases={};occ=[]
        for row in rows:
            lookup=defaultdict(set);visible=set()
            for r in by_image.get(row['image_id'],[]):
                for k,v in json.loads(r['tags_json']):lookup[(k,v)].add(r['candidate_id'])
                if r['verdict']=='visible':visible.add(r['candidate_id'])
            seen=set()
            for obj in row['osm_annotations']:
                entry,names=named_record(row['image_id'],obj,lookup,visible)
                if entry and entry['entity'] not in seen:occ.append(entry);seen.add(entry['entity'])
                for name in names:aliases[(name['entity'],name['field'],name['value'])]=name
        for path,data,schema in [(a,list(aliases.values()),ALIAS_SCHEMA),(o,occ,OCC_SCHEMA)]:
            temp=path.with_suffix(path.suffix+'.tmp');pq.write_table(pa.Table.from_pylist(data,schema=schema),temp,compression='zstd');temp.replace(path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for i,_ in enumerate(pool.map(process,entries),1):
            if i%25==0:print('NAMES',i,'shards',flush=True)
    db=duckdb.connect(config={'threads':4,'memory_limit':'3GB'})
    db.execute(f"CREATE TEMP TABLE aliases AS SELECT DISTINCT * FROM read_parquet('{stage}/*.aliases')")
    db.execute(f"CREATE TEMP VIEW occurrences AS SELECT * FROM read_parquet('{stage}/*.objects')")
    outputs={
      'name_aliases.parquet':'SELECT * FROM aliases ORDER BY folded,entity,field',
      'named_objects.parquet':'SELECT * FROM occurrences ORDER BY entity,image_id',
      'name_entities.parquet':f"SELECT entity,count(DISTINCT o.image_id)::INTEGER images,count(DISTINCT CASE WHEN has_visible THEN o.image_id END)::INTEGER visible_images,string_agg(DISTINCT nullif(i.country,''),';') countries,min(i.year) first_year,max(i.year) last_year FROM occurrences o JOIN read_parquet('{ROOT}/data/images.parquet') i USING(image_id) GROUP BY entity ORDER BY entity",
    }
    manifest=json.loads((ROOT/'data/manifest.json').read_text())
    for filename,sql in outputs.items():
        path=ROOT/'data'/filename;db.execute(f"COPY ({sql}) TO '{path}' (FORMAT PARQUET,COMPRESSION ZSTD,COMPRESSION_LEVEL 9,ROW_GROUP_SIZE 65536)")
        manifest['files'][filename]={'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        print(filename,path.stat().st_size,flush=True)
    counts=db.execute('SELECT count(*),count(DISTINCT entity) FROM aliases').fetchone()
    manifest['name_index']={'alias_count':counts[0],'entities':counts[1],'occurrences':db.execute('select count(*) from occurrences').fetchone()[0],'scope':'Named spatial osm_annotations only; all stored language variants retained. Same-object reviewed tags provide optional v2 associations; names are not AI visual labels.'}
    (ROOT/'data/manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2));print('DONE',manifest['name_index'],flush=True)
if __name__=='__main__':main()
