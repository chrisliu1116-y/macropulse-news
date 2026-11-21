# -*- coding: utf-8 -*-
"""合并 CF __news 池 + 方案A补抓池 → data/news_merged.json（供 build_news_map/radar 读）。

只读，不写 CF KV。标题前20字去重（与 CF ingest 同口径），按 ts 倒序。
补抓条目带 supplement:true 标记，便于审计"哪些是隔夜补投喂"。
"""
import os, sys, json, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CF_URL = 'https://macropulse-api.pages.dev/__news'
SUPP = os.path.join(HERE, '..', 'data', 'news_supplement.json')
OUT = os.path.join(HERE, '..', 'data', 'news_merged.json')


def key_of(t):
    return ''.join(str(t or '').split())[:20]


def load_cf():
    try:
        req = urllib.request.Request(CF_URL, headers={'User-Agent': 'mp-evolve'})
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode()).get('items', []) or []
    except Exception as e:
        print('[merge_news] CF池读取失败:', type(e).__name__, str(e)[:80])
        return []


def load_supp():
    try:
        with open(SUPP, encoding='utf-8') as f:
            return json.load(f).get('items', []) or []
    except Exception as e:
        print('[merge_news] 补抓池读取失败(将只用CF池):', type(e).__name__, str(e)[:80])
        return []


def main():
    cf, supp = load_cf(), load_supp()
    merged, seen = [], set()
    for x in sorted(cf + supp, key=lambda a: -(a.get('ts') or 0)):
        k = key_of(x.get('title'))
        if not k or k in seen:
            continue
        seen.add(k)
        merged.append(x)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump({'generated': __import__('time').strftime('%Y-%m-%d %H:%M:%S'),
                   'cf': len(cf), 'supplement': len(supp), 'merged': len(merged),
                   'items': merged}, f, ensure_ascii=False)
    print('[merge_news] CF池 %d + 补抓 %d → 合并去重 %d 条' % (len(cf), len(supp), len(merged)))
    print('[merge_news] wrote', OUT)


if __name__ == '__main__':
    main()
