"""OSM Explorer: Gradio UI with a full-result, viewport-aggregated Leaflet map."""
import csv, html, io, json, os, re, threading, time, uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import gradio as gr
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from shapely import wkb
from engine import Engine, LABELS, THEMES, ROOT, matching_tags
from imagery import rgb, overlay, CACHE

engine=Engine(); api=FastAPI(title='OSM Explorer')
api.mount('/osm-static',StaticFiles(directory=ROOT/'static'),name='static')
queries=OrderedDict();query_lock=threading.Lock()

def remember(spec):
    key=uuid.uuid4().hex
    with query_lock:
        queries[key]=(time.time(),spec)
        while len(queries)>128:queries.popitem(last=False)
    return key

def get_spec(key):
    if not key:return {'coverage':True}
    with query_lock:
        pair=queries.get(key)
    if not pair or time.time()-pair[0]>7200:raise HTTPException(410,'查询已过期，请重新查询')
    return pair[1]

def frame(key=''):
    return '<iframe id="osm-map" title="OSM 样本全球分布" src="/map.html?query='+key+'" loading="eager"></iframe>'

@api.get('/healthz')
def health():return {'status':'ready','images':engine.manifest['image_count'],'reviews':engine.manifest['review_count'],'complete':engine.manifest['complete'],'revision':engine.manifest['revision']}
@api.get('/map.html')
def map_page():return FileResponse(ROOT/'static/map.html')
@api.get('/api/map')
def map_data(query:str='',west:float=-180,south:float=-90,east:float=180,north:float=90,zoom:float=2):
    import math
    if not all(math.isfinite(x) for x in [west,south,east,north,zoom]) or south>=north or not 0<=zoom<=20:raise HTTPException(400,'无效地图范围')
    return engine.map_points(get_spec(query),west,south,east,north,zoom)
@api.get('/api/cell')
def cell(query:str='',gx:int=0,gy:int=0,step:float=1):
    import math
    if not math.isfinite(step) or not .005<=step<=180:raise HTTPException(400,'Invalid cell')
    return engine.cell_points(get_spec(query),gx,gy,step)
@api.get('/api/image/{image_id}')
def image_data(image_id:str):
    try:path=rgb(engine.image(image_id))
    except KeyError:raise HTTPException(404,'影像不存在')
    except Exception as exc:
        print('image fetch failed',image_id,type(exc).__name__,flush=True);raise HTTPException(502,'源影像读取失败，请重试')
    return FileResponse(path,media_type='image/png',headers={'Cache-Control':'public,max-age=86400'})
@api.get('/api/footprint/{image_id}')
def footprint(image_id:str):
    try:row=engine.image(image_id)
    except KeyError:raise HTTPException(404,'影像不存在')
    return wkb.loads(row['footprint']).__geo_interface__
