# -*- coding: utf-8 -*-
"""Groq 重型通道验收测试（2026-11-22 用户要求）。

目的：评估 Groq 重型模型能否作为事件卡 Gemini 的兜底。
标准：完全复刻 2026-11-20「Gemini 主通道验收」的口径——
  同一 SYS、同一 build_user_prompt、同一 5 卡（iran/fed/ukraine/apec/huang）、
  同样连跑 3 次、temperature=0.15、max_tokens=4000。

顺带核查 GHA secrets 配置（只打印数量，绝不打印明文）：
  GEMINI key 拆分数量 / ZHIPU 有无 / GROQ 有无。

输出：data/_groq_heavy_test.json（完整输出）+ 控制台摘要。
"""
import os, sys, json, re, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from evolve_events import SYS, build_user_prompt, extract_json  # 复用真实链路

GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'
HEAVY_CANDIDATES = [
    'openai/gpt-oss-120b',        # 有 reasoning 能力
    'qwen/qwen3-32b',
    'moonshotai/kimi-k2-instruct',
    'deepseek-r1-distill-llama-70b',
    'llama-3.3-70b-versatile',
]
ROUNDS = 3
ODDS_DEFAULT = {
    'iran': 'Polymarket：解除封锁37% / 通行正常8.5%',
    'fed': 'Polymarket：12月加息25bp约97%；CME 96.8%；美债10Y约4.3%',
    'ukraine': 'Polymarket：停火合约无明确终局定价',
    'apec': 'Polymarket：暂无对应终局合约；亚太概念受会议预期提振',
    'huang': 'Polymarket：暂无对应终局合约；英伟达管理层事件跟踪',
}


def log(*a):
    print('[groq-test]', *a, flush=True)


def groq_key():
    for env in ('GROQ_KEY', 'GROQ_API_KEY'):
        v = (os.environ.get(env, '') or '').strip()
        if v:
            return v
    return ''


def gemini_count():
    """与 llm_pool._gemini_keys 同逻辑（本地复制，避免 import 拉起通道探测）"""
    raw = []
    for env in ('GEMINI_KEY', 'GEMINI_API_KEY', 'GEMINI_API_KEYS'):
        v = os.environ.get(env, '') or ''
        for k in v.split(','):
            k = k.strip()
            if k and k not in raw:
                raw.append(k)
    return len(raw)


def groq_models(key):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request('https://api.groq.com/openai/v1/models',
                                 headers={'Authorization': 'Bearer ' + key, 'User-Agent': 'mp-test'})
    with op.open(req, timeout=25) as r:
        return [m['id'] for m in json.load(r).get('data', [])]


def groq_chat(key, model, msgs, temperature=0.15, max_tokens=4000, timeout=120):
    body = {'model': model, 'messages': msgs,
            'max_tokens': max_tokens, 'temperature': temperature}
    req = urllib.request.Request(GROQ_URL, data=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'User-Agent': 'mp-test'})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    ms = round((time.time() - t0) * 1000)
    msg = (d.get('choices') or [{}])[0].get('message') or {}
    text = msg.get('content') or ''
    usage = d.get('usage') or {}
    # gpt-oss 的推理在 message.reasoning 里，qwen3 可能有 <think>
    reasoning_len = len(msg.get('reasoning') or '')
    return text, ms, usage, reasoning_len


