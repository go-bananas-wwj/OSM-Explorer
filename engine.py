"""Read-only analytical queries. Scores always refer to the selected AI verdict."""
import json, math, threading, os, hashlib
from pathlib import Path
import duckdb
from bootstrap import resolve_index

ROOT = Path(__file__).resolve().parent
LABELS = {'visible':'可见','possible':'疑似','unrecognized':'未辨认','unreviewed':'无关联判读'}
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
        self.data=resolve_index(Path(data or os.getenv('OSM_DATA_DIR') or ROOT/'data'))
        self.manifest=json.loads((self.data/'manifest.json').read_text())
        if not self.manifest.get('complete') and os.getenv('OSM_ALLOW_PARTIAL')!='1': raise RuntimeError('The full verified index is required')
        for filename,metadata in self.manifest.get('files',{}).items():
            if hashlib.sha256((self.data/filename).read_bytes()).hexdigest()!=metadata['sha256']:raise RuntimeError('Index checksum mismatch: '+filename)
        self.lock=threading.RLock()
        self.db=duckdb.connect(config={'threads':2,'memory_limit':'1GB'})
        for name in ['images','reviews']:
            path=str(self.data/(name+'.parquet')).replace("'","''")
            self.db.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path}')")
        for name in ['search_catalog','name_aliases','named_objects','name_entities']:
            path=self.data/(name+'.parquet')
            if path.exists():self.db.execute("CREATE VIEW "+name+" AS SELECT * FROM read_parquet('"+str(path).replace("'","''")+"')")
        self.has_names=all((self.data/(n+'.parquet')).exists() for n in ['name_aliases','named_objects','name_entities'])
        self._catalog=None
        self._stats_cache={}
        self.has_object_counts='osm_object_rows' in [r['name'] for r in self.rows("PRAGMA table_info('reviews')")]
        self.categories=self.rows('SELECT category,count(*) n FROM reviews GROUP BY category ORDER BY n DESC')
    def rows(self,sql,params=None):
        with self.lock:
            result=self.db.execute(sql,params or []);names=[c[0] for c in result.description]
            return [dict(zip(names,row)) for row in result.fetchall()]
    def base(self, spec):
        if spec.get('coverage'): return 'SELECT i.*,NULL::VARCHAR category,NULL::VARCHAR verdict,NULL::INTEGER score,NULL::INTEGER candidate_id,0::BIGINT matches FROM images i',[]
        clauses=[];params=[]
        named=spec.get('name_entity')
        if named:
            if not self.has_names:raise ValueError('名称索引尚未就绪')
            clauses.append('o.entity=?');params.append(named)
        q=(spec.get('query') or '').strip();q=ALIASES.get(q.lower(),q)
        if named:pass
        elif spec.get('exact_tag'):
            key,value=spec['exact_tag']
            clauses.append('(list_contains(CAST(r.tags_json AS JSON)::VARCHAR[][],[?,?]) OR list_contains(CAST(r.context_tags_json AS JSON)::VARCHAR[][],[?,?]))');params.extend([key,value,key,value])
        elif spec.get('exact_category'):
            clauses.append('r.category=?');params.append(spec['exact_category'])
        elif q and q!='全部类别':
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
        minimum=int(spec.get('score',0))
        if not named or minimum>0:
            clauses.append('r.score>=?');params.append(minimum)
        if spec.get('year') and str(spec['year'])!='全部年份':clauses.append('i.year=?');params.append(int(spec['year']))
        if spec.get('country'):clauses.append("contains(','||i.country||',',','||?||',')");params.append(spec['country'])
        if spec.get('bbox'):
            west,south,east,north=spec['bbox'];clauses+=['i.lat BETWEEN ? AND ?'];params += [south,north]
            if west<=east:clauses+=['i.lon BETWEEN ? AND ?'];params +=[west,east]
            else:clauses+=['(i.lon>=? OR i.lon<=?)'];params +=[west,east]
        where=' AND '.join(clauses) or 'TRUE'
        # Each image once. Its representative is the highest-scoring matched review.
        if named:
            sql=f'''SELECT * EXCLUDE(rn) FROM (SELECT i.*,r.category,coalesce(r.verdict,'unreviewed') verdict,r.score,r.candidate_id,
              count(r.candidate_id) OVER(PARTITION BY i.image_id) matches,
              row_number() OVER(PARTITION BY i.image_id ORDER BY r.score DESC NULLS LAST,r.candidate_id ASC) rn
              FROM named_objects o JOIN images i USING(image_id) LEFT JOIN reviews r ON r.image_id=i.image_id AND list_contains(o.candidate_ids,r.candidate_id)
              WHERE {where}) WHERE rn=1'''
            return sql,params
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
    def label_catalog(self):
        if self._catalog is None:
            if (self.data/'search_catalog.parquet').exists():self._catalog=self.rows('SELECT * FROM search_catalog')
            else:
                self._catalog=self.rows("SELECT 'category:'||category AS token,category AS label,'category' AS kind,count(*) AS records,count(DISTINCT image_id) AS images,count(DISTINCT CASE WHEN verdict='visible' THEN image_id END) AS visible_images FROM reviews GROUP BY category")
        return self._catalog

    def resolve_query(self,text,mode='类别与标签'):
        from resolver import Resolver
        return Resolver(self).resolve(text or '',mode)

    def entity_aliases(self,entity):
        return self.rows('SELECT field,value FROM name_aliases WHERE entity=? ORDER BY field,value',[entity]) if self.has_names else []

    def entity_candidates(self,image_id,entity):
        rows=self.rows('SELECT candidate_ids FROM named_objects WHERE image_id=? AND entity=?',[image_id,entity]) if self.has_names else []
        return set(cid for row in rows for cid in row['candidate_ids'])

    def image(self,image_id):
        rows=self.rows('SELECT * FROM images WHERE image_id=?',[image_id])
        if not rows:raise KeyError('Unknown image')
        return rows[0]
    def reviews(self,image_id):
        return self.rows('SELECT * FROM reviews WHERE image_id=? ORDER BY score DESC NULLS LAST,candidate_id',[image_id])
    def stats(self,verdict='all',keyword='',sort='images'):
        if verdict not in ('all','visible','possible','unrecognized'):raise ValueError('Invalid verdict')
        if verdict not in self._stats_cache:
            where='' if verdict=='all' else 'WHERE verdict=?'
            params=[] if verdict=='all' else [verdict]
            objects='sum(osm_object_rows)' if self.has_object_counts else 'NULL::BIGINT'
            total=self.manifest['image_count']
            rows=self.rows(f"""SELECT category,count(*) records,count(DISTINCT image_id) images,
              {objects} object_rows,100.0*count(DISTINCT image_id)/{int(total)} image_pct
              FROM reviews {where} GROUP BY category ORDER BY images DESC,category""",params)
            summary=self.rows(f"SELECT count(*) records,count(DISTINCT image_id) images,count(DISTINCT category) categories FROM reviews {where}",params)[0]
            summary['image_pct']=100.0*summary['images']/total
            self._stats_cache[verdict]=(rows,summary)
        rows,summary=self._stats_cache[verdict]
        filtered=[dict(r) for r in rows if keyword.strip().casefold() in r['category'].casefold()]
        if sort=='objects':filtered.sort(key=lambda r:(-(r['object_rows'] or 0),r['category']))
        elif sort=='records':filtered.sort(key=lambda r:(-r['records'],r['category']))
        elif sort=='category':filtered.sort(key=lambda r:r['category'])
        return filtered,dict(summary)

    def discoveries(self,mode,category=None,min_score=0,limit=12,offset=0):
        limit=max(1,min(30,int(limit)));offset=max(0,int(offset))
        where='r.score>=?';params=[int(min_score)]
        if category and category!='全部类别':where+=' AND r.category=?';params.append(category)
        if mode=='类别丰富':
            sql=f"""WITH richness AS (SELECT image_id,count(DISTINCT category) richness FROM reviews WHERE verdict='visible' GROUP BY image_id), ranked AS (
              SELECT r.*,richness.richness,
              row_number() OVER(PARTITION BY image_id ORDER BY score DESC,candidate_id) rn
              FROM reviews r JOIN richness USING(image_id) WHERE r.verdict='visible' AND {where})
              SELECT image_id,candidate_id,category,score,verdict,richness metric,explanation,alternative
              FROM ranked WHERE rn=1 ORDER BY richness DESC,score DESC,image_id LIMIT ? OFFSET ?"""
        elif mode=='疑似与别解':
            sql=f"""SELECT image_id,candidate_id,category,score,verdict,score metric,explanation,alternative
              FROM (SELECT r.*,row_number() OVER(PARTITION BY image_id ORDER BY score DESC,candidate_id) rn
              FROM reviews r WHERE verdict='possible' AND alternative IS NOT NULL AND alternative NOT IN ('无','') AND {where})
              WHERE rn=1 ORDER BY score DESC,image_id LIMIT ? OFFSET ?"""
        else:
            sql=f"""WITH frequency AS (
                SELECT category,count(DISTINCT image_id) frequency FROM reviews WHERE verdict='visible' GROUP BY category HAVING count(DISTINCT image_id) BETWEEN 1 AND 50),
              ranked AS (SELECT r.*,f.frequency,row_number() OVER(PARTITION BY image_id ORDER BY f.frequency,r.score DESC,r.candidate_id) rn
                FROM reviews r JOIN frequency f USING(category) WHERE verdict='visible' AND {where})
              SELECT image_id,candidate_id,category,score,verdict,frequency metric,explanation,alternative
              FROM ranked WHERE rn=1 ORDER BY frequency,score DESC,image_id LIMIT ? OFFSET ?"""
        rows=self.rows(sql,params+[limit+1,offset]);has_more=len(rows)>limit;rows=rows[:limit]
        if rows:
            ids=[r['image_id'] for r in rows];positions={r['image_id']:r for r in self.rows('SELECT image_id,year,country,lat,lon FROM images WHERE image_id IN ('+','.join(['?']*len(ids))+')',ids)}
            for r in rows:r.update(positions[r['image_id']])
        return rows,has_more
