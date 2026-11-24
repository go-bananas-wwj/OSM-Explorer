"""Read-only analytical queries. Scores always refer to the selected AI verdict."""
import json, math, threading, os, hashlib
from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parent
LABELS = {'visible':'可见','possible':'疑似','unrecognized':'未辨认'}
THEMES = {
 '光伏': ['generator:source=solar','plant:source=solar','generator:method=photovoltaic','光伏','太阳能'],
 '工业设施': ['landuse=industrial','building=industrial','man_made=works','industrial='],
 '农田': ['耕地','landuse=farmland'], '水域': ['水面','natural=water'],
 '风电': ['generator:source=wind','plant:source=wind','风力'],
 '机场': ['aeroway=aerodrome','aeroway=runway'],
 '港口': ['landuse=port','harbour=yes','港口'],
 '矿场': ['landuse=quarry','man_made=mine'],
}
ALIASES={'solar':'光伏','photovoltaic':'光伏','工厂':'工业设施','factory':'工业设施','industrial':'工业设施','farmland':'农田','water':'水域','wind':'风电'}

def matching_tags(review,query):
    tags=json.loads(review['tags_json'])+json.loads(review.get('context_tags_json') or '[]')
    query=ALIASES.get((query or '').lower(),query or '')
    terms=THEMES.get(query,[query])
    result=[]
    for key,value in tags:
        for term in terms:
            if '=' in term:
                if key+'='+value==term or (term.endswith('=') and key+'='==term):result.append([key,value]);break
            elif term and (term.lower() in review['category'].lower() or term.lower() in (key+'='+value).lower()):result.append([key,value]);break
    return result if result else tags

