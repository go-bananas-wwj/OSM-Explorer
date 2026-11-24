"""Associate solar/wind qualifiers via the SAME OSM object's reviewed power tag.
Never transfer an AI verdict across unrelated objects or invent solar-specific reviews.
"""
import json,sys,concurrent.futures,time
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
ROOT=Path(__file__).resolve().parents[1]

def derive_context(objects,reviews):
    ids={(k,v):r['candidate_id'] for r in reviews for k,v in json.loads(r['tags_json']) if k=='power'}
    result={}
    for obj in objects:
        tags=dict(obj['tags'])
        if tags.get('power') not in ('generator','plant'):continue
        qualifiers=[(k,v) for k,v in tags.items() if (k in ('generator:source','plant:source') and v in ('solar','wind')) or (k=='generator:method' and v=='photovoltaic')]
        cid=ids.get(('power',tags['power']))
        if cid is not None and qualifiers:result.setdefault(cid,set()).update(qualifiers)
    return result

def process(ip):
    stage=ip.parent;rp=ip.with_suffix('.reviews');out=ip.with_suffix('.enriched')
    if out.exists():return
    images=pq.ParquetFile(ip).read(columns=['shard']).to_pylist();raw=ROOT/'work/raw'/images[0]['shard']
    table=pq.ParquetFile(raw).read(columns=['image_id','osm_annotations.list.element.tags'])
    review_table=pq.ParquetFile(rp).read();by_image={}
    for r in review_table.to_pylist():by_image.setdefault(r['image_id'],[]).append(r)
    extra={}
    for row in table.to_pylist():
        rs=by_image.get(row['image_id'],[])
        for cid,tags in derive_context(row['osm_annotations'],rs).items():extra[(row['image_id'],cid)]=sorted(tags)
    records=review_table.to_pylist()
    for r in records:
        tags=extra.get((r['image_id'],r['candidate_id']),[]);r['context_tags_json']=json.dumps(tags,ensure_ascii=False)
        if tags:r['tags_text']+='\n'+'\n'.join(k+'='+v for k,v in tags)
    schema=review_table.schema.append(pa.field('context_tags_json',pa.string()))
    tmp=out.with_suffix('.enriched.tmp');pq.write_table(pa.Table.from_pylist(records,schema=schema),tmp,compression='zstd');tmp.replace(out)

def main():
    if '--watch' in sys.argv:
        while True:
            todo=[p for p in (ROOT/'work/index').glob('*.images') if p.with_suffix('.reviews').exists() and not p.with_suffix('.enriched').exists()]
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(process,todo))
            n=len(list((ROOT/'work/index').glob('*.enriched')));print('ENRICHED',n,flush=True)
            if n==491:break
            time.sleep(15)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(process,(ROOT/'work/index').glob('*.images')))
    if '--watch' in sys.argv:
        while not (ROOT/'data/manifest.json').exists():time.sleep(5)
    manifest=json.loads((ROOT/'data/manifest.json').read_text());entries=sorted((ROOT/'work/index').glob('*.enriched'))
    if len(entries)!=manifest['shards']:raise ValueError('Incomplete enrichment')
    out=ROOT/'data/reviews.parquet';tmp=out.with_suffix('.tmp')
    with pq.ParquetWriter(tmp,pq.ParquetFile(entries[0]).schema_arrow,compression='zstd',compression_level=9) as writer:
        for path in entries:writer.write_table(pq.ParquetFile(path).read())
    tmp.replace(out)
    import hashlib
    manifest['files']['reviews.parquet']={'bytes':out.stat().st_size,'sha256':hashlib.sha256(out.read_bytes()).hexdigest()};manifest['theme_mapping']='Same-object solar/wind qualifiers linked through reviewed power tag; original AI category and score unchanged.'
    (ROOT/'data/manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2));print('ENRICHMENT COMPLETE',flush=True)
if __name__=='__main__':main()
