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

# 兜底关键词（卡片没带 match_keywords 时才用，保证老卡/自动卡也有基本归类）
FALLBACK_KEYWORDS = {
    'iran': ['伊朗', '霍尔木兹', '以色列', '胡塞', '也门', '沙特', '波斯湾', '原油', 'OPEC'],
    'fed': ['美联储', '鲍威尔', 'FOMC', '议息', '降息', '加息', '非农', 'CPI', 'PCE', '利率', '国债收益率'],
    'ukraine': ['乌克兰', '泽连斯基', '俄罗斯', '俄乌', '普京', '战俘', '停火'],
    'apec': ['APEC', '亚太经合', '习近平', '峰会', '领导人会晤', '经贸', '关税'],
    'huang': ['黄仁勋', '英伟达', 'NVIDIA', 'Jensen Huang', 'CEO接任'],
}

def load_keywords(events_path):
    """v462【隐患4】动态读所有活跃卡自带的 match_keywords；没带的用兜底词表。
    这样自动建的新卡只要建卡时写了 match_keywords，第二天就能收到新快讯。"""
    kws = {}
    try:
        d = json.load(open(events_path, encoding='utf-8'))
        cards = d.get('cards', {})
    except Exception as e:
        print('[build_news_map] 读卡片词表失败: %s' % e)
        cards = {}
    all_ids = list(cards.keys()) or list(FALLBACK_KEYWORDS.keys())
    for cid in all_ids:
        c = cards.get(cid, {}) if isinstance(cards, dict) else {}
        # 自动建卡时 LLM 已生成 newsKeys（高区分度关键词），优先用它；兼容 match_keywords
        own = (c.get('match_keywords') or c.get('newsKeys')) if isinstance(c, dict) else None
        if isinstance(own, list) and own:
            kws[cid] = [str(x) for x in own][:20]
        elif cid in FALLBACK_KEYWORDS:
            kws[cid] = FALLBACK_KEYWORDS[cid]
    return kws or FALLBACK_KEYWORDS

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
    events_path = sys.argv[2] if len(sys.argv) > 2 else 'data/events_daily.json'
    EVENT_KEYWORDS = load_keywords(events_path)
    print('[build_news_map] 事件词表: ' + ', '.join('%s=%d词' % (k, len(v)) for k, v in EVENT_KEYWORDS.items()))
    items = fetch_pool()
    if not items:
        # CF不可达 → 保留旧文件（不覆盖成空，避免污染卡片）
        print('[build_news_map] CF池为空/不可达，保留现有映射')
        return 0
    # v502【输入硬倒序】每卡最多12条,若不显式排序,池顺序一变最新主线进展就会被截断。
    items = sorted(items, key=lambda x: x.get('ts') or 0, reverse=True)
    # v501【顺手导出建卡器输入】event_incubator 期望 {"results":[{title,level,card_id,is_dup,ts,title_idx}]}
    # 之前它读的 daily_news_graded.json 是已下线的全量贴标产物,链路断裂导致永远空输入。
    # 这里用同一份 CF 池条目直接生成,避免重复拉取。只取 S/A + 过去24h + 轻量标题去重。
    try:
        import time as _t, re as _re
        cutoff = (_t.time() - 24 * 3600) * 1000
        seen = set(); results = []
        for i, it in enumerate(items):
            if it.get('level') not in ('S', 'A'):
                continue
            ts = it.get('ts') or 0
            if ts and ts < cutoff:
                continue
            title = str(it.get('title') or '').strip()
            if not title:
                continue
            tkey = _re.sub(r'\s+', '', title)[:30]
            if tkey in seen:
                continue
            seen.add(tkey)
            results.append({'title': title, 'level': it['level'],
                            'card_id': 'none', 'is_dup': False,
                            'ts': ts, 'title_idx': 'cf_%s' % str(it.get('id') or i)[:20]})
        # v502: 按 ts 倒序(最新在前),确保 evolve_events.py 的 news[:30] 截断不会漏掉最新核心进展
        results.sort(key=lambda x: x.get('ts') or 0, reverse=True)
        graded_path = 'data/daily_news_graded.json'
        os.makedirs(os.path.dirname(graded_path) or '.', exist_ok=True)
        with open(graded_path, 'w', encoding='utf-8') as f:
            json.dump({'results': results, 'sample': results[:20],
                       'gradedAt': _t.strftime('%Y-%m-%d %H:%M:%S')}, f, ensure_ascii=False)
        print('[build_news_map] 已导出建卡输入: %d 条 S/A 到 %s' % (len(results), graded_path))
    except Exception as e:
        print('[build_news_map] 建卡输入导出失败(不影响映射): %s' % e)
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
