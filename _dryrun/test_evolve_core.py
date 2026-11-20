# -*- coding: utf-8 -*-
"""走真实evolve()路径验证:15:41核心进展进lead,常规新闻在输入层就被剔除"""
import os,sys,json
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','tools'))
import evolve_events as E
news=[
 '黄仁勋据称将创办新的半导体公司 或将筹资500亿用于创业',
 '英伟达与微软将加速计算从Azure扩展至Windows PC',
 '英伟达接近以逾5亿美元收购一家扩散式AI开发商Inception',
 '“白毛股神”爆料：黄仁勋若不回归 英伟达恐爆发核心人才流失潮',
 '特朗普9月调仓披露：买入英伟达、Palantir、SpaceX',
]
prev=json.load(open('data/events_daily.json',encoding='utf-8'))['cards']['huang']
out=E.evolve('huang',news,'Polymarket：暂无对应终局合约；英伟达管理层事件跟踪',prev.get('summary',''),True,prev)
print('status:',out.get('status'))
print('summary:',out.get('summary'))
print('facts:',out.get('facts'))
print('noisy_dropped:',out.get('noisy_dropped'))
print('channel:',out.get('model_channel'))
