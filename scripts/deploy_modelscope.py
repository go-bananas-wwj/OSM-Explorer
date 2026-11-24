"""Redeploy an existing ModelScope Studio after pushing its master branch.
Authentication comes from MODELSCOPE_TOKEN, never from repository files.
"""
import argparse,os,requests

def main():
    p=argparse.ArgumentParser();p.add_argument('--studio',default='WeijieWu/OSM-Explorer');args=p.parse_args()
    token=os.environ['MODELSCOPE_TOKEN']
    r=requests.post('https://modelscope.cn/openapi/v1/studios/'+args.studio+'/deploy',headers={'Authorization':'Bearer '+token},timeout=90)
    r.raise_for_status();result=r.json()
    if not result.get('success'):raise RuntimeError(result)
    print('Deployment requested:',args.studio)
if __name__=='__main__':main()
