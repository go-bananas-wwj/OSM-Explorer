"""Materialize real v2 category/tag candidate counts, independent of query language."""
import hashlib,json,sys
from pathlib import Path
import duckdb
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

def main():
    db=duckdb.connect(config={'threads':4,'memory_limit':'2GB'})
    db.execute(f"CREATE VIEW reviews AS SELECT * FROM read_parquet('{ROOT}/data/reviews.parquet')")
    db.execute('''CREATE TEMP TABLE pairs AS SELECT DISTINCT image_id,candidate_id,verdict,kv[1] AS key,kv[2] AS value FROM reviews,UNNEST(list_concat(CAST(tags_json AS JSON)::VARCHAR[][],CAST(context_tags_json AS JSON)::VARCHAR[][])) t(kv)''')
    db.execute('''CREATE TEMP TABLE catalog AS SELECT 'category:'||category AS token,category AS label,'category' AS kind,'' AS key,'' AS value,count(*) AS records,count(DISTINCT image_id) AS images,count(DISTINCT CASE WHEN verdict='visible' THEN image_id END) AS visible_images FROM reviews GROUP BY category
    UNION ALL SELECT 'tag:'||key||'='||value,key||'='||value,'tag',key,value,count(*),count(DISTINCT image_id),count(DISTINCT CASE WHEN verdict='visible' THEN image_id END) FROM pairs GROUP BY key,value''')
    from engine import Engine,THEMES
    engine=Engine()
    for label in THEMES:
        total,_=engine.search({'query':label,'verdict':'all'},limit=0)
        visible,_=engine.search({'query':label,'verdict':'visible'},limit=0)
        if total:db.execute('INSERT INTO catalog VALUES (?,?,?,?,?,?,?,?)',['theme:'+label,label,'theme','','',None,total,visible])
    path=ROOT/'data/search_catalog.parquet'
    db.execute(f"COPY (SELECT * FROM catalog ORDER BY token) TO '{path}' (FORMAT PARQUET,COMPRESSION ZSTD)")
    manifest=json.loads((ROOT/'data/manifest.json').read_text());manifest['files'][path.name]={'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()};(ROOT/'data/manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    print('CATALOG',db.execute('SELECT kind,count(*) FROM catalog GROUP BY kind').fetchall(),path.stat().st_size,flush=True)
if __name__=='__main__':main()
