# -*- coding: utf-8 -*-
"""v501【快讯分级2.0影子验证·GHA·只读】
从线上 /__news?full=1 读2000条24h内原始标题(只读KV,不写),用2.0提示词全量分级,
输出分级分布/S-A样本/提示词行为观察报告。不部署、不上线、不碰生产。
"""
import os, json, time, urllib.request, sys
T0=time.time()
def tag(*a):print('[SHADOW]',*a,flush=True)

NEWS_URL='https://macropulse-api.pages.dev/__news?full=1'
RULES='prompt_rules.md'   # workflow checkout后在工作区根

# 1) 读线上新闻池(只读)
tag('读线上新闻池...')
try:
    req=urllib.request.Request(NEWS_URL,headers={'User-Agent':'shadow-v2'})
    with urllib.request.urlopen(req,timeout=30) as r:d=json.loads(r.read().decode())
    items=d.get('items',[])
except Exception as e:
    tag('读取失败',str(e)[:150]);sys.exit(1)
tag('读到',len(items),'条 | generated:',d.get('generated'))

# 只要标题+id,按时间倒序取最近N条
items=[x for x in items if x.get('title')]
items.sort(key=lambda x:x.get('ts') or 0,reverse=True)
LIMIT=int(os.environ.get('SHADOW_LIMIT','600'))  # 默认600条(约20-30批)
items=items[:LIMIT]
tag('影子分级样本',len(items),'条(上限%d)'%LIMIT)

rules=open(RULES,encoding='utf-8').read()
tag('提示词',len(rules),'字符')

# 2) 智谱分级(2.0提示词),每批20条
ZK=os.environ.get('ZHIPU_KEY','')
if not ZK:tag('无ZHIPU_KEY');sys.exit(1)
URL='https://open.bigmodel.cn/api/paas/v4/chat/completions'

def grade_batch(batch):
    user=json.dumps([{'id':i,'title':x['title'],'brief':''} for i,x in enumerate(batch)],ensure_ascii=False)
    body=json.dumps({'model':'glm-4-flash-250414',
      'messages':[{'role':'system','content':rules},{'role':'user','content':user}],
      'max_tokens':2000,'thinking':{'type':'disabled'}}).encode()
    req=urllib.request.Request(URL,data=body,headers={'Authorization':'Bearer '+ZK,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=90) as r:resp=json.loads(r.read().decode())
    content=((resp.get('choices') or [{}])[0].get('message') or {}).get('content','')
    # 兼容裸对象/围栏
    import re
    t=content.replace('```json','').replace('```','').strip()
    m=re.search(r'\[[\s\S]*\]',t)
    if not m:
        m2=re.search(r'\{[\s\S]*\}',t)
        if m2:return [json.loads(m2.group())]
        return []
    return json.loads(m.group())

results=[]
BATCH=20
for st in range(0,len(items),BATCH):
    part=items[st:st+BATCH]
    for attempt in range(3):
        try:
            out=grade_batch(part)
            for g in out:
                i=g.get('id')
                if isinstance(i,int) and 0<=i<len(part):
                    import datetime as _dt
                    _ts=part[i].get('ts') or 0
                    _tm=_dt.datetime.fromtimestamp(_ts/1000,_dt.timezone(_dt.timedelta(hours=8))).strftime('%m-%d %H:%M') if _ts else ''
                    results.append({'id':part[i].get('id'),'title':part[i]['title'],'ts':_ts,'time':_tm,
                      'level':g.get('level'),'s_code':g.get('s_code'),'is_dup':g.get('is_dup'),
                      'dup_of':g.get('dup_of'),'reason':str(g.get('reason',''))[:80],
                      'title_prefix':str(g.get('title_prefix',''))[:40]})
            break
        except Exception as e:
            tag('批',st,'失败',attempt,str(e)[:80]);time.sleep(3)
    tag('进度 %d/%d'%(min(st+BATCH,len(items)),len(items)),'用时%.0fs'%(time.time()-T0))
    time.sleep(0.3)

# 3) 报告
from collections import Counter
dist=Counter(x['level'] for x in results)
tag('=== 分级分布 ===',dict(dist))
S=[x for x in results if x['level']=='S']
A=[x for x in results if x['level']=='A']
tag('S级',len(S),'条 A级',len(A),'条')
tag('=== S级样本(前10) ===')
for x in S[:10]:tag('  ',x['level'],x.get('s_code'),x['title'][:50])
tag('=== A级样本(前10) ===')
for x in A[:10]:tag('  ',x['level'],x['title'][:50])
dups=sum(1 for x in results if x.get('is_dup'))
tag('语义去重标记 is_dup=true:',dups,'条 /',len(results))
tag('=== 完成 用时%.0fs ==='%(time.time()-T0))

report={'generated':time.strftime('%Y-%m-%d %H:%M:%S'),'source':NEWS_URL,
  'sample_n':len(items),'graded_n':len(results),'dist':dict(dist),
  'S':S[:30],'A':A[:50],'is_dup_count':dups,'elapsed_s':round(time.time()-T0,1),
  'all_results':results}   # v502: 全量明细(标题/级别/s_code/is_dup/原因)
# 去重后独立事件统计(回答"S级16条是否真有16个事件")
_indep={}
for x in results:
    if x.get('is_dup'):continue
    lv=x.get('level');_indep[lv]=_indep.get(lv,0)+1
report['dist_independent']=_indep
tag('=== 去重后独立事件 ===',dict(_indep))
with open('shadow_v2_report.json','w',encoding='utf-8') as f:
    json.dump(report,f,ensure_ascii=False,indent=2)
tag('报告已写 shadow_v2_report.json')
