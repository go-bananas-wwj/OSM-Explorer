"""Presentation helpers for explicitly defined v2 global statistics."""
import html
import pandas as pd

STAT_HEADERS=['v2 类别','AI 判读条数','图内 OSM 对象关联条数','覆盖影像数','占全库影像 %']
SCOPE={'全部判读':'all','可见':'visible','疑似':'possible','未辨认':'unrecognized'}
SORT={'覆盖影像数':'images','对象关联条数':'objects','AI 判读条数':'records','类别名称':'category'}

def statistic_values(rows):
    return [[r['category'],r['records'],r['object_rows'],r['images'],round(r['image_pct'],4)] for r in rows]

def statistics_chart(rows):
    top=sorted(rows,key=lambda r:(-r['images'],r['category']))[:15]
    frame=pd.DataFrame(statistic_values(top),columns=STAT_HEADERS)
    for column in STAT_HEADERS[1:]:frame[column]=pd.to_numeric(frame[column],errors='coerce').astype('float64')
    return frame

def metrics(items):
    return '<div class="global-metrics">'+''.join('<div><span>'+html.escape(label)+'</span><strong>'+html.escape(str(value))+'</strong></div>' for label,value in items)+'</div>'

def overview(engine):
    _,all_stats=engine.stats('all');_,visible=engine.stats('visible')
    return metrics([('全库影像',f"{engine.manifest['image_count']:,}"),('v2 全部类别',f"{all_stats['categories']:,}"),('AI 判读记录',f"{all_stats['records']:,}"),('有判读的影像',f"{all_stats['images']:,}"),('至少一类可见的影像',f"{visible['images']:,}"),('图内 OSM 对象标注',f"{engine.manifest.get('osm_annotation_rows',0):,}")])

def chosen_category(category,rows,total):
    r=next((r for r in rows if r['category']==category),None)
    if not r:return '没有匹配的类别。'
    return (f"### {html.escape(category)}\n\n"
            f"**{r['records']:,} 条 AI 判读** · **{r['images']:,} 张影像**\n\n"
            f"覆盖全库 **{r['image_pct']:.4f}%**（{r['images']:,} / {total:,}）\n\n"
            + (f"对应 **{r['object_rows']:,} 条图内 OSM 对象关联**。" if r['object_rows'] is not None else '对象关联计数尚未生成。'))

def discovery_caption(row,mode):
    criterion=f"{row['metric']} 类可见" if mode=='类别丰富' else f"全库 {row['metric']} 张可见" if mode=='稀有类别' else '疑似 · 有别解'
    return f"{row['category']} · {row['score']} 分 · {criterion} · {row['image_id']}"

def discovery_description(row,mode):
    if not row:return '选择一张影像查看判读依据，再点击“打开样本与 mask”。'
    label={'visible':'可见','possible':'疑似','unrecognized':'未辨认'}[row['verdict']]
    reason=(f"整张影像共有 **{row['metric']} 个可见类别**。" if mode=='类别丰富' else f"该类别在全库仅 **{row['metric']} 张影像**中判为可见。" if mode=='稀有类别' else '模型给出了疑似判读及其他解释。')
    return f"**{html.escape(row['category'])}** · {label} · 自评分 **{row['score']}**\n\n{reason}\n\n{html.escape(row['explanation'] or '')}\n\n别解：{html.escape(row['alternative'] or '无')}\n\n`{row['image_id']}` · {row['year']} 年 · {row['lat']:.4f}, {row['lon']:.4f}"
