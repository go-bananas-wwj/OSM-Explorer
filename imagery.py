"""Fetch only selected Parquet row groups; render the same RGB recipe used by the AI."""
import io, json, os, threading, hashlib, uuid, time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote
import requests
import numpy as np
import pyarrow.parquet as pq
from PIL import Image, ImageDraw
from shapely import wkb
from shapely.ops import transform as transform_geometry
from pyproj import Transformer

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

def overlay(row,tags):
    """Draw matching ORIGINAL OSM geometries, not an AI localization mask."""
    path=rgb(row);im=Image.open(path).convert('RGB');draw=ImageDraw.Draw(im)
    info=json.loads(row['info_json']);a,b,c,d,e,f=info['transform'];det=a*e-b*d
    project=Transformer.from_crs('EPSG:4326',info['crs'],always_xy=True)
    def pixel(x,y):
        x,y=project.transform(x,y);x-=c;y-=f
        return ((e*x-b*y)/det,(-d*x+a*y)/det)
    wanted=set(tuple(t) for t in tags);count=0
    def draw_geom(g):
        typ=g['type'];co=g.get('coordinates')
        if typ=='Polygon':
            for ring in co:draw.line([pixel(*xy[:2]) for xy in ring],fill='#ffaa20',width=2)
        elif typ=='LineString':draw.line([pixel(*xy[:2]) for xy in co],fill='#ffaa20',width=2)
        elif typ=='Point':
            x,y=pixel(*co[:2]);draw.ellipse((x-3,y-3,x+3,y+3),outline='#ffaa20',width=2)
        elif typ.startswith('Multi'):
            for part in co:draw_geom({'type':typ[5:],'coordinates':part})
        elif typ=='GeometryCollection':
            for part in g['geometries']:draw_geom(part)
    matching=[]
    for obj in annotations(row):
        if wanted.intersection(obj['tags'].items()):
            draw_geom(obj['geometry']);count+=1;matching.append(obj)
    return im,count,matching
