"""Build a reproducible, verified full-dataset index; resume per source shard."""
import argparse, concurrent.futures, hashlib, json, time
from pathlib import Path
import requests
import pyarrow as pa
import pyarrow.parquet as pq
from shapely import wkb

ROOT = Path(__file__).resolve().parents[1]
DATASET = 'WeijieWu/EEE-benchmark-OSM-249K-AI-Filtered-v2'
REVISION = 'a3a5fbb34b0eedab262468240d8e5e06f9f24fdc'
IMAGE_SCHEMA = pa.schema([(k, t) for k,t in [
 ('image_id',pa.string()),('lon',pa.float64()),('lat',pa.float64()),('year',pa.int32()),
 ('capture_time',pa.string()),('country',pa.string()),('status',pa.string()),
 ('shard',pa.string()),('physical_row',pa.int32()),('source_json',pa.string()),('info_json',pa.string()),('footprint',pa.binary())]])
REVIEW_SCHEMA = pa.schema([(k,t) for k,t in [
 ('image_id',pa.string()),('candidate_id',pa.int32()),('category',pa.string()),('verdict',pa.string()),
 ('score',pa.int32()),('explanation',pa.string()),('location',pa.string()),('alternative',pa.string()),('tags_json',pa.string()),('tags_text',pa.string())]])

def download(entry, cache):
    dst = cache / entry['path']; dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == entry['size_bytes']:
        if hashlib.sha256(dst.read_bytes()).hexdigest() == entry['sha256']: return dst
    url = f'https://modelscope.cn/datasets/{DATASET}/resolve/{REVISION}/{entry["path"]}'
    for attempt in range(4):
        try:
            with requests.get(url,stream=True,timeout=(15,90)) as r:
                r.raise_for_status(); digest=hashlib.sha256()
                with dst.with_suffix('.tmp').open('wb') as f:
                    for chunk in r.iter_content(1024*1024): f.write(chunk); digest.update(chunk)
            if digest.hexdigest()!=entry['sha256']: raise ValueError('SHA256 mismatch: '+entry['path'])
            dst.with_suffix('.tmp').replace(dst);return dst
        except Exception:
            if attempt==3:raise
            time.sleep(1+attempt)

def process(entry, cache, stage):
    key=entry['path'].replace('/','_'); ip=stage/(key+'.images');rp=stage/(key+'.reviews')
    if ip.exists() and rp.exists(): return entry['rows']
    path=download(entry,cache)
    table=pq.ParquetFile(path).read(columns=['image_id','image_source','image_info','ai_status','ai_feature_reviews','ai_tag_reviews'])
    images=[];reviews=[]
    for i,row in enumerate(table.to_pylist()):
        info=row['image_info'];footprint=info.pop('footprint'); geom=wkb.loads(footprint)
        # Handle footprints crossing +/-180 by unwrapping longitudes around one vertex.
        from shapely.ops import transform
        if geom.bounds[2]-geom.bounds[0]>180:
            shifted=transform(lambda x,y,z=None: (x+360 if x<0 else x,y),geom)
            center=shifted.centroid; lon=(center.x+180)%360-180;lat=center.y
        else: center=geom.centroid;lon=center.x;lat=center.y
        tags_by_id={};countries=[]
        for t in row['ai_tag_reviews']:
            if t['key'] in ('ISO3166-1','ISO3166-1:alpha2') and len(t['value'])==2:countries.append(t['value'])
            if t['candidate_id'] is not None: tags_by_id.setdefault(t['candidate_id'],set()).add((t['key'],t['value']))
        images.append(dict(image_id=row['image_id'],lon=lon,lat=lat,year=int(info['capture_time'][:4]),capture_time=info['capture_time'],country=','.join(sorted(set(countries))),status=row['ai_status'],shard=entry['path'],physical_row=i,source_json=json.dumps(row['image_source']),info_json=json.dumps(info),footprint=footprint))
        for f in row['ai_feature_reviews']:
            tags=sorted(tags_by_id.get(f['candidate_id'],[]))
            reviews.append(dict(image_id=row['image_id'],candidate_id=f['candidate_id'],category=f['category'],verdict=f['ai_verdict'],score=f['ai_visible'],explanation=f['ai_explanation'],location=f['evidence_location'],alternative=f['ai_alternative'],tags_json=json.dumps(tags,ensure_ascii=False),tags_text='\n'.join(k+'='+v for k,v in tags)))
    for records,schema,path in [(images,IMAGE_SCHEMA,ip),(reviews,REVIEW_SCHEMA,rp)]:
        pq.write_table(pa.Table.from_pylist(records,schema=schema),path.with_suffix(path.suffix+'.tmp'),compression='zstd');path.with_suffix(path.suffix+'.tmp').replace(path)
    return len(images)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--workers',type=int,default=8);ap.add_argument('--limit',type=int);args=ap.parse_args()
    work=ROOT/'work';cache=work/'raw';stage=work/'index';stage.mkdir(parents=True,exist_ok=True)
    manifest_path=work/'release_manifest.json'
    if not manifest_path.exists():
        response=requests.get(f'https://modelscope.cn/datasets/{DATASET}/resolve/39c53ecde67a4ebf1675b90ae8f0ba88b4720e77/release_manifest.json',timeout=90)
        response.raise_for_status();manifest_path.write_bytes(response.content)
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest()!='dd5ef40716b8d571651654db121502d9e824af8d4ab0da4875d58672068237dd':
        raise ValueError('Source manifest checksum mismatch')
    manifest=json.loads((work/'release_manifest.json').read_text());entries=[x for x in manifest['files'] if x['path'].endswith('.parquet')]
    if args.limit: entries=entries[:args.limit]
    start=time.time();done=0;fail=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures={pool.submit(process,e,cache,stage):e for e in entries}
        for future in concurrent.futures.as_completed(futures):
            entry=futures[future]
            try:done+=future.result()
            except Exception as exc:fail.append(entry['path']);print('FAILED',entry['path'],type(exc).__name__,str(exc)[:150],flush=True)
            if len([f for f in futures if f.done()])%10==0:print('PROGRESS',done,'images',round(time.time()-start),'sec',flush=True)
    if fail: raise RuntimeError(f'{len(fail)} failed shards; rerun to resume')
    output=ROOT/'data';output.mkdir(exist_ok=True)
    for suffix,schema in [('images',IMAGE_SCHEMA),('reviews',REVIEW_SCHEMA)]:
        with pq.ParquetWriter(output/(suffix+'.parquet'),schema,compression='zstd',compression_level=9) as writer:
            for e in entries:writer.write_table(pq.ParquetFile(stage/(e['path'].replace('/','_')+'.'+suffix)).read())
    images=pq.read_table(output/'images.parquet');reviews=pq.read_table(output/'reviews.parquet')
    ids=images.column('image_id').to_pylist();assert len(ids)==len(set(ids))
    if not args.limit:assert len(ids)==manifest['image_count']
    from collections import Counter
    stats=dict(dataset=DATASET,revision=REVISION,image_count=len(ids),review_count=reviews.num_rows,shards=len(entries),complete=not args.limit,years=sorted(set(images.column('year').to_pylist())),statuses=dict(Counter(images.column('status').to_pylist())),manifest_sha256=hashlib.sha256((work/'release_manifest.json').read_bytes()).hexdigest(),built_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),files={})
    for p in output.glob('*.parquet'):stats['files'][p.name]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    (output/'manifest.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2));print('COMPLETE',json.dumps(stats,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
