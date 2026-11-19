# -*- coding: utf-8 -*-
"""v460：从 CF 边缘 KV 新闻池（/__news）自动生成今日事件新闻映射。

卡片演进(evolve_events.py)读 data/events_news_today.json 作为输入，
之前这份文件是静态的、没人更新 → 卡片内容十几个小时不变。
本脚本在 GHA evolve workflow 第一步运行，从 CF 现成池子拉最近24h新闻，
按每张活跃卡的事件关键词归类，生成映射，让演进永远用最新新闻。

只读 CF KV（GET /__news，不写 KV，不受 429 影响）。
"""
import json, os, sys, re, urllib.request

EDGE_NEWS = 'https://macropulse-api.pages.dev/__news'

# 每个事件卡的匹配关键词（命中任一即归入该卡；可按需扩充）
EVENT_KEYWORDS = {
    'iran': ['伊朗', '霍尔木兹', '以色列', '胡塞', '也门', '沙特', '波斯湾', '原油', 'OPEC'],
    'fed': ['美联储', '鲍威尔', 'FOMC', '议息', '降息', '加息', '非农', 'CPI', 'PCE', '利率', '国债收益率'],
    'ukraine': ['乌克兰', '泽连斯基', '俄罗斯', '俄乌', '普京', '战俘', '停火'],
    'apec': ['APEC', '亚太经合', '习近平', '峰会', '领导人会晤', '经贸', '关税'],
}

def fetch_pool():
    try:
        req = urllib.request.Request(EDGE_NEWS, headers={'User-Agent': 'mp-evolve'})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode())
        return d.get('items', []) or []
    except Exception as e:
        print('[build_news_map] CF池读取失败: %s' % e)
        return []

def main():
    out = sys.argv[1] if len(sys.argv) > 1 else 'data/events_news_today.json'
    items = fetch_pool()
    if not items:
        # CF不可达 → 保留旧文件（不覆盖成空，避免污染卡片）
        print('[build_news_map] CF池为空/不可达，保留现有映射')
        return 0
    result = {k: [] for k in EVENT_KEYWORDS}
    seen = set()   # 跨源重复标题去重（同一条新闻常在多源出现）
    for it in items:
        title = str(it.get('title') or '').strip()
        if not title:
            continue
        tkey = re.sub(r'\s+', '', title)[:30]
        if tkey in seen:
            continue
        seen.add(tkey)
        for ev, kws in EVENT_KEYWORDS.items():
            if any(kw.lower() in title.lower() for kw in kws):
                if len(result[ev]) < 12:  # 每卡最多12条，防 prompt 过长
                    result[ev].append(title)
                break   # 一条新闻只归一个事件，避免重复
    total = sum(len(v) for v in result.values())
    if total == 0:
        print('[build_news_map] 无新闻命中任何事件，保留现有映射')
        return 0
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print('[build_news_map] 已从CF池生成映射: ' + ', '.join('%s=%d' % (k, len(v)) for k, v in result.items()))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
