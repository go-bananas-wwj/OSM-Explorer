"""Fetch only selected Parquet row groups; render the same RGB recipe used by the AI."""
import io, json, os, threading, hashlib, uuid, time, zipfile
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote
import requests
import numpy as np
import pyarrow.parquet as pq
from PIL import Image, ImageDraw
from shapely import wkb

ROOT=Path(__file__).resolve().parent
CACHE=Path(os.getenv('OSM_CACHE_DIR',str(ROOT/'.cache')))
CACHE.mkdir(parents=True,exist_ok=True)
LOCKS=[threading.Lock() for _ in range(32)]
FETCH_LIMIT=threading.Semaphore(3)
DATASET='WeijieWu/EEE-benchmark-OSM-249K-AI-Filtered-v2'
REVISION='a3a5fbb34b0eedab262468240d8e5e06f9f24fdc'

def dataset_url(dataset,revision,path):
    if dataset not in (DATASET,'Major-TOM/Core-S2L2A-249k'):raise ValueError('Unsupported provenance dataset')
    return f'https://modelscope.cn/datasets/{dataset}/resolve/{quote(revision,safe="")}/{quote(path,safe="/")}'

class HTTPRangeFile(io.RawIOBase):
    """Bounded exact HTTP ranges. Never download a complete source imagery shard."""
    def __init__(self,url):
        self.deadline=time.monotonic()+90;self.session=requests.Session();self.pos=0;self.blocks=OrderedDict();self.blocksize=256*1024
        r=self.session.get(url,headers={'Range':'bytes=0-0'},timeout=(10,30));r.raise_for_status()
        if r.status_code!=206: raise IOError('Source does not support byte ranges')
        self.size=int(r.headers['Content-Range'].split('/')[-1]);self.url=r.url
    def readable(self):return True
    def seekable(self):return True
    def tell(self):return self.pos
    def seek(self,offset,whence=0):
        self.pos=offset if whence==0 else self.pos+offset if whence==1 else self.size+offset
        if self.pos<0:raise ValueError('Negative offset')
        return self.pos
    def read(self,size=-1):
        if size<0:size=self.size-self.pos
        size=min(size,self.size-self.pos)
        if size<=0:return b''
        chunks=[];end=self.pos+size
        while self.pos<end:
            index=self.pos//self.blocksize
            if index not in self.blocks:
                lo=index*self.blocksize;hi=min(self.size-1,lo+self.blocksize-1)
                for attempt in range(3):
                    try:
                        remaining=self.deadline-time.monotonic()
                        if remaining<=0:raise TimeoutError('Preview source timeout; retry this image')
                        r=self.session.get(self.url,headers={'Range':f'bytes={lo}-{hi}'},timeout=(min(10,remaining),min(30,remaining)));r.raise_for_status()
                        if r.status_code!=206 or len(r.content)!=hi-lo+1:raise IOError('Invalid source range response')
                        self.blocks[index]=r.content;break
                    except (requests.RequestException,IOError):
                        if attempt==2:raise
                while len(self.blocks)>32:self.blocks.popitem(last=False)
            self.blocks.move_to_end(index);block=self.blocks[index];offset=self.pos%self.blocksize
            n=min(len(block)-offset,end-self.pos);chunks.append(block[offset:offset+n]);self.pos+=n
        return b''.join(chunks)
    def readinto(self,b):
        value=self.read(len(b));b[:len(value)]=value;return len(value)
    def close(self):
        if hasattr(self,'session'):self.session.close()
        super().close()

def prune_cache():
    files=list(CACHE.glob('*.png'))
    if len(files)>800:
        for f in sorted(files,key=lambda p:p.stat().st_mtime)[:len(files)-800]:f.unlink(missing_ok=True)

