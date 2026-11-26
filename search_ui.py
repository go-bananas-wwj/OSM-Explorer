"""Readable, grounded query plans for the two-stage search UI."""
import html
from resolver import KIND_LABEL

def candidate_label(row):
    kind=KIND_LABEL[row['kind']]
    place=''
    if row['kind']=='name':
        countries=','.join(sorted(set(c for group in row.get('countries','').split(';') for c in group.split(',') if c)))
        place=' · '+countries if countries else ''
        place+=' · '+row.get('entity',row['token'][5:])
    return f"{kind} · {row['label']}{place} · {row['images']:,} 图 / 关联可见 {row['visible_images']:,} 图"

def candidate_plan(row,engine):
    if not row:return '输入检索词并查找候选；确认原始标签后再筛选。'
    esc=html.escape;kind=row['kind']
    text=f"**{KIND_LABEL[kind]}：{esc(row['label'])}**\n\n{esc(row['reason'])}\n\n全库覆盖 **{row['images']:,} 张影像**，其中 **{row['visible_images']:,} 张**有关联的 v2 可见判读。"
    if kind=='category':text+='\n\n精确筛选 v2 类别：`'+esc(row['label'])+'`。'
    elif kind=='tag':text+='\n\n精确匹配原始键值：`'+esc(row['label'])+'`，叠加包含此键值的原始标注。'
    elif kind=='theme':
        from engine import THEMES
        catalog=engine.label_catalog();available=[]
        for term in THEMES[row['label']]:
            for item in catalog:
                if item['kind']=='tag' and ('=' in term and ((term.endswith('=') and item.get('key')==term[:-1]) or item['label']==term)):
                    available.append(item['label'])
                elif item['kind']=='category' and item['label']==term:available.append('v2 类别：'+term)
        available=sorted(set(available));text+='\n\n匹配依据（任一满足）：'+', '.join('`'+esc(t)+'`' for t in available[:20])
        if len(available)>20:text+=f' 等 {len(available)} 项原始标签/类别。'
    else:
        text+='\n\n对象：`'+esc(row['token'][5:])+'`；匹配字段：'+', '.join('`'+esc(t)+'`' for t in row.get('matched_fields',[]))
        text+='\n\n同一对象的各语言和历史名称聚合检索；名称来自 OSM 元数据，分数仅来自其关联的 v2 图像级类别判读。无关联判读时分数留空。'
    return text
