"""Resolve LFS pointers at runtime, avoiding large-file smudge in Studio builds."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit
import uuid
import requests

LFS_ENDPOINT='https://modelscope.cn/studios/WeijieWu/OSM-Explorer.git/info/lfs/objects/batch'

def verified(path,meta):
    return path.exists() and path.stat().st_size==meta['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==meta['sha256']

def resolve_index(data_dir):
    data_dir=Path(data_dir);manifest=json.loads((data_dir/'manifest.json').read_text())
    files=manifest.get('files',{})
    # Ordinary GitHub checkouts already contain the actual Parquet files.
    pending=[]
    for name,meta in files.items():
        path=data_dir/name
        if not path.exists():pending.append(name);continue
        with path.open('rb') as f:pointer=f.read(64).startswith(b'version https://git-lfs.github.com/spec/v1')
        if pointer:pending.append(name)
    if not pending:return data_dir
    fingerprint=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()[:16]
    cache=Path(os.getenv('OSM_CACHE_DIR',str(data_dir.parent/'.cache')))/('index-'+fingerprint)
    cache.mkdir(parents=True,exist_ok=True)
    missing=[]
    for name,meta in files.items():
        if verified(cache/name,meta):continue
        if verified(data_dir/name,meta):shutil.copyfile(data_dir/name,cache/name)
        else:missing.append(name)
    if missing:
        objects=[{'oid':files[name]['sha256'],'size':files[name]['bytes']} for name in missing]
        r=requests.post(LFS_ENDPOINT,json={'operation':'download','transfers':['basic'],'objects':objects},headers={'Accept':'application/vnd.git-lfs+json','Content-Type':'application/vnd.git-lfs+json'},timeout=(10,30));r.raise_for_status()
        actions={o['oid']:o for o in r.json().get('objects',[])}
        def download(name):
            meta=files[name];obj=actions.get(meta['sha256'],{})
            action=obj.get('actions',{}).get('download')
            if not action or obj.get('size')!=meta['bytes']:raise RuntimeError('Index object unavailable: '+name)
            href=action['href'];host=urlsplit(href).hostname or ''
            if urlsplit(href).scheme!='https' or not (host=='modelscope.cn' or host.endswith('.modelscope.cn')):raise RuntimeError('Unexpected index source')
            print('Preparing verified index:',name,flush=True)
            target=cache/name;temp=target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            try:
                digest=hashlib.sha256()
                with requests.get(href,headers=action.get('header',{}),stream=True,timeout=(10,90)) as response:
                    response.raise_for_status()
                    with temp.open('wb') as out:
                        for chunk in response.iter_content(1024*1024):out.write(chunk);digest.update(chunk)
                if digest.hexdigest()!=meta['sha256'] or temp.stat().st_size!=meta['bytes']:raise RuntimeError('Index checksum mismatch: '+name)
                temp.replace(target)
            finally:temp.unlink(missing_ok=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(download,missing))
    (cache/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    return cache