def rgb(row):
    seed=ROOT/'data/previews'/(row['image_id']+'.png')
    if seed.exists():return seed
    path=CACHE/(row['image_id']+'.png')
    with LOCKS[int(hashlib.sha256(row['image_id'].encode()).hexdigest(),16)%len(LOCKS)]:
        if path.exists():return path
        source=json.loads(row['source_json'])
        url=dataset_url(source['dataset'],source['revision'],source['file'])
        with FETCH_LIMIT,HTTPRangeFile(url) as f:
            pf=pq.ParquetFile(f);names=pf.schema_arrow.names
            cols=[b for b in ['B04','B03','B02','image_id'] if b in names]
            data=pf.read_row_group(source['row_group'],columns=cols).to_pylist()
        if len(data)!=1:raise ValueError('Source row group must identify one image')
        data=data[0]
        if 'image_id' in data and data['image_id']!=row['image_id']:raise ValueError('Source identity mismatch')
        import rasterio
        bands=[];valid=None
        for band in ['B04','B03','B02']:
            with rasterio.io.MemoryFile(data[band]) as mem:
                with mem.open() as ds:
                    b=ds.read(1).astype('float32');m=(ds.read_masks(1)>0)&np.isfinite(b)&(b>0)
            bands.append(b);valid=m if valid is None else valid&m
        arr=np.stack(bands,axis=-1)
        if arr.shape[:2]!=(384,384):raise ValueError('Unexpected source dimensions')
        if not np.any(valid):raise ValueError('This source image has no valid RGB signal')
        lo,hi=np.percentile(arr[valid],[2,98]);hi=max(hi,lo+1)
        out=np.clip((arr-lo)/(hi-lo),0,1)**(1/1.8);out[~valid]=0
        im=Image.fromarray(np.round(out*255).astype('uint8'))
        temp=path.with_suffix('.'+uuid.uuid4().hex+'.tmp');im.save(temp,format='PNG');temp.replace(path);prune_cache();return path

