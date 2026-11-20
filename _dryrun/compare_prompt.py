# -*- coding: utf-8 -*-
"""本地dry-run: 用黄仁勋真实新闻对比新旧提示词摘要(仅本机,不写生产)"""
import os,sys,json,urllib.request,re,time
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','tools'))
import llm_pool
# 1) 拉CF池真实新闻(非东财)
d=json.loads(urllib.request.urlopen(urllib.request.Request('https://macropulse-api.pages.dev/__news?full=1',headers={'User-Agent':'mp'}),timeout=25).read().decode())
items=d.get('items',[])
cutoff=(time.time()-24*3600)*1000
seen=set();news=[]
for it in items:
    t=str(it.get('title') or '')
    if it.get('level') not in ('S','A'):continue
    ts=it.get('ts') or 0
    if ts and ts<cutoff:continue
    k=re.sub(r'\s+','',t)[:30]
    if k in seen:continue
    seen.add(k)
    if any(x in t for x in ['黄仁勋','英伟达','NVIDIA','Jensen','CEO接任','董事会','人才流失','创办新公司']):
        news.append((ts,t))
news.sort(key=lambda x:x[0],reverse=True)
facts='\n'.join('- '+t for _,t in news[:30])
print('=== 真实新闻(倒序,前12条) ===')
for ts,t in news[:12]:
    print(' ',time.strftime('%H:%M',time.localtime(ts/1000)),'|',t[:75])
print('总条数:',len(news))
# 2) 取新旧SYS
new_src=open(os.path.join(os.path.dirname(__file__),'..','tools','evolve_events.py'),encoding='utf-8').read()
new_sys=new_src.split('SYS = """')[1].split('"""')[0]
import subprocess
old_src=subprocess.check_output(['git','show','HEAD:tools/evolve_events.py'],cwd=os.path.join(os.path.dirname(__file__),'..'),text=True)
old_sys=old_src.split('SYS = """')[1].split('"""')[0]
print('旧提示词长度',len(old_sys),'| 新提示词长度',len(new_sys))
# 3) 构造prompt(用当前卡scope+昨日状态)
scope='黄仁勋相关调查进展、其后续动向与英伟达CEO接任安排'
yesterday='跟踪黄仁勋相关调查后续、本人动向与英伟达CEO接任安排，等待权威媒体与官方声明确认实质进展。'
def build(sys):
    return sys+'\n\n【本卡】core_subject='+scope+'\n昨日卡片：'+yesterday+'\n今日关联新闻（标题列表，数字已脱敏）：\n'+facts+'\n\n只输出JSON。'
# 4) 各跑一次(智谱重型)
for label,sys in [('修改前',old_sys),('修改后',new_sys)]:
    msgs=[{'role':'user','content':build(sys)}]
    t=time.time()
    try:
        txt,ch=llm_pool._chat_heavy_once(msgs,max_tokens=4000,timeout=100,verbose=False)
        print('\n===',label,'| channel:',ch,'| %.1fs'%(time.time()-t),'===')
        m=re.search(r'\{[\s\S]*\}',txt)
        if m:
            o=json.loads(m.group())
            print('status:',o.get('status'))
            print('lead:',o.get('lead'))
            print('rest:',o.get('rest'))
            print('facts:',o.get('facts'))
            print('noisy_dropped:',o.get('noisy_dropped'))
            print('summary长度:',len(str(o.get('lead',''))+str(o.get('rest',''))))
        else:print('RAW:',txt[:500])
    except Exception as e:print(label,'ERR',str(e)[:150])