class Engine:
    def __init__(self, data=None):
        self.data=Path(data or os.getenv('OSM_DATA_DIR') or ROOT/'data')
        self.manifest=json.loads((self.data/'manifest.json').read_text())
        if not self.manifest.get('complete') and os.getenv('OSM_ALLOW_PARTIAL')!='1': raise RuntimeError('The full verified index is required')
        for filename,metadata in self.manifest.get('files',{}).items():
            if hashlib.sha256((self.data/filename).read_bytes()).hexdigest()!=metadata['sha256']:raise RuntimeError('Index checksum mismatch: '+filename)
        self.lock=threading.RLock()
        self.db=duckdb.connect(config={'threads':2,'memory_limit':'1GB'})
        for name in ['images','reviews']:
            path=str(self.data/(name+'.parquet')).replace("'","''")
            self.db.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path}')")
        self.categories=self.rows('SELECT category,count(*) n FROM reviews GROUP BY category ORDER BY n DESC')
    def rows(self,sql,params=None):
        with self.lock:
            result=self.db.execute(sql,params or []);names=[c[0] for c in result.description]
            return [dict(zip(names,row)) for row in result.fetchall()]
    def base(self, spec):
        if spec.get('coverage'): return 'SELECT i.*,NULL::VARCHAR category,NULL::VARCHAR verdict,NULL::INTEGER score,NULL::INTEGER candidate_id,0::BIGINT matches FROM images i',[]
        clauses=[];params=[]
        q=(spec.get('query') or '').strip();q=ALIASES.get(q.lower(),q)
        if q and q!='全部类别':
            terms=THEMES.get(q,[q]);conditions=[]
            for t in terms:
                if '=' in t:
                    # Exact key/value boundaries, or a key-prefix when '=' has no value.
                    if t.endswith('='):conditions.append('contains(r.tags_text, ?)');params.append(t)
                    else:conditions.append("contains(chr(10)||r.tags_text||chr(10),chr(10)||?||chr(10))");params.append(t)
                else:conditions.append('(r.category ILIKE ? OR r.tags_text ILIKE ?)');params.extend(['%'+t+'%']*2)
            clauses.append('('+' OR '.join(conditions)+')')
        verdict=spec.get('verdict','visible')
        if verdict!='all': clauses.append('r.verdict=?');params.append(verdict)
        clauses.append('r.score>=?');params.append(int(spec.get('score',0)))
        if spec.get('year') and str(spec['year'])!='全部年份':clauses.append('i.year=?');params.append(int(spec['year']))
        if spec.get('country'):clauses.append("contains(','||i.country||',',','||?||',')");params.append(spec['country'])
        if spec.get('bbox'):
            west,south,east,north=spec['bbox'];clauses+=['i.lat BETWEEN ? AND ?'];params += [south,north]
            if west<=east:clauses+=['i.lon BETWEEN ? AND ?'];params +=[west,east]
            else:clauses+=['(i.lon>=? OR i.lon<=?)'];params +=[west,east]
        where=' AND '.join(clauses) or 'TRUE'
        # Each image once. Its representative is the highest-scoring matched review.
        sql=f'''SELECT * EXCLUDE (rn) FROM (
          SELECT i.*,r.category,r.verdict,r.score,r.candidate_id,
          count(*) OVER(PARTITION BY i.image_id) matches,
          row_number() OVER(PARTITION BY i.image_id ORDER BY r.score DESC NULLS LAST,r.candidate_id ASC) rn
          FROM reviews r JOIN images i USING(image_id) WHERE {where}) WHERE rn=1'''
        return sql,params
    def search(self,spec,limit=100,offset=0):
        sql,params=self.base(spec)
        order={'评分从高到低':'score DESC NULLS LAST,image_id ASC','评分从低到高':'score ASC NULLS LAST,image_id ASC','拍摄时间从新到旧':'capture_time DESC,image_id ASC'}.get(spec.get('sort'),'score DESC NULLS LAST,image_id ASC')
        n=self.rows(f'SELECT count(*) n FROM ({sql})',params)[0]['n']
        rows=self.rows(f'SELECT image_id,lon,lat,year,capture_time,country,category,verdict,score,candidate_id,matches FROM ({sql}) ORDER BY {order} LIMIT ? OFFSET ?',params+[int(limit),int(offset)])
        return n,rows
    def map_points(self,spec,west=-180,south=-85,east=180,north=85,zoom=2):
        sql,params=self.base(spec)
        width=east-west
        bounds='lat BETWEEN ? AND ?';args=[max(-90,south),min(90,north)]
        if width<359.9:
            west=(west+180)%360-180;east=(east+180)%360-180
            bounds+=' AND '+('lon BETWEEN ? AND ?' if west<=east else '(lon>=? OR lon<=?)');args +=[west,east]
        sql=f'SELECT * FROM ({sql}) WHERE {bounds}';params+=args
        step=max(0.005,360/(2**min(zoom,17)*10),math.sqrt(min(360,abs(width))*min(180,abs(north-south))/2000))
        points=self.rows(f'''SELECT floor(lon/?) gx,floor(lat/?) gy,avg(lon) lon,avg(lat) lat,count(*) n,
            max(score) score,CASE WHEN count(*)=1 THEN any_value(image_id) ELSE NULL END image_id,
            CASE WHEN count(*)=1 THEN any_value(candidate_id) ELSE NULL END candidate_id
            FROM ({sql}) GROUP BY gx,gy''',[step,step]+params)
        return {'points':points,'count':sum(p['n'] for p in points),'cell_degrees':step,'coverage':bool(spec.get('coverage'))}
    def cell_points(self,spec,gx,gy,step):
        sql,params=self.base(spec)
        return self.rows(f'''SELECT image_id,lon,lat,year,score,candidate_id,category FROM ({sql})
            WHERE lon>=? AND lon<? AND lat>=? AND lat<? ORDER BY score DESC NULLS LAST,image_id LIMIT 30''',params+[gx*step,(gx+1)*step,gy*step,(gy+1)*step])
    def image(self,image_id):
        rows=self.rows('SELECT * FROM images WHERE image_id=?',[image_id])
        if not rows:raise KeyError('Unknown image')
        return rows[0]
    def reviews(self,image_id):
        return self.rows('SELECT * FROM reviews WHERE image_id=? ORDER BY score DESC NULLS LAST,candidate_id',[image_id])
    def stats(self):
        return self.rows('''SELECT category,count(*) candidates,count(*) FILTER(WHERE verdict='visible') visible,
          count(*) FILTER(WHERE verdict='possible') possible,count(*) FILTER(WHERE verdict='unrecognized') unrecognized,
          round(100.0*count(*) FILTER(WHERE verdict='visible')/count(*),1) visible_pct
          FROM reviews GROUP BY category ORDER BY visible DESC,category''')
    def discoveries(self,mode):
        if mode=='类别丰富':
            return self.rows('''SELECT r.image_id,count(*) n,i.year,round(i.lat,3) lat,round(i.lon,3) lon
              FROM reviews r JOIN images i USING(image_id) WHERE verdict='visible'
              GROUP BY r.image_id,i.year,i.lat,i.lon ORDER BY n DESC,r.image_id LIMIT 30''')
        if mode=='疑似与别解':
            return self.rows("SELECT image_id,category,score,alternative FROM reviews WHERE verdict='possible' AND alternative IS NOT NULL AND alternative NOT IN ('无','') ORDER BY score DESC,image_id LIMIT 30")
        return self.rows('''WITH rare AS (SELECT category,count(*) n FROM reviews WHERE verdict='visible' GROUP BY category HAVING count(*) BETWEEN 1 AND 50)
            SELECT r.image_id,r.category,r.score,rare.n FROM reviews r JOIN rare USING(category) WHERE verdict='visible' ORDER BY rare.n,r.score DESC,r.image_id LIMIT 30''')
