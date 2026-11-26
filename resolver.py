"""Resolve user words to actual indexed candidates, showing the matching basis."""
import difflib,re
from multilingual import normalize,fold_name,concept_matches,CONCEPTS,is_name_key
KIND_LABEL={'theme':'检索主题','category':'v2 类别','tag':'OSM 标签','name':'地物名称'}

class Resolver:
    def __init__(self,engine):self.engine=engine
    def labels(self,text,limit=30):
        q=normalize(text);folded=fold_name(text)
        if not q:return []
        catalog=self.engine.label_catalog();by_token={r['token']:r for r in catalog};found={}
        def offer(token,rank,reason):
            if token not in by_token:return
            if token not in found or found[token]['rank']<rank:
                found[token]={**by_token[token],'rank':rank,'reason':reason}
        if q.startswith('v2:'):
            for r in catalog:
                if r['kind']=='category' and normalize(r['label'])==normalize(text[3:].strip()):offer(r['token'],120,'v2 原始类别精确匹配')
            return sorted(found.values(),key=lambda r:r['token'])[:limit]
        if '=' in text:
            key,value=text.strip().split('=',1)
            for r in catalog:
                if value and normalize(r['label'])==q:
                    offer(r['token'],120 if r['kind']=='tag' else 115,'原始 key=value 匹配' if r['kind']=='tag' else '同名 v2 类别')
                elif not value and r['kind']=='tag' and normalize(r.get('key',''))==normalize(key):offer(r['token'],100,'该键在索引中的实际取值')
            return sorted(found.values(),key=lambda r:(-r['rank'],-r['visible_images'],r['token']))[:limit]
        concepts=concept_matches(text)
        for concept in concepts:
            for i,token in enumerate(concept['targets']):offer(token,concept['rank']-i*.1,'多语言词汇：'+concept['match']+' → '+concept['label'])
        for r in catalog:
            label=normalize(r['label']);plain=fold_name(r['label'].replace('_',' ').replace('=',' '))
            if q==label:offer(r['token'],110,'原始标签或类别名称精确匹配')
            elif not concepts and len(q)>=2:
                words=fold_name(text.replace('_',' '))
                match=bool(re.search(r'(?<!\w)'+re.escape(words)+r'(?!\w)',plain)) if words.isascii() else words in plain
                if match:offer(r['token'],65,'原始名称文本匹配')
        if not found and len(folded)>=4:
            aliases={fold_name(a):c for c in CONCEPTS for a in [c[0]]+c[2]}
            for approximate in difflib.get_close_matches(folded,list(aliases),n=3,cutoff=.84):
                label,targets,_=aliases[approximate]
                for i,token in enumerate(targets):offer(token,50-i*.1,'近似拼写：'+approximate+' → '+label)
        return sorted(found.values(),key=lambda r:(-r['rank'],-r['visible_images'],-r['images'],r['token']))[:limit]

    def names(self,text,limit=30):
        if not self.engine.has_names:return []
        field=None;q=text.strip()
        if '=' in q:
            key,value=q.split('=',1)
            if is_name_key(key):field=key;q=value
        folded=fold_name(q)
        if len(folded)<2:return []
        if re.fullmatch(r'(node|way|relation)/[0-9]+',q):
            result=self.engine.rows('SELECT entity,images,visible_images,countries,first_year,last_year FROM name_entities WHERE entity=?',[q])
            return [dict(token='name:'+r['entity'],kind='name',label=r['entity'],images=r['images'],visible_images=r['visible_images'],reason='原始 OSM 对象 ID 精确匹配',matched_fields=[],countries=r['countries'] or '',first_year=r['first_year'],last_year=r['last_year'],rank=120) for r in result]
        where='contains(a.folded,?)';params=[folded]
        if field:where+=' AND a.field=?';params.append(field)
        # First rank aliases, then collapse language variants by the same OSM entity.
        sql=f'''WITH matched AS (
          SELECT a.*,CASE WHEN a.folded=? THEN 0 WHEN starts_with(a.folded,?) THEN 1 ELSE 2 END quality
          FROM name_aliases a WHERE {where}),
          best AS (SELECT *,row_number() OVER(PARTITION BY entity ORDER BY quality,length(value),field,value) rn FROM matched)
          SELECT b.entity,b.field,b.value,b.quality,e.images,e.visible_images,e.countries,e.first_year,e.last_year FROM best b JOIN name_entities e USING(entity)
          WHERE rn=1 ORDER BY quality,visible_images DESC,images DESC,entity LIMIT ?'''
        rows=self.engine.rows(sql,[folded,folded]+params+[limit])
        return [dict(token='name:'+r['entity'],kind='name',label=r['value'],images=r['images'],visible_images=r['visible_images'],reason=('名称精确匹配' if r['quality']==0 else '名称文本匹配')+' · '+r['field'],matched_fields=[r['field']+'='+r['value']],entity=r['entity'],countries=r['countries'] or '',first_year=r['first_year'],last_year=r['last_year'],rank=100-r['quality']) for r in rows]

    def resolve(self,text,mode):
        return self.names(text) if mode=='地物名称' else self.labels(text)
