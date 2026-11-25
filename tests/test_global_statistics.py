import json
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from engine import Engine
from scripts.build_global_counts import count_objects

@pytest.fixture
def global_engine(tmp_path):
    images=[dict(image_id=i,year=2020,country='CN',lat=1.,lon=2.,capture_time='2020',status='complete') for i in ['a','b','c','d']]
    def review(i,cid,cat,score=90,verdict='visible',objects=1,alt='无'):
        return dict(image_id=i,candidate_id=cid,category=cat,score=score,verdict=verdict,osm_object_rows=objects,tags_text='',tags_json='[]',explanation='reason',alternative=alt,location='中央')
    records=[review('a',1,'common',100,objects=4),review('b',1,'common',95,objects=2),review('c',1,'common',90,objects=3),review('a',2,'rare',80,objects=1),review('b',2,'ambiguous',85,'possible',0,'另一种解释'),review('d',1,'common',100,'unrecognized',2)]
    for n,rows in [('images',images),('reviews',records)]:pq.write_table(pa.Table.from_pylist(rows),tmp_path/(n+'.parquet'))
    (tmp_path/'manifest.json').write_text(json.dumps({'complete':True,'image_count':4}))
    return Engine(tmp_path)

def test_global_denominator_and_verdict_scopes(global_engine):
    rows,summary=global_engine.stats('visible');common=next(r for r in rows if r['category']=='common')
    assert common==dict(category='common',records=3,images=3,object_rows=9,image_pct=75.)
    assert summary['records']==4 and summary['images']==3 and summary['image_pct']==75.
    all_rows,summary=global_engine.stats('all');common=next(r for r in all_rows if r['category']=='common')
    assert common['images']==4 and common['object_rows']==11 and common['image_pct']==100.
    filtered,filtered_summary=global_engine.stats('visible','rare')
    assert len(filtered)==1 and filtered[0]['image_pct']==25.
    assert filtered_summary==global_engine.stats('visible')[1]

def test_object_counts_deduplicate_multiple_tags_not_images():
    reviews=[{'candidate_id':1,'tags_json':'[["building","industrial"],["building:part","yes"]]'}, {'candidate_id':2,'tags_json':'[["landuse","industrial"]]'}]
    objects=[{'object_key':'x','tags':[('building','industrial'),('building:part','yes'),('landuse','industrial')]},{'object_key':'x','tags':[('building','industrial')]},{'object_key':'y','tags':[('name','Unrelated')]}]
    counts,n=count_objects(objects,reviews)
    assert counts=={1:1,2:1} and n==2
    assert count_objects(objects,reviews)[0]==counts  # A separate image gets its own occurrence count.

def test_discovery_retains_correct_candidate_and_global_rarity(global_engine):
    rows,_=global_engine.discoveries('稀有类别');a=next(r for r in rows if r['image_id']=='a')
    assert a['category']=='rare' and a['candidate_id']==2 and a['score']==80 and a['metric']==1
    assert len({r['image_id'] for r in rows})==len(rows)
    rows,_=global_engine.discoveries('类别丰富','common')
    assert rows[0]['image_id']=='a' and rows[0]['metric']==2
    rows,_=global_engine.discoveries('疑似与别解')
    assert len(rows)==1 and rows[0]['candidate_id']==2 and rows[0]['verdict']=='possible'

def test_discovery_paging_and_empty(global_engine):
    first,more=global_engine.discoveries('类别丰富',limit=1)
    second,_=global_engine.discoveries('类别丰富',limit=1,offset=1)
    assert more and first[0]['image_id']!=second[0]['image_id']
    assert global_engine.discoveries('类别丰富','missing')==([],False)

def test_exact_category_bypasses_theme_expansion(global_engine):
    n,rows=global_engine.search({'query':'光伏','exact_category':'common','verdict':'all'})
    assert n==4 and all(r['category']=='common' for r in rows)

def test_chart_numbers_remain_quantitative_when_empty():
    from analytics import statistics_chart,STAT_HEADERS
    from pandas.api.types import is_numeric_dtype
    empty=statistics_chart([])
    assert all(is_numeric_dtype(empty[c]) for c in STAT_HEADERS[1:])

def test_discovery_tab_initial_state_and_return_navigation(monkeypatch,global_engine):
    import app
    monkeypatch.setattr(app,'engine',global_engine)
    for empty in ({},[],None):
        result=app.discovery_tab_open('类别丰富','全部类别',0,empty)
        assert result[1]['page']==1 and result[1]['rows']
    state={'mode':'类别丰富','category':'全部类别','score':0,'page':2}
    assert app.discovery_tab_open('类别丰富','全部类别',0,state)[1]['page']==2
    assert app.discovery_tab_open('稀有类别','全部类别',0,state)[1]['page']==1