def load_inputs():
    """从线上 raw 读昨日卡状态 + 今日新闻映射（与真实班次同源）"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = 'https://raw.githubusercontent.com/chrisliu1116-y/macropulse-news/main/data/'
    def get(fn):
        with op.open(urllib.request.Request(base + fn, headers={'User-Agent': 'mp-test'}), timeout=30) as r:
            return json.load(r)
    ev = get('events_daily.json')
    try:
        nmap = get('events_news_today.json')
        if isinstance(nmap, dict) and 'items' in nmap:
            nmap = nmap['items']
    except Exception:
        nmap = {}
    return ev, nmap


def main():
    # 0) secrets 核查（只打印数量）
    log('--- GHA secrets 核查（数量级，零明文）---')
    log('GEMINI key 拆分数量 =', gemini_count(), '（期望3；1=仍单token）')
    log('ZHIPU_KEY =', '已配置' if (os.environ.get('ZHIPU_KEY') or '').strip() else '【缺失】')
    log('GROQ key =', '已配置' if groq_key() else '【缺失】')

    key = groq_key()
    if not key:
        log('X 无 GROQ key，测试终止'); return 1

    # 1) 探测可用模型，选重型主候选
    try:
        avail = groq_models(key)
        log('Groq 可用模型', len(avail), '个')
        model = next((m for m in HEAVY_CANDIDATES if m in avail), None)
        if not model:
            log('X 候选重型模型均不可用，可用:', avail[:12]); return 1
        log('选定重型测试模型:', model)
    except urllib.error.HTTPError as e:
        log('X Groq key 无效: HTTP', e.code); return 1

    # 2) 加载与真实班次同源的输入
    ev, nmap = load_inputs()
    cards = ev.get('cards', {})
    log('昨日卡池', list(cards.keys()), '新闻映射卡', list(nmap.keys()) if isinstance(nmap, dict) else '?')

    report = {'generated': time.strftime('%Y-%m-%d %H:%M'), 'model': model,
              'gemini_key_count': gemini_count(), 'runs': []}
    ok_parse = 0; total = 0; lens = []

    for rnd in range(1, ROUNDS + 1):
        log('===== 第 %d 轮 =====' % rnd)
        for cid in ('iran', 'fed', 'ukraine', 'apec', 'huang'):
            prev_card = cards.get(cid) if isinstance(cards.get(cid), dict) else None
            news = nmap.get(cid) or []
            if isinstance(news, dict):
                news = [str(v) for v in news.values()]
            news = [str(x) for x in news][:30]
            yesterday = {k: prev_card.get(k) for k in ('status', 'progress_line', 'summary', 'facts')
                         } if prev_card else {}
            prompt = build_user_prompt(cid, '\n'.join('- ' + n for n in news) or '（今日无关联新闻）',
                                       ODDS_DEFAULT.get(cid, ''), yesterday)
            msgs = [{'role': 'system', 'content': SYS}, {'role': 'user', 'content': prompt}]
            total += 1
            rec = {'round': rnd, 'card': cid, 'model': model}
            try:
                text, ms, usage, rlen = groq_chat(key, model, msgs)
                out = extract_json(text)
                rec['ms'] = ms; rec['reasoning_chars'] = rlen
                rec['usage'] = {k: usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
                if out:
                    ok_parse += 1
                    summ = str(out.get('summary') or '')
                    lens.append(len(summ))
                    first = re.split(r'[。！？]', summ)[0] if summ else ''
                    rec.update({'parse': 'ok', 'summary_len': len(summ), 'first_len': len(first),
                                'status': str(out.get('status') or '')[:20],
                                'summary': summ[:200],
                                'lead': str(out.get('lead') or '')[:80]})
                    log('r%d %s OK %dms 字数=%d 首句=%d status=%s' % (rnd, cid, ms, len(summ), len(first), rec['status']))
                else:
                    rec.update({'parse': 'FAIL', 'raw_head': text[:300]})
                    log('r%d %s JSON解析失败 raw=%s' % (rnd, cid, text[:120]))
            except urllib.error.HTTPError as e:
                rec.update({'parse': 'HTTP%d' % e.code, 'err': str(e)[:120]})
                log('r%d %s HTTP%d' % (rnd, cid, e.code))
            except Exception as e:
                rec.update({'parse': type(e).__name__, 'err': str(e)[:120]})
                log('r%d %s %s' % (rnd, cid, type(e).__name__))
            report['runs'].append(rec)
            time.sleep(8)  # 卡间间隔压 RPM

    report['stats'] = {'total': total, 'parse_ok': ok_parse,
                       'parse_rate': '%.0f%%' % (100.0 * ok_parse / max(1, total)),
                       'summary_lens': lens,
                       'len_ok_40_160': sum(1 for n in lens if 40 <= n <= 160)}
    outp = os.path.join(HERE, '..', 'data', '_groq_heavy_test.json')
    with open(outp, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log('统计:', report['stats'])
    log('完整结果已写', os.path.normpath(outp))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
