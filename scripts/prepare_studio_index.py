"""Bundle verified indexes in ordinary Git parts below Studio's 10-MiB limit."""
import argparse,hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def make_bundle(data_dir,output,part_size=8*1024*1024):
    data_dir=Path(data_dir);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((data_dir/'manifest.json').read_text());bundle={'files':{}}
    for name,meta in manifest['files'].items():
        source=data_dir/name
        if source.stat().st_size!=meta['bytes'] or hashlib.sha256(source.read_bytes()).hexdigest()!=meta['sha256']:raise ValueError('Invalid index '+name)
        parts=[]
        with source.open('rb') as f:
            i=0
            while data:=f.read(part_size):
                part_name=Path(name).stem+f'-{i:04d}.part';(output/part_name).write_bytes(data)
                parts.append({'name':part_name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()});i+=1
        bundle['files'][name]={**meta,'parts':parts}
    (output/'manifest.json').write_text(json.dumps(bundle,indent=2))
    return bundle

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',default=str(ROOT/'work/index-chunks'));args=p.parse_args()
    b=make_bundle(ROOT/'data',args.output);print('Verified parts:',sum(len(f['parts']) for f in b['files'].values()))