@api.get('/api/export/{key}')
def export(key:str):
    spec=get_spec(key);n,rows=engine.search(spec,limit=300000)
    buf=io.StringIO();fields=['image_id','category','verdict','score','lon','lat','year','candidate_id','matches'];writer=csv.DictWriter(buf,fieldnames=fields,extrasaction='ignore');writer.writeheader()
    for row in rows:
        row={k: ("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()};writer.writerow(row)
    return Response('\ufeff'+buf.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="osm-explorer-results.csv"'})

CSS='''
.gradio-container{--block-padding:8px!important;--layout-gap:12px!important;width:100%!important;min-width:0!important;max-width:1500px!important;margin:auto!important;font-family:system-ui,sans-serif}
.gradio-container main{max-width:100%!important}
#map-frame{padding:0!important;border:1px solid #ddd;border-radius:4px;overflow:hidden}
#map-frame iframe{width:100%;height:520px;border:0;display:block}
#map-selection,#map-bounds{display:none!important}
#search-layout>div{min-width:0!important}
.intro{font-size:16px;color:#656070;line-height:1.7;margin:0 0 4px}.links{font-size:14px;margin-left:12px}#intro{padding:0!important}#intro .html-container{padding:0!important}.links a{color:#9851ed;margin-right:14px}
#result-table{overflow-x:auto}#result-table table{min-width:850px}
@media(max-width:850px){#search-layout{flex-direction:column!important}#map-frame iframe{height:380px}.gradio-container{padding:12px!important}h1{font-size:23px!important}}
'''
BRIDGE_JS='''() => {
 if(window.__osmBridge)return;window.__osmBridge=true;
 window.addEventListener('message',e=>{
 const frame=document.querySelector('#map-frame iframe');
 if(!frame||e.source!==frame.contentWindow||e.origin!==location.origin||e.data?.source!=='osm-explorer')return;
 const id=e.data.type==='select'?'map-selection':e.data.type==='bounds'?'map-bounds':null;if(!id)return;
 const input=document.querySelector('#'+id+' textarea, #'+id+' input');if(!input)return;
 input.value=JSON.stringify(e.data);input.dispatchEvent(new Event('input',{bubbles:true}));
 });
}'''
TABLE_HEADERS=['序号','AI 判读类别','判读','自评分','纬度','经度','年份','影像 ID','匹配类别数']

def table_rows(rows,offset=0):
    return [[offset+i+1,r['category'] or '—',LABELS.get(r['verdict'],'—'),r['score'],round(r['lat'],5),round(r['lon'],5),r['year'],r['image_id'],r['matches']] for i,r in enumerate(rows)]

def display_query(spec,page=1,size=50):
    page=max(1,int(page or 1));size=int(size);count,rows=engine.search(spec,limit=size,offset=(page-1)*size)
    pages=max(1,(count+size-1)//size)
    if page>pages:page=pages;count,rows=engine.search(spec,limit=size,offset=(page-1)*size)
    key=remember(spec);state={'spec':spec,'rows':rows,'key':key,'page':page,'size':size}
    choices=[(f'{(page-1)*size+i+1}. {r["image_id"]} · {r["score"] if r["score"] is not None else "—"} 分',r['image_id']+'|'+str(r['candidate_id'] or '')) for i,r in enumerate(rows)]
    status=f'共 **{count:,} 张影像**匹配 · 第 {page} / {pages} 页 · 地图展示全部匹配影像，表格每页 {size} 条。'
    if count==0:status+=' 没有符合条件的样本，可降低分数或选择其他判读。'
    export_html=f'<a href="/api/export/{key}" target="_blank" rel="noopener">下载全部 {count:,} 条匹配结果（CSV）</a>'
    return frame(key),table_rows(rows,(page-1)*size),status,state,gr.update(choices=choices,value=choices[0][1] if choices else None),gr.update(value=page),export_html,[],''

def search(query,verdict,year,score,sort,country,use_bounds,bounds,size):
    spec={'query':query or '全部类别','verdict':{'可见':'visible','疑似':'possible','未辨认':'unrecognized','全部':'all'}[verdict],'year':year,'score':score,'sort':sort,'country':(country or '').strip().upper()}
    if use_bounds:
        try:
            b=json.loads(bounds)['bounds'];w,e=b['west'],b['east'];spec['bbox']=[-180 if e-w>=359.9 else (w+180)%360-180,max(-90,b['south']),180 if e-w>=359.9 else (e+180)%360-180,min(90,b['north'])]
        except Exception:raise gr.Error('地图范围尚未就绪，请稍后重试')
    return display_query(spec,size=size)

def paginate(page,state,size):
    if not state:raise gr.Error('请先查询')
    return display_query(state['spec'],page,size)

def load_gallery(state):
    if not state or not state['rows']:return [],'当前没有结果'
    selected=state['rows'][:4];results=[];errors=[]
    def fetch(r):
        try:return str(rgb(engine.image(r['image_id']))),f'{r["category"]} · {r["score"]} 分 · {r["image_id"]}'
        except Exception:errors.append(r['image_id']);return None
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(fetch,selected):
            if result:results.append(result)
    return results,'已读取本页前 '+str(len(results))+' 张原始影像。'+(' 读取失败：'+', '.join(errors)+'；可点击重试。' if errors else '')

def detail(selection,show_overlay,current=None):
    if not selection:return None,'选择一条结果查看影像与判读。',[],[],None
    bits=selection.split('|');image_id=bits[0];cid=int(bits[1]) if len(bits)>1 and bits[1].isdigit() else None
    try:row=engine.image(image_id)
    except KeyError:raise gr.Error('样本不存在')
    reviews=engine.reviews(image_id);chosen=next((r for r in reviews if r['candidate_id']==cid),reviews[0] if reviews else None)
    info=f'**影像** `{image_id}` · 拍摄时间 {row["capture_time"]} · 经纬度 {row["lon"]:.5f}, {row["lat"]:.5f}\n\n'
    tags=json.loads(chosen['tags_json']) if chosen else []
    context_tags=json.loads(chosen.get('context_tags_json') or '[]') if chosen else []
    if context_tags:
        info+='同一 OSM 对象的主题属性：'+', '.join(k+'='+v for k,v in context_tags)+'。评分保留原始 AI 类别判读。\n\n'
        tags=tags+context_tags
    if chosen and cid is not None:
        tags=matching_tags(chosen,((current or {}).get('spec') or {}).get('query',''))
    if chosen:
        info+=f'**AI 判读类别：{html.escape(chosen["category"])}** · {LABELS.get(chosen["verdict"],chosen["verdict"])} · 自评分 **{chosen["score"]}**\n\n'
        info+=f'证据方位：{html.escape(chosen["location"] or "—")}\n\n判读理由：{html.escape(chosen["explanation"] or "—")}\n\n别解：{html.escape(chosen["alternative"] or "无")}'
    else:info+='此影像没有 AI 类别判读，处理状态：'+row['status']
    picture=None
    try:
        if show_overlay:
            picture,n,_=overlay(row,tags);info+=f'\n\n叠加 {n} 个匹配的原始 OSM 对象轮廓。'
        else:picture=str(rgb(row))
    except Exception as exc:info+='\n\n影像或轮廓暂未加载，请点击“查看影像与标签”重试。';print('detail failed',image_id,type(exc).__name__,flush=True)
    review_table=[[r['category'],LABELS.get(r['verdict'],r['verdict']),r['score'],r['location'],r['explanation'],r['alternative']] for r in reviews]
    meta={'image_id':image_id,'capture_time':row['capture_time'],'latitude':row['lat'],'longitude':row['lon'],'source':json.loads(row['source_json']),'annotation_shard':row['shard'],'dataset_revision':engine.manifest['revision']}
    return picture,info,tags,review_table,meta

def select_table(state,evt:gr.SelectData):
    if not state:return None
    i=evt.index[0] if isinstance(evt.index,(list,tuple)) else evt.index
    if i>=len(state['rows']):return None
    r=state['rows'][i];return r['image_id']+'|'+str(r['candidate_id'] or '')

def select_map(message,current):
    try:
        d=json.loads(message);iid=d['image_id'];cid=d.get('candidate_id');engine.image(iid)
        if not re.fullmatch('[a-f0-9]{20,64}',iid):return gr.skip()
        value=iid+'|'+str(cid or '')
        return gr.update(choices=[(iid,value)],value=value)
    except Exception:return gr.skip()

def stats_table():return [[r[k] for k in ['category','candidates','visible','possible','unrecognized','visible_pct']] for r in engine.stats()]
def discovery(mode):
    rows=engine.discoveries(mode)
    headers=list(rows[0]) if rows else ['image_id'];return gr.update(headers=headers,value=[[r[k] for k in headers] for r in rows]),rows

def discovery_select(rows,evt:gr.SelectData):
    if not rows:return gr.skip()
    i=evt.index[0];r=rows[i];value=r['image_id']+'|';return gr.update(choices=[(r['image_id'],value)],value=value)

with gr.Blocks(title='OSM Explorer · OSM 标签与卫星影像探索',theme=gr.themes.Default(primary_hue=gr.themes.colors.purple,font=['ui-sans-serif','system-ui','sans-serif']),css=CSS,js=BRIDGE_JS) as demo:
    gr.Markdown('# OSM Explorer：OSM 标签与卫星影像探索')
    if not engine.manifest['complete']:gr.Markdown('本地开发预览：全量索引正在构建。')
    gr.HTML(f'<div class="intro">浏览 <b>{engine.manifest["image_count"]:,} 张影像</b>中的 OSM 标签、AI 判读与地理分布。查询结果默认按自评分从高到低排列。<span class="links"><a href="https://modelscope.cn/datasets/WeijieWu/EEE-benchmark-OSM-249K-AI-Filtered-v2" target="_blank">数据集</a><a href="https://github.com/go-bananas-wwj/OSM-Explorer" target="_blank">GitHub 源码</a><a href="https://modelscope.cn/datasets/WeijieWu/EEE-benchmark-OSM-249K-AI-Filtered-v2/resolve/master/DATA_FORMAT.md" target="_blank">字段说明</a></span></div>',elem_id='intro')
    state=gr.State({});discovery_state=gr.State([])
    map_selection=gr.Textbox(elem_id='map-selection',container=False);map_bounds=gr.Textbox(elem_id='map-bounds',container=False)
    with gr.Row(elem_id='search-layout'):
        with gr.Column(scale=4,min_width=280):
            with gr.Tabs():
                with gr.Tab('标签检索'):
                    query=gr.Dropdown(list(THEMES)+['全部类别']+[r['category'] for r in engine.categories if r['category'] not in THEMES],value='光伏',allow_custom_value=True,filterable=True,label='地物类别或 OSM 标签')
                    gr.Examples([['光伏'],['工业设施'],['农田'],['水域']],inputs=query,label='常用类别')
                    verdict=gr.Radio(['可见','疑似','未辨认','全部'],value='可见',label='AI 判读结果')
                    year=gr.Dropdown(['全部年份']+[str(y) for y in engine.manifest['years']],value='全部年份',label='拍摄年份')
                    score=gr.Slider(0,100,value=0,step=1,label='自评分下限')
                    sort=gr.Dropdown(['评分从高到低','评分从低到高','拍摄时间从新到旧'],value='评分从高到低',label='结果排序')
                    submit=gr.Button('查询并显示分布',variant='primary')
                    with gr.Accordion('更多筛选',open=False):
                        country=gr.Textbox(label='国家或地区代码',placeholder='例如 CN、DE、US；留空为全球')
                        use_bounds=gr.Checkbox(label='仅查询当前地图范围',value=False)
                        size=gr.Dropdown([20,50,100,200],value=50,label='每页结果数')
                    coverage=gr.Button('显示全部覆盖',variant='secondary')
                with gr.Tab('类别统计'):
                    gr.Markdown('按影像内的 AI 类别统计候选数与可见比例。点击类别后，再点击查询。')
                    stats=gr.Dataframe(headers=['类别','候选','可见','疑似','未辨认','可见率 %'],value=stats_table(),interactive=False,max_height=420)
                with gr.Tab('样本发现'):
                    mode=gr.Radio(['类别丰富','稀有类别','疑似与别解'],value='类别丰富',label='发现方式')
                    discover=gr.Button('查看样本',variant='primary')
                    findings=gr.Dataframe(headers=['image_id','可见类别数','年份','纬度','经度'],value=[],interactive=False,max_height=400)
        with gr.Column(scale=7,min_width=0):
            map_html=gr.HTML(frame(),elem_id='map-frame')
            status=gr.Markdown('全球覆盖图包含全部影像。选择类别后查询，缩放地图可查看更细的分布。')
    table=gr.Dataframe(headers=TABLE_HEADERS,datatype=['number','str','str','number','number','number','number','str','number'],value=[],interactive=False,max_height=300,wrap=False,label='查询结果',elem_id='result-table')
    with gr.Row():
        previous=gr.Button('上一页',size='sm');page=gr.Number(value=1,minimum=1,precision=0,label='页码');go=gr.Button('跳转',size='sm');next_page=gr.Button('下一页',size='sm');export_html=gr.HTML('')
    with gr.Row():
        selection=gr.Dropdown(choices=[],label='选择样本',allow_custom_value=True,filterable=True)
        show=gr.Button('查看影像与标签',variant='primary')
    with gr.Row():
        with gr.Column(scale=1):
            selected_image=gr.Image(label='原始 Sentinel-2 RGB',type='pil',height=384,interactive=False,elem_id='selected-image')
            show_overlay=gr.Checkbox(label='叠加匹配的原始 OSM 轮廓',value=False)
        with gr.Column(scale=2):
            info=gr.Markdown('选择结果行、地图样本或下拉框中的影像。',elem_id='sample-detail')
            with gr.Tabs():
                with gr.Tab('匹配的 OSM 标签'):tag_table=gr.Dataframe(headers=['键','值'],interactive=False)
                with gr.Tab('整图全部判读'):review_table=gr.Dataframe(headers=['类别','判读','自评分','证据方位','理由','别解'],interactive=False,wrap=True)
                with gr.Tab('影像来源'):metadata=gr.JSON()
    with gr.Row():
        previews=gr.Button('加载 / 重试本页前四张影像',variant='secondary');gallery_status=gr.Markdown('')
    gallery=gr.Gallery(label='本页影像预览',columns=4,height='auto',object_fit='contain')
    gr.Markdown('评分对应所选 AI 判读；主题通过 OSM 标签匹配，详情保留原始判读类别。')
    gr.Markdown('地图展示本数据集的样本分布；一张影像只计一次。影像 © Copernicus Sentinel-2 / Major TOM（CC BY-SA 4.0）；OSM © OpenStreetMap contributors（ODbL 1.0）；底图 © Esri，海岸线 Natural Earth。')
    outputs=[map_html,table,status,state,selection,page,export_html,gallery,gallery_status]
    inputs=[query,verdict,year,score,sort,country,use_bounds,map_bounds,size]
    event=submit.click(search,inputs,outputs,api_name='search')
    event.success(load_gallery,state,[gallery,gallery_status],api_name=False)
    go.click(paginate,[page,state,size],outputs,api_name='page')
    previous.click(lambda p,s,z:paginate(max(1,p-1),s,z),[page,state,size],outputs,api_name=False)
    next_page.click(lambda p,s,z:paginate(p+1,s,z),[page,state,size],outputs,api_name=False)
    coverage.click(lambda:display_query({'coverage':True}),outputs=outputs,api_name='coverage')
    table.select(select_table,state,selection,api_name=False)
    detail_outputs=[selected_image,info,tag_table,review_table,metadata]
    selection.change(detail,[selection,show_overlay,state],detail_outputs,api_name='detail')
    show.click(detail,[selection,show_overlay,state],detail_outputs,api_name=False)
    show_overlay.change(detail,[selection,show_overlay,state],detail_outputs,api_name=False)
    previews.click(load_gallery,state,[gallery,gallery_status],api_name='previews')
    map_selection.input(select_map,[map_selection,selection],selection,api_name=False)
    def category_selected(evt:gr.SelectData):return evt.row_value[0]
    stats.select(category_selected,outputs=query,api_name=False)
    discover.click(discovery,mode,[findings,discovery_state],api_name='discover')
    findings.select(discovery_select,discovery_state,selection,api_name=False)
    def gallery_selected(current,evt:gr.SelectData):
        caption=evt.value.get('caption','') if isinstance(evt.value,dict) else ''
        for r in current.get('rows',[]):
            if r['image_id'] in caption:return r['image_id']+'|'+str(r['candidate_id'] or '')
        return gr.skip()
    gallery.select(gallery_selected,state,selection,api_name=False)
    # First page is useful immediately: real solar matches and their full distribution.
    demo.load(lambda:search('光伏','可见','全部年份',0,'评分从高到低','',False,'',50),outputs=outputs,api_name=False).success(load_gallery,state,[gallery,gallery_status],api_name=False)

demo.queue(max_size=32,default_concurrency_limit=4)
app=gr.mount_gradio_app(api,demo,path='/',allowed_paths=[str(CACHE),str(ROOT/'data/previews')])
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='0.0.0.0',port=int(os.getenv('PORT','7860')))
