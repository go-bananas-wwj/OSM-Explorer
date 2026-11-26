import json
import pytest
from engine import Engine
from multilingual import fold_name,is_name_key

@pytest.fixture(scope='module')
def engine():return Engine()

def test_multilingual_labels_resolve_same_real_candidates(engine):
    def tokens(q):return {r['token'] for r in engine.resolve_query(q)}
    assert tokens('工厂')==tokens('factory')==tokens('usine')
    assert not any('limousine' in t for t in tokens('usine'))
    assert tokens('solar panels')==tokens('太陽光パネル')==tokens('солнечные панели')
    assert 'theme:光伏' in tokens('solar panels')
    assert not engine.resolve_query('imaginarykey=imaginaryvalue')

def test_literal_tag_counts_and_exact_matching(engine):
    r=engine.resolve_query('water=lake')[0]
    assert r['token']=='tag:water=lake'
    for verdict,column in [('all','images'),('visible','visible_images')]:
        n,rows=engine.search({'exact_tag':['water','lake'],'verdict':verdict})
        assert n==r[column]
        for item in rows:
            review=next(x for x in engine.reviews(item['image_id']) if x['candidate_id']==item['candidate_id'])
            assert ['water','lake'] in json.loads(review['tags_json'])+json.loads(review['context_tags_json'])

def test_name_normalization_preserves_nonlatin_marks():
    assert fold_name('ÉCOLE')=='ecole'
    assert fold_name('ＡＢＣ')=='abc'
    assert fold_name('ガ')!=fold_name('カ')
    assert is_name_key('name:zh-Hans') and not is_name_key('name:etymology')

def test_name_languages_group_by_object_and_retain_scores(engine):
    groups=[engine.resolve_query(q,'地物名称') for q in ['Moscow','Москва']]
    for rows in groups:
        assert len(rows)==len({r['token'] for r in rows})
        assert any(r['token']=='name:relation/2555133' for r in rows)
    for group in groups:
        row=next(r for r in group if r['token']=='name:relation/2555133')
        n,images=engine.search({'name_entity':'relation/2555133','verdict':'all'})
        assert n==row['images']==2
        for image in images:
            allowed=engine.entity_candidates(image['image_id'],'relation/2555133')
            assert image['candidate_id'] in allowed or image['candidate_id'] is None
    obj=engine.rows('SELECT entity,image_id FROM named_objects WHERE len(candidate_ids)=0 LIMIT 1')[0]
    _,images=engine.search({'name_entity':obj['entity'],'verdict':'all'},limit=300000)
    image=next(r for r in images if r['image_id']==obj['image_id'])
    assert image['score'] is None and image['category'] is None and image['candidate_id'] is None

def test_ui_requires_fresh_candidate_selection():
    import app
    choices,state,plan,button=app.resolve_input('water=lake','类别与标签')
    assert state['items'][0]['token']=='tag:water=lake'
    with pytest.raises(Exception,match='请先查找候选'):
        app.apply_candidate('tag:water=lake',state,'factory','类别与标签')
    out=app.apply_candidate('tag:water=lake',state,'water=lake','类别与标签','可见','全部年份',0,'评分从高到低','',False,'',50)
    assert out[3]['total']==4150

def test_named_mask_selects_entity_not_shared_name(engine):
    from imagery import original_mask
    entity='relation/2555133'
    _,rows=engine.search({'name_entity':entity,'verdict':'all'})
    mask,objects=original_mask(engine.image(rows[0]['image_id']),[['name','unrelated']],entity=entity)
    assert objects and mask.any()
    assert all(f"{o['osm_type']}/{o['osm_id']}"==entity for o in objects)

def test_name_builder_only_associates_same_object_tags():
    from scripts.build_name_index import named_record
    o,names=named_record('image',{'osm_type':'way','osm_id':2,'tags':[('name','École'),('name:en','School'),('amenity','school')]},{('landuse','industrial'):{1},('amenity','school'):{2}},{1})
    assert o['candidate_ids']==[2] and not o['has_visible']
    assert {n['entity'] for n in names}=={'way/2'}
