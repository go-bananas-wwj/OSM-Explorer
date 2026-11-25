"""Package a few real, provenance-tracked previews for a fast first visit."""
import concurrent.futures,json,shutil,sys,argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import Engine,ROOT,THEMES
from imagery import rgb

def main():
    p=argparse.ArgumentParser();p.add_argument('--topics',nargs='*');p.add_argument('--limit',type=int,default=10);args=p.parse_args()
    engine=Engine();dest=ROOT/'data/previews';dest.mkdir(exist_ok=True)
    rows={}
    for topic in (args.topics or THEMES):
        _,found=engine.search({'query':topic,'verdict':'visible'},limit=args.limit)
        for r in found:rows[r['image_id']]=engine.image(r['image_id'])
    def work(row):
        try:
            p=rgb(row);out=dest/(row['image_id']+'.png')
            if p!=out:shutil.copyfile(p,out)
            print('READY',row['image_id'],flush=True)
            return {'image_id':row['image_id'],'source':json.loads(row['source_json']),'capture_time':row['capture_time'],'license':'CC BY-SA 4.0','attribution':'Copernicus Sentinel-2 / Major-TOM'}
        except Exception as e:print('FAILED',row['image_id'],type(e).__name__,flush=True);return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:results=[r for r in pool.map(work,rows.values()) if r]
    previous=json.loads((dest/'provenance.json').read_text()) if (dest/'provenance.json').exists() else []
    combined={r['image_id']:r for r in previous+results}
    (dest/'provenance.json').write_text(json.dumps(list(combined.values()),ensure_ascii=False,indent=2))
    print('PREVIEWS',len(results),'of',len(rows),flush=True)
if __name__=='__main__':main()