def annotations(row):
    cache=CACHE/(row['image_id']+'.annotations.json')
    if cache.exists():return json.loads(cache.read_text())
    local=ROOT/'work'/'raw'/row['shard']
    f=local.open('rb') if local.exists() else HTTPRangeFile(dataset_url(DATASET,REVISION,row['shard']))
    with FETCH_LIMIT,f:
        pf=pq.ParquetFile(f);index=int(row['physical_row']);rg=0
        while index>=pf.metadata.row_group(rg).num_rows:index-=pf.metadata.row_group(rg).num_rows;rg+=1
        record=pf.read_row_group(rg,columns=['image_id','osm_annotations']).slice(index,1).to_pylist()[0]
    if record['image_id']!=row['image_id']:raise ValueError('Annotation identity mismatch')
    result=[]
    for a in record['osm_annotations']:
        result.append(dict(osm_type=a['osm_type'],osm_id=a['osm_id'],tags=dict(a['tags']),geometry=wkb.loads(a['geometry_in_image']).__geo_interface__))
    tmp=cache.with_suffix('.'+uuid.uuid4().hex+'.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False));tmp.replace(cache)
    return result

def union_saved_masks(blob, indices, expected_shape, capture=None):
    """Read selected original mask planes without expanding the full N×H×W array."""
    union=np.zeros(expected_shape,dtype=bool)
    if not indices:return union
    with zipfile.ZipFile(io.BytesIO(blob)) as archive, archive.open('masks.npy') as stream:
        version=np.lib.format.read_magic(stream)
        shape,fortran,dtype=np.lib.format._read_array_header(stream,version)
        if fortran or len(shape)!=3 or tuple(shape[1:])!=tuple(expected_shape) or dtype!=np.dtype('uint8'):
            raise ValueError('Unexpected original mask layout')
        plane_bytes=int(np.prod(shape[1:]));position=0
        for index in sorted(set(indices)):
            if not 0<=index<shape[0]:raise ValueError('Mask index outside original array')
            skip=index*plane_bytes-position
            while skip:
                chunk=stream.read(min(skip,1024*1024))
                if not chunk:raise ValueError('Truncated mask array')
                skip-=len(chunk);position+=len(chunk)
            data=stream.read(plane_bytes)
            if len(data)!=plane_bytes:raise ValueError('Truncated mask plane')
            plane=np.frombuffer(data,dtype=np.uint8).reshape(expected_shape)>0
            union|=plane
            if capture is not None and index in capture:capture[index]=plane.copy()
            position+=plane_bytes
    return union

def mask_cache_path(row,tags,object_key=None):
    key=hashlib.sha256(json.dumps([sorted(set(tuple(t) for t in tags)),object_key],ensure_ascii=False).encode()).hexdigest()[:16]
    return CACHE/(row['image_id']+'.mask-v2-'+key+'.npz')

def save_mask(path,mask,objects):
    temp=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    with temp.open('wb') as f:np.savez_compressed(f,mask=mask,objects=json.dumps(objects,ensure_ascii=False))
    temp.replace(path)

def original_mask(row,tags,object_key=None):
    wanted=set(tuple(t) for t in tags)
    path=mask_cache_path(row,tags,object_key)
    if path.exists():
        with np.load(path,allow_pickle=False) as saved:
            return saved['mask'].astype(bool),json.loads(str(saved['objects']))
    local=ROOT/'work'/'raw'/row['shard']
    stream=local.open('rb') if local.exists() else HTTPRangeFile(dataset_url(DATASET,REVISION,row['shard']))
    with FETCH_LIMIT,stream:
        pf=pq.ParquetFile(stream);index=int(row['physical_row']);rg=0
        while index>=pf.metadata.row_group(rg).num_rows:index-=pf.metadata.row_group(rg).num_rows;rg+=1
        record=pf.read_row_group(rg,columns=['image_id','osm_annotations.list.element.tags','osm_annotations.list.element.mask_index','osm_annotations.list.element.object_key','osm_annotations.list.element.osm_type','osm_annotations.list.element.osm_id','osm_annotations.list.element.foreground_pixels','masks_npz']).slice(index,1).to_pylist()[0]
    if record['image_id']!=row['image_id']:raise ValueError('Mask source identity mismatch')
    matching=[]
    for obj in record['osm_annotations']:
        if wanted.intersection(dict(obj['tags']).items()) and (object_key is None or obj['object_key']==object_key):
            matching.append(dict(osm_type=obj['osm_type'],osm_id=obj['osm_id'],object_key=obj['object_key'],mask_index=obj['mask_index'],foreground_pixels=obj['foreground_pixels'],tags=dict(obj['tags'])))
    info=json.loads(row['info_json'])
    largest=max(matching,key=lambda o:o['foreground_pixels'],default=None)
    capture={largest['mask_index']:None} if largest and object_key is None else {}
    mask=union_saved_masks(record['masks_npz'],[o['mask_index'] for o in matching],(info['height'],info['width']),capture)
    save_mask(path,mask,matching)
    if largest and capture:
        save_mask(mask_cache_path(row,tags,largest['object_key']),capture[largest['mask_index']],[largest])
    return mask,matching

def render_mask(image,mask):
    """Tint original foreground pixels, preserving holes and untouched background."""
    original=np.asarray(image.convert('RGB'))
    if original.shape[:2]!=mask.shape:raise ValueError('RGB/mask shape mismatch')
    result=original.copy()
    result[mask]=np.round(original[mask]*0.50+np.array([255,45,170])*0.50).astype(np.uint8)
    padded=np.pad(mask,1,constant_values=False)
    interior=(padded[1:-1,:-2]&padded[1:-1,2:]&padded[:-2,1:-1]&padded[2:,1:-1])
    result[mask&~interior]=[255,45,170]
    return Image.fromarray(result)

def overlay(row,tags):
    """Overlay the dataset's masks_npz planes selected by original mask_index."""
    image=Image.open(rgb(row)).convert('RGB')
    mask,matching=original_mask(row,tags)
    return render_mask(image,mask),len(matching),matching


def mask_crop_box(mask):
    ys,xs=np.where(mask)
    if not len(xs):return None
    h,w=mask.shape;extent=max(int(xs.max()-xs.min()+1),int(ys.max()-ys.min()+1))
    side=max(32,extent+2*max(6,int(np.ceil(extent*.2))))
    cw,ch=min(w,side),min(h,side)
    x=max(0,min(w-cw,(int(xs.min())+int(xs.max())+1-cw)//2))
    y=max(0,min(h-ch,(int(ys.min())+int(ys.max())+1-ch)//2))
    return (x,y,x+cw,y+ch)

def mask_views(row,tags,show_mask=True,object_key=None):
    image=Image.open(rgb(row)).convert('RGB')
    union,objects=original_mask(row,tags)
    objects=sorted(objects,key=lambda o:(-o['foreground_pixels'],o['object_key']))
    chosen=next((o for o in objects if o['object_key']==object_key),objects[0] if objects else None)
    full=render_mask(image,union) if show_mask else image.copy()
    if chosen is None:return full,None,objects,None,None,int(union.sum())
    mask,_=original_mask(row,tags,chosen['object_key'])
    box=mask_crop_box(mask)
    if box is None:return full,None,objects,chosen,None,int(union.sum())
    zoom=(render_mask(image,mask) if show_mask else image).crop(box)
    scale=384/max(zoom.size)
    zoom=zoom.resize((round(zoom.width*scale),round(zoom.height*scale)),Image.Resampling.NEAREST)
    ImageDraw.Draw(full).rectangle((box[0],box[1],box[2]-1,box[3]-1),outline='#00c9eb',width=2)
    return full,zoom,objects,chosen,box,int(union.sum())
