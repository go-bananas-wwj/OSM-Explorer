import io
import json
import numpy as np
import pytest
from PIL import Image
from imagery import union_saved_masks,render_mask

def test_original_masks_keep_holes_and_exact_pixels():
    masks=np.zeros((4,8,9),dtype=np.uint8)
    masks[0,0,0]=1
    masks[2,1:7,1:8]=1;masks[2,3:5,3:5]=0
    masks[3,7,8]=1
    b=io.BytesIO();np.savez_compressed(b,masks=masks)
    actual=union_saved_masks(b.getvalue(),[3,2,2],(8,9))
    expected=(masks[2]|masks[3]).astype(bool)
    assert np.array_equal(actual,expected)
    base=np.full((8,9,3),80,dtype=np.uint8)
    rendered=np.array(render_mask(Image.fromarray(base),actual))
    assert np.array_equal(rendered[~expected],base[~expected])
    assert np.all(np.any(rendered[expected]!=base[expected],axis=1))
    assert not actual[3,3] and not actual[0,0]

def test_invalid_original_mask_index_rejected():
    b=io.BytesIO();np.savez_compressed(b,masks=np.zeros((2,3,4),dtype=np.uint8))
    with pytest.raises(ValueError,match='outside'):union_saved_masks(b.getvalue(),[2],(3,4))

def test_preview_visibility_boundary_and_global_order(monkeypatch):
    import app
    class FakeEngine:
        count=9
        def search(self,spec,limit=50,offset=0):
            rows=[dict(image_id=str(i),category='光伏',verdict='visible',score=100-i,lat=1,lon=2,year=2020,candidate_id=1,matches=1) for i in range(self.count)]
            return self.count,rows[offset:offset+limit]
        def image(self,iid):return {'image_id':iid}
    engine=FakeEngine();monkeypatch.setattr(app,'engine',engine)
    calls=[]
    monkeypatch.setattr(app,'rgb',lambda row:calls.append(row['image_id']) or '/tmp/example.png')
    for count in (0,1,9):
        engine.count=count;out=app.display_query({},size=20)
        assert out[-1]['visible'] is False
        assert app.load_gallery(out[3])==([], '')
    assert calls==[]
    for count in (10,25):
        engine.count=count;out=app.display_query({},page=2,size=20)
        assert out[-1]['visible'] is True
        state=out[3]
        assert [r['image_id'] for r in state['preview_rows']]==[str(i) for i in range(10)]
        gallery,status=app.load_gallery(state)
        assert len(gallery)==10
        assert [item[1].split(' · ')[-1] for item in gallery]==[str(i) for i in range(10)]

def test_small_edge_mask_zoom_contains_original_foreground():
    from imagery import mask_crop_box
    mask=np.zeros((384,384),bool);mask[261:316,0:6]=True
    x0,y0,x1,y1=mask_crop_box(mask)
    assert x0==0 and y0<=261 and y1>=316 and x1>=6
    assert (x1-x0)==(y1-y0)<100
    assert mask_crop_box(np.zeros((384,384),bool)) is None

def test_zoom_object_selection_and_untinted_view(monkeypatch,tmp_path):
    import imagery
    image=Image.fromarray(np.full((384,384,3),80,dtype=np.uint8));path=tmp_path/'source.png';image.save(path)
    large=np.zeros((384,384),bool);large[100:140,100:140]=True
    small=np.zeros((384,384),bool);small[1:3,380:384]=True
    objects=[dict(object_key='small',foreground_pixels=8,osm_type='way',osm_id=1),dict(object_key='large',foreground_pixels=1600,osm_type='way',osm_id=2)]
    monkeypatch.setattr(imagery,'rgb',lambda r:path)
    monkeypatch.setattr(imagery,'original_mask',lambda r,t,object_key=None:(large|small,objects) if object_key is None else ((large if object_key=='large' else small),objects))
    full,zoom,_,chosen,box,_=imagery.mask_views({},[])
    assert chosen['object_key']=='large' and zoom.size==(384,384)
    full,zoom,_,chosen,box,_=imagery.mask_views({},[],False,'small')
    assert chosen['object_key']=='small' and box[2]==384 and box[1]==0
    assert np.all(np.asarray(zoom)==80)
