"""Add original spatial OSM object occurrences per v2 image/category review.
One object contributes once per image/candidate, even if multiple tags match.
Context-only parent relations are excluded; these are not AI object detections.
"""
import concurrent.futures,hashlib,json,sys
from pathlib import Path
from collections import defaultdict
import pyarrow as pa
import pyarrow.parquet as pq
ROOT=Path(__file__).resolve().parents[1]

def count_objects(objects,reviews):
    lookup=defaultdict(set);counts={r['candidate_id']:0 for r in reviews}
    for r in reviews:
        for k,v in json.loads(r['tags_json']):lookup[(k,v)].add(r['candidate_id'])
    seen=set()
    for obj in objects:
        identity=obj['object_key']
        if identity in seen:continue
        seen.add(identity);ids=set()
        for pair in obj['tags']:ids.update(lookup.get(tuple(pair),()))
        for cid in ids:counts[cid]+=1
    return counts,len(seen)

def main():
    reviews=pq.read_table(ROOT/'data/reviews.parquet');by_image=defaultdict(list)
    for r in reviews.select(['image_id','candidate_id','tags_json']).to_pylist():by_image[r['image_id']].append(r)
    files=[f for f in json.loads((ROOT/'work/release_manifest.json').read_text())['files'] if f['path'].endswith('.parquet')]
    def process(f):
        rows=pq.ParquetFile(ROOT/'work/raw'/f['path']).read(columns=['image_id','osm_annotations.list.element.object_key','osm_annotations.list.element.tags']).to_pylist()
        result={};total=0
        for row in rows:
            counts,n=count_objects(row['osm_annotations'],by_image.get(row['image_id'],[]));total+=n
            for cid,count in counts.items():result[(row['image_id'],cid)]=count
        return result,total
    counts={};total_objects=0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for i,(result,n) in enumerate(pool.map(process,files),1):
            counts.update(result);total_objects+=n
            if i%50==0:print('COUNTED',i,'shards',flush=True)
    keys=reviews.select(['image_id','candidate_id']).to_pylist()
    values=[counts[(r['image_id'],r['candidate_id'])] for r in keys]
    if 'osm_object_rows' in reviews.column_names:reviews=reviews.drop(['osm_object_rows'])
    reviews=reviews.append_column('osm_object_rows',pa.array(values,type=pa.int32()))
    temp=ROOT/'data/reviews-counted.tmp';pq.write_table(reviews,temp,compression='zstd',compression_level=9,row_group_size=16384);temp.replace(ROOT/'data/reviews.parquet')
    manifest=json.loads((ROOT/'data/manifest.json').read_text());p=ROOT/'data/reviews.parquet'
    manifest['files'][p.name]={'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
    manifest['osm_annotation_rows']=total_objects
    manifest['object_count_definition']='Unique object_key occurrences within image/candidate, linked by original v2 ai_tag_reviews key/value pairs. Only osm_annotations; excludes context-only parent relations. Not individual-object AI verification.'
    (ROOT/'data/manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    print('COMPLETE',len(values),'reviews;',total_objects,'OSM object occurrences;',sum(values),'object/category associations',flush=True)
if __name__=='__main__':main()
