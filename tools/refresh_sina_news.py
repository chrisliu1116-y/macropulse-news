# -*- coding: utf-8 -*-
"""【灰度试点·新浪财经】边缘新闻分级产物生成器（GHA 云端跑，非本机）。

链路（用户 2026-11-18 拍板的 3 条铁律）：
  ① 本脚本在 GHA 云端异步跑：抓新浪 → 10条微批 LLM 分级去重 → 写 CF KV。
     前端只读 KV 成品（毫秒级），绝不等待 LLM 推理。
  ② 前端读失败/空 → 自动回退原浏览器直连 JSONP + 本地规则（不断流）。
  ③ 无新增短路：若本轮新浪 feed 与上次完全相同（id 集合一致），跳过 LLM 省额度。

数据源：新浪财经 7×24（zhibo.sina.com.cn，JSONP）—— 非东财，GHA 云端抓。
KV key：mp_news_sina；value：{generated, items:[{id,ts,title,brief,src,level,category,reason}]}

用法：
  python3 tools/refresh_sina_news.py            # 抓+分级+写KV
  python3 tools/refresh_sina_news.py --dry-run  # 只抓+分级，不写KV
"""
import os, re, json, time, sys, argparse, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SINA_URL = ('https://zhibo.sina.com.cn/api/zhibo/feed'
            '?page=1&page_size=30&zhibo_id=152&tag_id=0&dire=f&dpc=1')
KV_KEY = 'mp_news_sina'
BATCH = 10

def fetch_sina(timeout=15):
    """抓新浪 JSONP，返回 [{id,ts,title,brief,src}]。"""
    req = urllib.request.Request(SINA_URL, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36',
        'Referer': 'https://finance.sina.com.cn/7x24/'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode('utf-8', 'replace')
    # 剥 JSONP 外壳 callback(...)
    m = re.search(r'\(\s*(\{[\s\S]*\})\s*\)', raw)
    data = json.loads(m.group(1) if m else raw)
    feed = (((data or {}).get('result') or {}).get('data') or {}).get('feed') or {}
    items = feed.get('list') or []
    out = []
    for it in items:
        text = re.sub(r'<[^>]+>', '', (it.get('rich_text') or it.get('content') or '')).strip()
        if not text: continue
        mm = re.match(r'^【(.+?)】\s*([\s\S]*)$', text)
        title = (mm.group(1).strip() if mm else text[:60])
        brief = (mm.group(2).strip() if mm else '')
        ts_raw = it.get('create_time') or ''
        ts = _parse_sina_ts(ts_raw)
        out.append({'id': 'sn-' + str(it.get('id') or ''), 'ts': ts,
                    'title': title, 'brief': brief, 'src': '新浪财经'})
    return out

def _parse_sina_ts(s):
    """新浪时间: '2026-11-18 21:30:00' 或 '11-18 21:30'。"""
    import datetime
    s = str(s).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%m-%d %H:%M'):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            if fmt.startswith('%m'):
                dt = dt.replace(year=datetime.datetime.now().year)
            return int(dt.timestamp() * 1000)
        except Exception:
            continue
    return int(time.time() * 1000)

def grade(items, verbose=True):
    """复用 llm_news_pipeline 的微批分级。返回带 level/category 的 items。"""
    from llm_news_pipeline import grade_batch
    out = []
    for start in range(0, len(items), BATCH):
        chunk = items[start:start + BATCH]
        batch = [{'idx': k, 'title': it['title'], 'brief': it.get('brief', ''), 'src': it['src']}
                 for k, it in enumerate(chunk)]
        rs, ch, _ = grade_batch(batch, verbose=verbose)
        for k, it in enumerate(chunk):
            r = rs[k] if k < len(rs) else {}
            it2 = dict(it)
            it2['level'] = r.get('level', 'B')
            it2['category'] = r.get('category', '快讯')
            it2['reason'] = r.get('reason', '')
            it2['is_dup'] = bool(r.get('is_dup'))
            out.append(it2)
    return out

def load_kv(token, account, ns):
    url = ('https://api.cloudflare.com/client/v4/accounts/%s/storage/kv/namespaces/%s/values/%s'
           % (account, ns, KV_KEY))
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token, 'User-Agent': 'mp'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

def write_kv(token, account, ns, payload):
    url = ('https://api.cloudflare.com/client/v4/accounts/%s/storage/kv/namespaces/%s/values/%s'
           % (account, ns, KV_KEY))
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), method='PUT',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'User-Agent': 'mp'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    items = fetch_sina()
    print('[sina] 抓到 %d 条' % len(items))
    if not items:
        print('[sina] 空 feed，跳过'); return 0
    # 无新增短路：与上次 KV 的 id 集合一致 → 跳过 LLM
    tok = os.environ.get('CF_TOKEN', ''); acc = os.environ.get('CF_ACCOUNT_ID', '')
    ns = os.environ.get('CF_KV_NS', '2b7a91bb9ec945ebafbc41393c6fd8f8')
    old = None
    if tok and acc:
        old = load_kv(tok, acc, ns)
    if old and old.get('items'):
        old_ids = {x['id'] for x in old['items']}
        new_ids = {x['id'] for x in items}
        if new_ids.issubset(old_ids):
            print('[sina] 无新增(全部已分级)，跳过 LLM 省额度'); return 0
    graded = grade(items)
    payload = {'generated': time.strftime('%Y-%m-%d %H:%M:%S'), 'src': '新浪财经',
               'items': graded}
    print('[sina] 分级完成: %s' % {lv: sum(1 for x in graded if x['level'] == lv) for lv in 'SABC'})
    # 写静态产物：Pages 网关把它作为 news.json 部署（前端毫秒级读 CDN，零 KV binding 依赖）
    out_json = os.environ.get('SINA_OUT', os.path.join(HERE, 'news_sina.json'))
    open(out_json, 'w', encoding='utf-8').write(json.dumps(payload, ensure_ascii=False))
    print('[sina] 静态产物已写 %s' % out_json)
    if a.dry_run:
        print('[dry-run] 未写 KV'); return 0
    if not (tok and acc):
        print('[warn] 缺 CF 凭据，未写 KV'); return 0
    write_kv(tok, acc, ns, payload)
    print('[sina] 已写 CF KV %s (%d 条)' % (KV_KEY, len(graded)))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
