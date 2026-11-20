# -*- coding: utf-8 -*-
"""注入用户截图里的15:41核心进展,对比新旧提示词谁抓住主线"""
import os,sys,json,re,time,subprocess
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','tools'))
import llm_pool
new_src=open(os.path.join(os.path.dirname(__file__),'..','tools','evolve_events.py'),encoding='utf-8').read()
new_sys=new_src.split('SYS = """')[1].split('"""')[0]
old_src=subprocess.check_output(['git','show','HEAD:tools/evolve_events.py'],cwd=os.path.join(os.path.dirname(__file__),'..'),text=True)
old_sys=old_src.split('SYS = """')[1].split('"""')[0]
scope='黄仁勋相关调查进展、其后续动向与英伟达CEO接任安排'
y='调查僵持：英伟达股价转跌，核心人才流失风险浮现。董事会正推进CEO接任流程。'
# 按时间倒序:15:41核心进展在最前;后面全是同公司常规新闻(用户抱怨的噪声)
news=[
 '- 黄仁勋据称将创办新的半导体公司 或将筹资500亿用于创业',
 '- 英伟达与微软将加速计算从Azure扩展至Windows PC',
 '- 英伟达接近以逾5亿美元收购一家扩散式AI开发商Inception',
 '- “白毛股神”爆料：黄仁勋若不回归 英伟达恐爆发核心人才流失潮',
 '- 特朗普9月调仓披露：买入英伟达、Palantir、SpaceX',
]
def build(sys):
    return sys+'\n\n【本卡】core_subject='+scope+'\n昨日卡片：'+y+'\n今日关联新闻（标题列表，时间倒序）：\n'+'\n'.join(news)+'\n\n只输出JSON。'
for label,sys in [('修改前',old_sys),('修改后',new_sys)]:
    t=time.time()
    try:
        txt,ch=llm_pool._chat_heavy_once([{'role':'user','content':build(sys)}],max_tokens=4000,timeout=100,verbose=False)
        m=re.search(r'\{[\s\S]*\}',txt);o=json.loads(m.group()) if m else {}
        print('\n===',label,'|',ch,'| %.1fs'%(time.time()-t),'===')
        print('lead:',o.get('lead'))
        print('rest:',o.get('rest'))
        print('facts:',o.get('facts'))
        print('noisy_dropped:',o.get('noisy_dropped'))
        s=str(o.get('lead',''))+str(o.get('rest',''))
        print('抓到500亿核心:', ('500亿' in s or '创办' in s or '新公司' in s),'| 塞入微软/Inception:',('微软' in s or 'Inception' in s))
    except Exception as e:print(label,'ERR',str(e)[:150])
