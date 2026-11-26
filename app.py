"""OSM Explorer: Gradio UI with a full-result, viewport-aggregated Leaflet map."""
import csv, html, io, json, os, re, threading, time, uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode
import gradio as gr
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from shapely import wkb
from engine import Engine, LABELS, THEMES, ROOT, matching_tags
from imagery import rgb, mask_views, CACHE
from search_ui import candidate_label, candidate_plan
from analytics import STAT_HEADERS, SCOPE, SORT, statistic_values, statistics_chart, overview, chosen_category, discovery_caption, discovery_description

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
    buf=io.StringIO();fields=['image_id','category','verdict','score','lon','lat','year','candidate_id','matches','matched_osm_entity','query'];writer=csv.DictWriter(buf,fieldnames=fields,extrasaction='ignore');writer.writeheader()
    for row in rows:
        row={**row,'matched_osm_entity':spec.get('name_entity',''),'query':spec.get('query','')}
        row={k: ("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()};writer.writerow(row)
    return Response('\ufeff'+buf.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="osm-explorer-results.csv"'})

CSS='''
.global-metrics{display:flex;flex-wrap:wrap;gap:24px;padding:8px 0 18px}.global-metrics>div{flex:1;min-width:150px}.global-metrics span{display:block;color:#656070;font-size:14px}.global-metrics strong{display:block;font-size:25px;font-weight:600;margin-top:6px}
#statistics-table{overflow-x:auto}

.gradio-container{--block-padding:8px!important;--layout-gap:12px!important;width:100%!important;min-width:0!important;max-width:1500px!important;margin:auto!important;font-family:system-ui,sans-serif}
.gradio-container main{max-width:100%!important}
#map-frame{padding:0!important;border:1px solid #ddd;border-radius:4px;overflow:hidden}
#map-frame iframe{width:100%;height:520px;border:0;display:block}
#map-selection,#map-bounds{display:none!important}
#search-layout>div{min-width:0!important}
.intro{font-size:16px;color:#656070;line-height:1.7;margin:0 0 4px}.links{font-size:14px;margin-left:12px}#intro{padding:0!important}#intro .html-container{padding:0!important}.links a{color:#9851ed;margin-right:14px}
#result-table{overflow-x:auto}#result-table table{min-width:850px}
@media(max-width:850px){#discovery-gallery .grid-container{grid-template-columns:repeat(2,minmax(0,1fr))!important}#search-layout{flex-direction:column!important}#map-frame iframe{height:380px}.gradio-container{padding:12px!important}h1{font-size:23px!important}}
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
    preview_rows=(rows[:10] if page==1 and len(rows)>=10 else engine.search(spec,limit=10)[1]) if count>=10 else []
    key=remember(spec);state={'spec':spec,'rows':rows,'preview_rows':preview_rows,'total':count,'key':key,'page':page,'size':size}
    choices=[(f'{(page-1)*size+i+1}. {r["image_id"]} · {r["score"] if r["score"] is not None else "—"} 分',r['image_id']+'|'+str(r['candidate_id'] or '')) for i,r in enumerate(rows)]
    status=f'共 **{count:,} 张影像**匹配 · 第 {page} / {pages} 页 · 地图展示全部匹配影像，表格每页 {size} 条。'
    if spec.get('exact_category'):status='**v2 精确类别：'+html.escape(spec['exact_category'])+'** · '+status
    if spec.get('exact_tag'):status='**原始标签：'+html.escape('='.join(spec['exact_tag']))+'** · '+status
    if spec.get('name_entity'):status='**OSM 对象：'+html.escape(spec['name_entity'])+'** · '+status+' 名称来自 OSM；分数仅对应此对象关联的 v2 类别判读。'
    if count==0:status+=' 没有符合条件的样本，可降低分数或选择其他判读。'
    export_html=f'<a href="/api/export/{key}" target="_blank" rel="noopener">下载全部 {count:,} 条匹配结果（CSV）</a>'
    return frame(key),table_rows(rows,(page-1)*size),status,state,gr.update(choices=choices,value=choices[0][1] if choices else None),gr.update(value=page),export_html,[],'',gr.update(visible=count>=10)

def search(query,verdict,year,score,sort,country,use_bounds,bounds,size):
    query=query or '全部类别'
    spec={'query':query,'verdict':{'可见':'visible','疑似':'possible','未辨认':'unrecognized','全部':'all'}[verdict],'year':year,'score':score,'sort':sort,'country':(country or '').strip().upper()}
    if query.startswith(('v2:','category:')):
        spec['exact_category']=query.split(':',1)[1];spec['query']=spec['exact_category']
    elif query.startswith('tag:'):
        spec['exact_tag']=query[4:].split('=',1);spec['query']=query[4:]
    elif query.startswith('name:'):spec['name_entity']=query[5:]
    elif query.startswith('theme:'):spec['query']=query[6:]
    if use_bounds:
        try:
            b=json.loads(bounds)['bounds'];w,e=b['west'],b['east'];spec['bbox']=[-180 if e-w>=359.9 else (w+180)%360-180,max(-90,b['south']),180 if e-w>=359.9 else (e+180)%360-180,min(90,b['north'])]
        except Exception:raise gr.Error('地图范围尚未就绪，请稍后重试')
    return display_query(spec,size=size)

@api.get('/api/resolve')
def resolve_api(q:str='',mode:str='类别与标签'):
    if len(q)>200:raise HTTPException(400,'检索词请保持在 200 字以内')
    return engine.resolve_query(q,mode)

def resolve_input(text,mode):
    items=resolve_api(text,mode);selected=items[0]['token'] if items else None
    return gr.update(choices=[(candidate_label(r),r['token']) for r in items],value=selected),{'text':text,'mode':mode,'items':items},candidate_plan(items[0],engine) if items else '没有找到候选。请换一个标签词、原始 key=value 或已记录的地物名称。',gr.update(interactive=bool(items))

def plan_selection(token,resolution):
    row=next((r for r in (resolution or {}).get('items',[]) if r['token']==token),None)
    return candidate_plan(row,engine)

def apply_candidate(token,resolution,text,mode,*filters):
    if not resolution or resolution.get('text')!=text or resolution.get('mode')!=mode or token not in [r['token'] for r in resolution.get('items',[])]:
        raise gr.Error('请先查找候选并选择要筛选的标签或对象')
    return search(token,*filters)

def clear_candidates():
    return gr.update(choices=[],value=None),{},'检索词已修改，请重新查找候选。',gr.update(interactive=False)

def paginate(page,state,size):
    if not state:raise gr.Error('请先查询')
    return display_query(state['spec'],page,size)

def load_gallery(state):
    if not state or state.get('total',0)<10:return [],''
    selected=state['preview_rows'][:10];results=[];errors=[]
    def fetch(r):
        try:return str(rgb(engine.image(r['image_id']))),f'{r["category"]} · {r["score"]} 分 · {r["image_id"]}'
        except Exception:errors.append(r['image_id']);return None
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(fetch,selected):
            if result:results.append(result)
    return results,'已读取当前排序前 10 张中的 '+str(len(results))+' 张原始影像。'+(' 读取失败：'+', '.join(errors)+'；可点击重试。' if errors else '')

def detail(selection,show_overlay,current=None,object_key=None):
    if not selection:return None,'选择一条结果查看影像与判读。',[],[],None,None,gr.update(choices=[],value=None),''
    bits=selection.split('|');image_id=bits[0];cid=int(bits[1]) if len(bits)>1 and bits[1].isdigit() else None
    try:row=engine.image(image_id)
    except KeyError:raise gr.Error('样本不存在')
    entity=(current or {}).get('spec',{}).get('name_entity')
    reviews=engine.reviews(image_id)
    if entity:
        allowed=engine.entity_candidates(image_id,entity);reviews=[r for r in reviews if r['candidate_id'] in allowed]
    chosen=next((r for r in reviews if r['candidate_id']==cid),reviews[0] if reviews else None)
    info=f'**影像** `{image_id}` · 拍摄时间 {row["capture_time"]} · 经纬度 {row["lon"]:.5f}, {row["lat"]:.5f}\n\n'
    tags=json.loads(chosen['tags_json']) if chosen else []
    context_tags=json.loads(chosen.get('context_tags_json') or '[]') if chosen else []
    if context_tags:
        info+='同一 OSM 对象的主题属性：'+', '.join(k+'='+v for k,v in context_tags)+'。评分保留原始 AI 类别判读。\n\n'
        tags=tags+context_tags
    if chosen and (current or {}).get('spec',{}).get('exact_category'):
        tags=json.loads(chosen['tags_json'])
    elif chosen and cid is not None:
        tags=matching_tags(chosen,((current or {}).get('spec') or {}).get('query',''))
    if chosen:
        info+=f'**AI 判读类别：{html.escape(chosen["category"])}** · {LABELS.get(chosen["verdict"],chosen["verdict"])} · 自评分 **{chosen["score"]}**\n\n'
        info+=f'证据方位：{html.escape(chosen["location"] or "—")}\n\n判读理由：{html.escape(chosen["explanation"] or "—")}\n\n别解：{html.escape(chosen["alternative"] or "无")}'
    else:info+=('此对象没有关联的 v2 类别判读，评分留空。' if entity else '此影像没有 AI 类别判读，处理状态：'+row['status'])
    if (current or {}).get('spec',{}).get('exact_tag'):tags=[current['spec']['exact_tag']]
    if entity:
        aliases=engine.entity_aliases(entity)
        info+='\n\n**OSM 对象** `'+html.escape(entity)+'`；名称为原始元数据。'
        tags=[[r['field'],r['value']] for r in aliases]
    picture=None;zoom=None;choices=[];chosen_object=None;zoom_info=''
    try:
        picture,zoom,objects,chosen_object,box,pixels=mask_views(row,tags,show_overlay,object_key,**({"entity":entity} if entity else {}))
        choices=[(f"{o['osm_type']}/{o['osm_id']} · {o['foreground_pixels']:,} 像元",o['object_key']) for o in objects]
        info+=f'\n\n匹配 {len(objects)} 个原始 OSM 对象，mask 合计 {pixels:,} 像元（{100*pixels/(picture.width*picture.height):.2f}%）。'
        if box:
            zoom_info=f"当前对象：{chosen_object['osm_type']}/{chosen_object['osm_id']} · {chosen_object['foreground_pixels']:,} 个 mask 像元 · 裁剪 {box[2]-box[0]} × {box[3]-box[1]} 个原始像元。青色框为放大范围，粉色为原始 mask。"
        else:zoom_info='当前对象的原始 mask 没有前景像元，无法生成局部放大图。' if objects else '当前样本没有匹配的原始 OSM mask。'
    except Exception as exc:
        try:picture=str(rgb(row))
        except Exception:pass
        info+='\n\n原始 mask 暂未加载，请点击“查看影像与标签”重试。';print('detail failed',image_id,type(exc).__name__,flush=True)
    review_table=[[r['category'],LABELS.get(r['verdict'],r['verdict']),r['score'],r['location'],r['explanation'],r['alternative']] for r in reviews]
    meta={'image_id':image_id,'capture_time':row['capture_time'],'latitude':row['lat'],'longitude':row['lon'],'source':json.loads(row['source_json']),'annotation_shard':row['shard'],'dataset_revision':engine.manifest['revision']}
    if entity:meta['matched_osm_entity']=entity
    return picture,info,tags,review_table,meta,zoom,gr.update(choices=choices,value=chosen_object['object_key'] if chosen_object else None),zoom_info

def select_table(state,evt:gr.SelectData):
    if not state:return None
    i=evt.index[0] if isinstance(evt.index,(list,tuple)) else evt.index
    if i>=len(state['rows']):return None
    r=state['rows'][i];return r['image_id']+'|'+str(r['candidate_id'] or '')

def select_review(selection,evt:gr.SelectData):
    if not selection:return gr.skip()
    image_id=selection.split('|')[0];category=evt.row_value[0]
    review=next((r for r in engine.reviews(image_id) if r['category']==category),None)
    if not review:return gr.skip()
    value=image_id+'|'+str(review['candidate_id'])
    return gr.update(choices=[(image_id+' · '+category,value)],value=value)

def select_map(message,current):
    try:
        d=json.loads(message);iid=d['image_id'];cid=d.get('candidate_id');engine.image(iid)
        if not re.fullmatch('[a-f0-9]{20,64}',iid):return gr.skip()
        value=iid+'|'+str(cid or '')
        return gr.update(choices=[(iid,value)],value=value)
    except Exception:return gr.skip()

def refresh_stats(scope,keyword,order):
    rows,summary=engine.stats(SCOPE[scope],keyword,SORT[order])
    choices=[r['category'] for r in rows];selected=choices[0] if choices else None
    text=f"**当前口径：{scope}** · {summary['records']:,} 条 AI 判读 · 覆盖 {summary['images']:,} 张影像（全库的 {summary['image_pct']:.4f}%） · {summary['categories']:,} 类。当前列表匹配 {len(rows):,} 类。"
    link='/api/statistics.csv?'+urlencode({'scope':SCOPE[scope],'keyword':keyword,'sort':SORT[order]})
    return statistic_values(rows),statistics_chart(rows),text,gr.update(choices=choices,value=selected),rows,chosen_category(selected,rows,engine.manifest['image_count']),f'<a href="{html.escape(link,quote=True)}" target="_blank">下载当前全局统计（CSV）</a>'

def statistic_selection(evt:gr.SelectData):return evt.row_value[0]

def open_statistic(category,scope):
    if not category:raise gr.Error('请先选择一个类别')
    spec={'query':category,'exact_category':category,'verdict':SCOPE[scope],'score':0,'sort':'评分从高到低','year':'全部年份'}
    result=display_query(spec)
    return (*result,gr.update(selected='explore'),'v2:'+category,'全部' if scope=='全部判读' else scope,'全部年份',0,'评分从高到低','',False,50)

def discovery(mode,category,min_score,page=1):
    page=max(1,int(page or 1));rows,more=engine.discoveries(mode,category,min_score,12,(page-1)*12)
    current={'rows':rows,'mode':mode,'category':category,'score':min_score,'page':page,'more':more}
    choices=[(f"{r['category']} · {r['score']} 分 · {r['image_id']}",r['image_id']) for r in rows]
    selected=rows[0] if rows else None
    status=f'第 {page} 组 · {len(rows)} 张影像 · '+('还有下一组' if more else '已到最后一组')
    return [],current,status,gr.update(choices=choices,value=selected['image_id'] if selected else None),discovery_description(selected,mode),gr.update(value=page),gr.update(interactive=more),gr.update(interactive=page>1)

def discovery_tab_open(mode,category,min_score,current):
    current=current if isinstance(current,dict) else {}
    same=(current.get('mode'),current.get('category'),current.get('score'))==(mode,category,min_score)
    return discovery(mode,category,min_score,current.get('page',1) if same else 1)

def discovery_gallery(current):
    rows=(current or {}).get('rows',[])
    if not rows:
        yield [],'没有符合条件的样本。';return
    images={};failed=[]
    def fetch(r):return (str(rgb(engine.image(r['image_id']))),discovery_caption(r,current['mode']))
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs={pool.submit(fetch,r):i for i,r in enumerate(rows)}
        for job in as_completed(jobs):
            i=jobs[job]
            try:images[i]=job.result()
            except Exception:failed.append(rows[i]['image_id'])
            yield [images[i] for i in sorted(images)],f"第 {current['page']} 组 · 已加载 {len(images)} / {len(rows)} 张影像"+(' · 可点击重试加载失败的影像' if failed else '')

def choose_discovery(current,evt:gr.SelectData):
    caption=evt.value.get('caption','') if isinstance(evt.value,dict) else ''
    for row in current.get('rows',[]):
        if row['image_id'] in caption:return row['image_id']
    return gr.skip()

def describe_discovery(image_id,current):
    row=next((r for r in (current or {}).get('rows',[]) if r['image_id']==image_id),None)
    return discovery_description(row,(current or {}).get('mode',''))

def open_discovery(image_id,current):
    row=next((r for r in (current or {}).get('rows',[]) if r['image_id']==image_id),None)
    if not row:raise gr.Error('请先选择一个发现样本')
    scope=LABELS[row['verdict']]
    result=list(open_statistic(row['category'],scope))
    value=row['image_id']+'|'+str(row['candidate_id'])
    choices=[c for c in result[4]['choices'] if c[1]!=value];choices.insert(0,(f"发现样本 · {row['image_id']} · {row['score']} 分",value))
    result[4]=gr.update(choices=choices,value=value)
    return tuple(result)

@api.get('/api/statistics')
def statistics_api(scope:str='all',keyword:str='',sort:str='images'):
    if scope not in SCOPE.values():raise HTTPException(400,'无效判读范围')
    rows,summary=engine.stats(scope,keyword,sort)
    return {'dataset':engine.manifest['dataset'],'revision':engine.manifest['revision'],'image_denominator':engine.manifest['image_count'],'scope':scope,'summary':summary,'categories':rows}

@api.get('/api/statistics.csv')
def statistics_export(scope:str='all',keyword:str='',sort:str='images'):
    result=statistics_api(scope,keyword,sort);buf=io.StringIO();writer=csv.writer(buf);writer.writerow(STAT_HEADERS+['全库影像数','判读范围','数据版本'])
    for row in statistic_values(result['categories']):
        writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in row]+[result['image_denominator'],scope,result['revision']])
    return Response('\ufeff'+buf.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="osm-v2-global-statistics.csv"'})

with gr.Blocks(title='OSM Explorer · OSM 标签与卫星影像探索',theme=gr.themes.Default(primary_hue=gr.themes.colors.purple,font=['ui-sans-serif','system-ui','sans-serif']),css=CSS,js=BRIDGE_JS) as demo:
    gr.Markdown('# OSM Explorer：OSM 标签与卫星影像探索')
    if not engine.manifest['complete']:gr.Markdown('本地开发预览：全量索引正在构建。')
    gr.HTML(f'<div class="intro">浏览 <b>{engine.manifest["image_count"]:,} 张影像</b>中的 <b>v2</b> OSM 标签、AI 判读与地理分布。查询结果默认按自评分从高到低排列。<span class="links"><a href="https://modelscope.cn/datasets/WeijieWu/EEE-benchmark-OSM-249K-AI-Filtered-v2" target="_blank">数据集</a><a href="https://github.com/go-bananas-wwj/OSM-Explorer" target="_blank">GitHub 源码</a><a href="https://modelscope.cn/datasets/WeijieWu/EEE-benchmark-OSM-249K-AI-Filtered-v2/resolve/master/DATA_FORMAT.md" target="_blank">字段说明</a></span></div>',elem_id='intro')
    state=gr.State({});discovery_state=gr.State({})
    map_selection=gr.Textbox(elem_id='map-selection',container=False);map_bounds=gr.Textbox(elem_id='map-bounds',container=False)
    with gr.Tabs(selected='explore') as main_tabs:
        with gr.Tab('标签检索',id='explore'):
            with gr.Row(elem_id='search-layout'):
                with gr.Column(scale=4,min_width=280):
                    search_mode=gr.Radio(['类别与标签','地物名称'],value='类别与标签',label='检索内容')
                    query=gr.Textbox(value='光伏',label='输入标签或名称',placeholder='光伏 / solar panels / usine / name:en=Moscow',elem_id='query-control')
                    resolve_button=gr.Button('查找候选')
                    initial=engine.resolve_query('光伏')
                    resolution=gr.State({'text':'光伏','mode':'类别与标签','items':initial})
                    candidate=gr.Dropdown([(candidate_label(r),r['token']) for r in initial],value=initial[0]['token'],label='选择实际匹配项（全库影像数）',elem_id='candidate-control')
                    with gr.Accordion('匹配依据与名称说明',open=False):
                        plan=gr.Markdown(candidate_plan(initial[0],engine))
                    gr.Markdown('支持常用多语言类别词；地物名称搜索 OSM 已记录的各语言名称。')
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
                with gr.Column(scale=7,min_width=0):
                    map_html=gr.HTML(frame(),elem_id='map-frame')
                    status=gr.Markdown('全球覆盖图包含全部影像。选择类别后查询，缩放地图可查看更细的分布。')
            table=gr.Dataframe(headers=TABLE_HEADERS,datatype=['number','str','str','number','number','number','number','str','number'],value=[],interactive=False,max_height=300,wrap=False,label='查询结果',elem_id='result-table')
            with gr.Row():
                previous=gr.Button('上一页',size='sm');page=gr.Number(value=1,minimum=1,precision=0,label='页码');go=gr.Button('跳转',size='sm');next_page=gr.Button('下一页',size='sm');export_html=gr.HTML('')
            with gr.Row():
                selection=gr.Dropdown(choices=[],label='选择样本',allow_custom_value=True,filterable=True,elem_id='sample-selection')
                show=gr.Button('查看影像与标签',variant='primary')
            with gr.Row():
                with gr.Column(scale=2):
                    with gr.Row():
                        selected_image=gr.Image(label='整图与原始 OSM mask',type='pil',format='png',height=384,interactive=False,elem_id='selected-image')
                        zoom_image=gr.Image(label='选中 mask 的局部放大',type='pil',format='png',height=384,interactive=False,elem_id='zoom-image')
                    show_overlay=gr.Checkbox(label='显示原始 OSM mask（半透明填色）',value=True)
                    object_selection=gr.Dropdown(choices=[],label='选择放大对象（按 mask 面积排序）',filterable=True,elem_id='mask-object-selection')
                    zoom_info=gr.Markdown('',elem_id='zoom-info')
                with gr.Column(scale=1):
                    info=gr.Markdown('选择结果行、地图样本或下拉框中的影像。',elem_id='sample-detail')
                    with gr.Tabs():
                        with gr.Tab('匹配的 OSM 标签'):tag_table=gr.Dataframe(headers=['键','值'],interactive=False)
                        with gr.Tab('相关类别判读'):review_table=gr.Dataframe(headers=['类别','判读','自评分','证据方位','理由','别解'],interactive=False,wrap=True)
                        with gr.Tab('影像来源'):metadata=gr.JSON()
            with gr.Group(visible=False,elem_id='top-ten-preview') as preview_panel:
                with gr.Row():
                    previews=gr.Button('加载 / 重试前 10 张影像',variant='secondary');gallery_status=gr.Markdown('')
                gallery=gr.Gallery(label='当前排序前 10 张影像',columns=5,height='auto',object_fit='contain')
            gr.Markdown('评分对应所选 AI 判读；主题通过 OSM 标签匹配，详情保留原始判读类别。')
            gr.Markdown('地图展示本数据集的样本分布；一张影像只计一次。影像 © Copernicus Sentinel-2 / Major TOM（CC BY-SA 4.0）；OSM © OpenStreetMap contributors（ODbL 1.0）；底图 © Esri，海岸线 Natural Earth。')
        with gr.Tab('全局统计',id='statistics') as statistics_tab:
            gr.HTML(overview(engine))
            gr.Markdown('每个类别有多少条记录、覆盖多少张影像、占全库多少比例。统计不受地图范围、年份或检索页分数筛选影响。')
            with gr.Row():
                stat_scope=gr.Radio(list(SCOPE),value='可见',label='判读范围',scale=2)
                stat_keyword=gr.Textbox(label='搜索 v2 类别',placeholder='例如 道路、建筑、power=plant',scale=2)
                stat_sort=gr.Dropdown(list(SORT),value='覆盖影像数',label='列表排序',scale=1)
                stat_apply=gr.Button('更新统计',variant='primary',scale=1)
            stats_rows=gr.State([])
            stat_summary=gr.Markdown()
            with gr.Row():
                stat_plot=gr.BarPlot(value=statistics_chart(engine.stats('visible')[0]),x='v2 类别',y='覆盖影像数',sort='-y',x_label_angle=-35,tooltip=STAT_HEADERS,height=380,label='影像覆盖数量 Top 15',scale=2)
                with gr.Column(scale=1):
                    stat_category=gr.Dropdown(choices=[],label='选择类别查看详情',filterable=True)
                    stat_detail=gr.Markdown()
                    stat_open=gr.Button('在地图中查看该类样本',variant='primary')
            stats=gr.Dataframe(headers=STAT_HEADERS,value=[],datatype=['str','number','number','number','number'],column_widths=['32%','15%','23%','15%','15%'],interactive=False,wrap=True,max_height=520,label='全局类别统计',elem_id='statistics-table')
            stat_export=gr.HTML()
            with gr.Accordion('数据版本与统计口径',open=False):
                gr.Markdown(f"数据来自 [{engine.manifest['dataset']}](https://modelscope.cn/datasets/{engine.manifest['dataset']})，固定版本 `{engine.manifest['revision']}`。AI 类别、判读、评分均沿用 v2 的 `ai_feature_reviews`。\n\n- **AI 判读条数**：图像级类别判读记录；v2 同图同类一条，因此单类的记录数与覆盖影像数相同。\n- **图内 OSM 对象关联条数**：根据 v2 `ai_tag_reviews` 原始键值关联 `osm_annotations`，同图同对象同类只计一次，跨图可重复。它不是 AI 逐对象检测数量；仅上下文父关系不计入。\n- **占全库影像比例**：覆盖影像数 ÷ {engine.manifest['image_count']:,}。一张影像可包含多个类别，各类占比不要求相加为 100%。\n- 光伏、工业设施等是检索主题；这里统计的是原始 v2 类别。")
        with gr.Tab('样本发现',id='discover') as discovery_tab:
            gr.Markdown('类别丰富按整图可见类别数排序；稀有类别在全库仅有 1–50 张可见影像；疑似与别解保留模型的其他解释。选中影像后可直接打开判读与原始 mask。')
            with gr.Row():
                mode=gr.Radio(['类别丰富','稀有类别','疑似与别解'],value='类别丰富',label='发现方式',scale=2)
                discovery_category=gr.Dropdown(['全部类别']+[r['category'] for r in engine.categories],value='全部类别',label='限定 v2 类别',filterable=True,scale=2)
                discovery_score=gr.Slider(0,100,value=0,step=1,label='候选类别自评分下限',scale=1)
                discover=gr.Button('浏览样本',variant='primary',scale=1)
            discovery_status=gr.Markdown()
            with gr.Row():
                discovery_images=gr.Gallery(label='发现样本（每组最多 12 张）',columns=4,height=None,object_fit='contain',allow_preview=False,scale=3,elem_id='discovery-gallery')
                with gr.Column(scale=1):
                    discovery_choice=gr.Dropdown(choices=[],label='选中样本',filterable=True)
                    discovery_detail=gr.Markdown('选择样本查看判读依据。')
                    discovery_open=gr.Button('打开样本与 mask',variant='primary')
            with gr.Row():
                discovery_previous=gr.Button('上一组',interactive=False)
                discovery_page=gr.Number(value=1,minimum=1,precision=0,label='组号')
                discovery_go=gr.Button('跳转')
                discovery_next=gr.Button('下一组',interactive=False)
                discovery_retry=gr.Button('重试影像加载')
    outputs=[map_html,table,status,state,selection,page,export_html,gallery,gallery_status,preview_panel]
    inputs=[query,verdict,year,score,sort,country,use_bounds,map_bounds,size]
    resolution_outputs=[candidate,resolution,plan,submit]
    resolve_button.click(resolve_input,[query,search_mode],resolution_outputs,api_name='resolve_candidates')
    query.submit(resolve_input,[query,search_mode],resolution_outputs,api_name=False)
    query.input(clear_candidates,outputs=resolution_outputs,api_name=False)
    search_mode.input(clear_candidates,outputs=resolution_outputs,api_name=False).then(lambda m:('全部' if m=='地物名称' else '可见',0),search_mode,[verdict,score],api_name=False)
    candidate.change(plan_selection,[candidate,resolution],plan,api_name=False)
    event=submit.click(apply_candidate,[candidate,resolution,query,search_mode]+inputs[1:],outputs,api_name='apply_candidate')
    gr.Button(visible=False).click(search,inputs,outputs,api_name='search')
    event.success(load_gallery,state,[gallery,gallery_status],api_name=False)
    go.click(paginate,[page,state,size],outputs,api_name='page').success(load_gallery,state,[gallery,gallery_status],api_name=False)
    previous.click(lambda p,s,z:paginate(max(1,p-1),s,z),[page,state,size],outputs,api_name=False).success(load_gallery,state,[gallery,gallery_status],api_name=False)
    next_page.click(lambda p,s,z:paginate(p+1,s,z),[page,state,size],outputs,api_name=False).success(load_gallery,state,[gallery,gallery_status],api_name=False)
    coverage.click(lambda:display_query({'coverage':True}),outputs=outputs,api_name='coverage').success(load_gallery,state,[gallery,gallery_status],api_name=False)
    table.select(select_table,state,selection,api_name=False)
    detail_outputs=[selected_image,info,tag_table,review_table,metadata,zoom_image,object_selection,zoom_info]
    selection.change(detail,[selection,show_overlay,state],detail_outputs,api_name='detail')
    show.click(detail,[selection,show_overlay,state,object_selection],detail_outputs,api_name=False)
    show_overlay.change(detail,[selection,show_overlay,state,object_selection],detail_outputs,api_name=False)
    review_table.select(select_review,selection,selection,api_name=False)
    object_selection.input(detail,[selection,show_overlay,state,object_selection],detail_outputs,api_name='zoom_object')
    previews.click(load_gallery,state,[gallery,gallery_status],api_name='previews')
    map_selection.input(select_map,[map_selection,selection],selection,api_name=False)
    stat_outputs=[stats,stat_plot,stat_summary,stat_category,stats_rows,stat_detail,stat_export]
    for trigger in [stat_apply.click,stat_keyword.submit]:trigger(refresh_stats,[stat_scope,stat_keyword,stat_sort],stat_outputs,api_name=False)
    stat_scope.change(refresh_stats,[stat_scope,stat_keyword,stat_sort],stat_outputs,api_name=False)
    stat_sort.change(refresh_stats,[stat_scope,stat_keyword,stat_sort],stat_outputs,api_name=False)
    statistics_tab.select(refresh_stats,[stat_scope,stat_keyword,stat_sort],stat_outputs,api_name='global_statistics')
    stats.select(statistic_selection,outputs=stat_category,api_name=False)
    stat_category.change(lambda category,rows:chosen_category(category,rows,engine.manifest['image_count']),[stat_category,stats_rows],stat_detail,api_name=False)
    navigation_outputs=outputs+[main_tabs,query,verdict,year,score,sort,country,use_bounds,size]
    stat_open.click(open_statistic,[stat_category,stat_scope],navigation_outputs,api_name='open_category').then(lambda: '类别与标签',outputs=search_mode,api_name=False).then(resolve_input,[query,search_mode],resolution_outputs,api_name=False).success(load_gallery,state,[gallery,gallery_status],api_name=False)
    discovery_outputs=[discovery_images,discovery_state,discovery_status,discovery_choice,discovery_detail,discovery_page,discovery_next,discovery_previous]
    discover.click(lambda m,c,s:discovery(m,c,s),[mode,discovery_category,discovery_score],discovery_outputs,api_name=False).success(discovery_gallery,discovery_state,[discovery_images,discovery_status],api_name=False)
    discovery_tab.select(discovery_tab_open,[mode,discovery_category,discovery_score,discovery_state],discovery_outputs,api_name='discover_tab').success(discovery_gallery,discovery_state,[discovery_images,discovery_status],api_name=False)
    discovery_go.click(discovery,[mode,discovery_category,discovery_score,discovery_page],discovery_outputs,api_name='discover').success(discovery_gallery,discovery_state,[discovery_images,discovery_status],api_name=False)
    discovery_previous.click(lambda m,c,s,p:discovery(m,c,s,max(1,p-1)),[mode,discovery_category,discovery_score,discovery_page],discovery_outputs,api_name=False).success(discovery_gallery,discovery_state,[discovery_images,discovery_status],api_name=False)
    discovery_next.click(lambda m,c,s,p:discovery(m,c,s,p+1),[mode,discovery_category,discovery_score,discovery_page],discovery_outputs,api_name=False).success(discovery_gallery,discovery_state,[discovery_images,discovery_status],api_name=False)
    discovery_retry.click(discovery_gallery,discovery_state,[discovery_images,discovery_status],api_name='discovery_previews')
    discovery_images.select(choose_discovery,discovery_state,discovery_choice,api_name=False)
    discovery_choice.change(describe_discovery,[discovery_choice,discovery_state],discovery_detail,api_name=False)
    discovery_open.click(open_discovery,[discovery_choice,discovery_state],navigation_outputs,api_name='open_discovery').then(lambda: '类别与标签',outputs=search_mode,api_name=False).then(resolve_input,[query,search_mode],resolution_outputs,api_name=False).then(None,None,None,js="() => {setTimeout(() => document.querySelector('#selected-image')?.scrollIntoView({behavior:'smooth',block:'start'}),100);return [];}",api_name=False)
    def gallery_selected(current,evt:gr.SelectData):
        caption=evt.value.get('caption','') if isinstance(evt.value,dict) else ''
        for r in current.get('preview_rows',[]):
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
