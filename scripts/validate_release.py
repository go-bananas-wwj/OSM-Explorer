"""Cross-check the release index against raw source rows and query semantics."""
import hashlib,json,sys,time
from pathlib import Path
import pyarrow.parquet as pq
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import Engine,ROOT

def main():
    e=Engine();m=e.manifest;start=time.time();report={'image_count':m['image_count'],'review_count':m['review_count'],'revision':m['revision'],'checks':[],'topics':{}}
    assert m['complete'] and m['image_count']==248719 and m['shards']==491
    for name,meta in m['files'].items():assert hashlib.sha256((ROOT/'data'/name).read_bytes()).hexdigest()==meta['sha256']
    report['checks'].append('All index hashes match the manifest')
    counts=e.rows('SELECT count(*) n,count(DISTINCT image_id) unique_n FROM images')[0];assert counts['n']==counts['unique_n']==248719
    assert e.rows('SELECT count(*) n FROM images WHERE lon IS NULL OR lat IS NULL OR NOT isfinite(lon) OR NOT isfinite(lat) OR abs(lon)>180 OR abs(lat)>90')[0]['n']==0
    report['checks'].append('248719 unique images with valid coordinates')
    assert e.map_points({'coverage':True},south=-90,north=90)['count']==248719
    for topic in ['光伏','工业设施','农田','水域','风电','机场','港口','矿场']:
        spec={'query':topic,'verdict':'visible'};t=time.time();count,rows=e.search(spec,limit=100)
        report['topics'][topic]={'visible_images':count,'query_seconds':round(time.time()-t,3),'top_scores':[r['score'] for r in rows[:5]]}
        assert len({r['image_id'] for r in rows})==len(rows)
        assert [r['score'] for r in rows]==sorted([r['score'] for r in rows],reverse=True)
        assert all(r['verdict']=='visible' for r in rows)
        assert e.map_points(spec,south=-90,north=90)['count']==count
        for r in rows[:2]:
            info=e.image(r['image_id']);path=ROOT/'work/raw'/info['shard']
            raw=pq.ParquetFile(path).read(columns=['image_id','ai_feature_reviews']).slice(info['physical_row'],1).to_pylist()[0]
            assert raw['image_id']==r['image_id'];review=next(f for f in raw['ai_feature_reviews'] if f['candidate_id']==r['candidate_id'])
            assert (review['category'],review['ai_verdict'],review['ai_visible'])==(r['category'],r['verdict'],r['score'])
    report['checks']+=['Default scores descend, unique images, correct verdicts across eight themes','Full-result map counts equal query counts independently of pagination','Top source reviews match original categories, verdicts and scores']
    report['elapsed_seconds']=round(time.time()-start,2);report['passed']=True
    dest=ROOT/'reports';dest.mkdir(exist_ok=True);(dest/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
