import json
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from engine import Engine

@pytest.fixture
def engine(tmp_path):
    images=[]
    for iid,lon,lat in [('a',179.5,10),('b',-179.5,20),('c',20,30),('d',30,40)]:
        images.append(dict(image_id=iid,lon=lon,lat=lat,year=2020,capture_time='2020-01-01',country='CN',status='complete',shard='x',physical_row=0,source_json='{}',info_json='{}',footprint=b''))
    reviews=[]
    for iid,cid,verdict,score,tags in [('a',1,'visible',80,'generator:source=solar'),('a',2,'visible',95,'plant:source=solar'),('b',1,'unrecognized',100,'generator:source=solar'),('c',1,'visible',90,'shop=solarium'),('d',1,'visible',85,'generator:source=solar')]:
        reviews.append(dict(image_id=iid,candidate_id=cid,category='power=generator',verdict=verdict,score=score,tags_text=tags,tags_json='[]',explanation='',location='',alternative=''))
    for n,rows in [('images',images),('reviews',reviews)]:pq.write_table(pa.Table.from_pylist(rows),tmp_path/(n+'.parquet'))
    (tmp_path/'manifest.json').write_text(json.dumps({'complete':True,'image_count':4}))
    return Engine(tmp_path)

def test_solar_exact_matching_dedup_and_descending(engine):
    n,rows=engine.search({'query':'光伏','verdict':'visible'})
    assert n==2
    assert [r['image_id'] for r in rows]==['a','d']
    assert [r['score'] for r in rows]==[95,85]
    assert rows[0]['candidate_id']==2 and rows[0]['matches']==2

def test_high_unrecognized_score_never_becomes_visible(engine):
    n,rows=engine.search({'query':'光伏','verdict':'unrecognized','score':90})
    assert n==1 and rows[0]['image_id']=='b'

def test_map_not_limited_by_result_page(engine):
    spec={'query':'光伏','verdict':'visible'}
    n,rows=engine.search(spec,limit=1)
    data=engine.map_points(spec,south=-90,north=90)
    assert len(rows)==1 and n==2 and data['count']==2

def test_antimeridian_bounds_and_zoom_dedup(engine):
    spec={'query':'光伏','verdict':'all','bbox':[170,-80,-170,80]}
    assert engine.search(spec)[0]==2
    data=engine.map_points({'coverage':True},west=170,east=190,south=-90,north=90)
    assert data['count']==2

def test_empty_and_stable_pagination(engine):
    assert engine.search({'query':'不存在'})==(0,[])
    spec={'query':'光伏','verdict':'visible'}
    one=engine.search(spec,limit=1,offset=0)[1][0]['image_id']
    two=engine.search(spec,limit=1,offset=1)[1][0]['image_id']
    assert one!=two

def test_solar_qualifier_requires_same_object():
    from scripts.enrich_themes import derive_context
    reviews=[{'candidate_id':4,'tags_json':json.dumps([['power','plant']])}]
    # A source tag on another unrelated object must not attach to this plant review.
    separate=[{'tags':[('power','plant')]},{'tags':[('generator:source','solar')]}]
    assert derive_context(separate,reviews)=={}
    same=[{'tags':[('power','plant'),('generator:source','solar')]}]
    assert derive_context(same,reviews)=={4:{('generator:source','solar')}}

def test_theme_overlay_does_not_highlight_unrelated_objects():
    from engine import matching_tags
    r={'category':'power=plant','tags_json':'[["power","plant"]]','context_tags_json':'[["generator:source","solar"]]'}
    assert matching_tags(r,'光伏')==[['generator:source','solar']]
    r={'category':'建筑','tags_json':'[["building","industrial"],["building","residential"]]'}
    assert matching_tags(r,'工业设施')==[['building','industrial']]

def test_bootstrap_ordinary_checkout_is_unchanged(tmp_path):
    from bootstrap import resolve_index
    import hashlib
    content=b'PAR1example';(tmp_path/'images.parquet').write_bytes(content)
    (tmp_path/'manifest.json').write_text(json.dumps({'files':{'images.parquet':{'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}}}))
    assert resolve_index(tmp_path)==tmp_path
