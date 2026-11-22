# -*- coding: utf-8 -*-
"""Phase 3：Polymarket 候选 → 前端审批队列 + 确认后 Gemini 补全三段式

红线（用户 2026-11-21 拍板）：
  · 未确认前：只写 auto_ 骨架卡(summary 用标题占位)，绝不调 Gemini
  · 只在 CF /__card-confirm 回执里的 cid，才调 llm_pool.chat_heavy(含Gemini)生成三段式
  · 活跃卡 >5 时按 heat 末位淘汰归档（无保留例外）

流程：
  1. 读 polymarket_radar_shadow.json 的 genuine_new（净新增候选）
  2. 读 CF /__card-confirm 回执（前端点「加入」才写入）
  3. 骨架入队：genuine_new 里不在 events_daily.json 的 → 写 auto_ 骨架卡
  4. 确认补全：回执里已确认且是 auto_ 骨架的 → 调 Gemini 生成 lead/rest/summary/progress_line
  5. 末位淘汰：活跃卡>5 → 归档最冷门
  6. 写回 events_daily.json（绝不写 insum-cloud 的卡池）
"""
import os, sys, json, time, re, argparse, urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
CONFIRM_URL = 'https://macropulse-api.pages.dev/__card-confirm'
MAX_ACTIVE = 5


def log(*a):
    print('[radar_publish]', *a, flush=True)


def bjnow():
    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')


def get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'mp-evolve'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def read_confirms():
    """读 CF 确认回执（前端点加入才写）。返回 {cid: {name,...}}。失败返回空 → 零 Gemini 调用。"""
    try:
        d = get_json(CONFIRM_URL)
        return {str(x.get('cid')): x for x in (d.get('items') or []) if x and x.get('cid')}
    except Exception as e:
        log('读回执失败:', type(e).__name__, '→ 本次零确认、零 Gemini')
        return {}

# v538【末位淘汰APP确认】CF 淘汰确认回执端点（用户在APP确认归档）
ARCHIVE_CONFIRM_URL = 'https://macropulse-api.pages.dev/__card-archive-confirm'


def read_archive_confirms():
    """读 APP 淘汰确认回执：返回 {cid: 回执}。空=不归档任何卡。"""
    try:
        d = get_json(ARCHIVE_CONFIRM_URL)
        return {str(x.get('cid')): x for x in (d.get('items') or []) if x and x.get('cid')}
    except Exception as e:
        log('读淘汰回执失败:', type(e).__name__, '→ 本次零归档')
        return {}


def heat(c):
    return len(c.get('facts') or []) + (0 if c.get('dormant') else 1) + (0 if c.get('archived') else 1)


def cid_for(title):
    slug = re.sub(r'[^a-z0-9]+', '', title.lower())[:18]
    return 'auto_radar_' + slug


def prepare_skeleton_info(g):
    """v538【骨架预整理】建骨架前用智谱重型整理：中文卡名 + 看点说明 + 关联新闻筛选。
    失败返回 None，make_skeleton 回退用原始英文名——绝不因 LLM 失败卡住建卡。
    用户拍板：新卡判定与骨架预整理都用智谱（人工把关，省 Gemini 额度）。"""
    try:
        from llm_pool import _zhipu
        news = ' / '.join((g.get('cf_titles') or '').split(' / ')[:5])
        prompt = (
            '你是金融终端事件编辑。把下面的 Polymarket 事件整理成事件追踪卡的基础信息。\n'
            '只输出严格 JSON（不要代码块）:\n'
            '{"name":"简洁中文卡名(≤12字)","desc":"这个事件值得关注什么(≤40字)",'
            '"focus":["关注点1","关注点2","关注点3"]}\n\n'
            '事件原名: ' + str(g.get('title') or '') + '\n'
            '24h成交额: $' + str(g.get('vol24h') or '') + '\n'
            '距落地: ' + str(g.get('days_left') or '?') + '天\n'
            '实体: ' + ','.join(g.get('entities') or []) + '\n'
            '关联新闻: ' + news[:300]
        )
        text, ch = _zhipu('glm-z1-flash')([{'role': 'user', 'content': prompt}], 600, 60)
        import re as _re
        text = _re.sub(r'<think>[\s\S]*?</think>', '', text).strip()
        m = _re.search(r'\{[\s\S]*\}', text)
        if not m:
            log('骨架预整理 JSON解析失败,回退原名:', text[:80]); return None
        d = json.loads(_re.sub(r',\s*([}\]])', r'\1', m.group(0)))
        return {'name': str(d.get('name') or '')[:16],
                'desc': str(d.get('desc') or '')[:60],
                'focus': [str(x)[:30] for x in (d.get('focus') or [])[:5]],
                'channel': ch}
    except Exception as e:
        log('骨架预整理失败(回退原名):', type(e).__name__, str(e)[:80])
        return None


def make_skeleton(g, prepared=None):
    """骨架卡：预整理信息(名称/看点/Polymarket链接/关联新闻)供人工确认——不调 Gemini。"""
    cid = cid_for(g['title'])
    slug = str(g.get('slug') or '')
    poly_url = ('https://polymarket.com/event/' + slug) if slug else ''
    name = (prepared or {}).get('name') or str(g['title'])[:16]
    desc = (prepared or {}).get('desc') or ''
    focus = (prepared or {}).get('focus') or []
    cf_titles = [x.strip() for x in (g.get('cf_titles') or '').split(' / ') if x.strip()][:5]
    return {
        'id': cid, 'icon': '📡', 'title': name, 'cat': 'Polymarket雷达',
        'status': '待确认', 'statusCls': 'watch', 'hot': 2,
        'scope': {'core_subject': g['title'][:100],
                  'stage_focus': focus or (g.get('entities') or [])[:5],
                  'exclude_noise': []},
        'proposed_name': name, 'proposed_desc': desc,
        'related_news': cf_titles, 'poly_url': poly_url,
        'summary': desc or ('Polymarket 净新增候选（$%s，CF 已验证）。待确认后生成三段式。' % g.get('vol24h')),
        'progress_line': '当前态势：待确认（Polymarket 雷达候选）',
        'facts': [g['title']], 'newsKeys': (g.get('entities') or [])[:6],
        'pred': {}, 'polymarket': {'title': g['title'], 'volume': g.get('vol24h'),
                                   'url': poly_url, 'matched_at': bjnow()},
        'auto_created': True, '_radar_pending': True, 'created_at': bjnow(),
        '_dormant': False, 'archived': False,
    }


def gemini_complete(card, g):
    """确认后才调：用 chat_heavy(含Gemini)生成三段式。失败保留骨架。"""
    try:
        from llm_pool import chat_heavy
        prompt = (
            '你是事件追踪编辑。为下面这个 Polymarket 事件生成事件追踪卡的三段式。\n'
            '只输出严格 JSON: {"lead":"本轮状态(20字内)","rest":"关键看点(30字内)",'
            '"status":"阶段(6字内)","summary":"一句话摘要(40字内)"}\n'
            '事件: ' + g['title'] + '\n24h成交额: $' + str(g.get('vol24h')) + '\n'
            '距落地: ' + str(g.get('days_left')) + '天\n'
            '实体: ' + ','.join(g.get('entities') or []) + '\n'
            '国内新闻佐证: ' + (g.get('cf_titles') or '')[:200]
        )
        text, ch = chat_heavy([{'role': 'user', 'content': prompt}], max_tokens=400, timeout=90, allow_zhipu=False)
        m = re.search(r'\{[\s\S]*?\}', text)
        if not m:
            log('Gemini 返回无法解析:', text[:80]); return card, ch
        d = json.loads(m.group(0))
        card['summary'] = str(d.get('summary') or '')[:80] or card['summary']
        card['progress_line'] = '当前态势：' + str(d.get('lead') or '')[:30]
        card['lead'] = str(d.get('lead') or '')[:40]
        card['rest'] = str(d.get('rest') or '')[:60]
        card['status'] = str(d.get('status') or card['status'])[:10]
        card['_radar_pending'] = False
        card['confirmed_at'] = bjnow()
        card['model_channel'] = ch
        log('Gemini 补全', card['id'], '→', card['summary'][:30])
        return card, ch
    except Exception as e:
        log('Gemini 补全失败(保留骨架):', type(e).__name__, str(e)[:100])
        return card, 'none'


def main():
    ap = argparse.ArgumentParser()
    # v538b：事件卡链路已全迁 macropulse-news，默认读本仓库本地产物（原 insum raw URL 已弃用）
    ap.add_argument('--radar', default=os.path.join(HERE, '..', 'data', '_radar_shadow.json'))
    ap.add_argument('--events', default=os.path.join(HERE, '..', 'data', 'events_daily.json'))
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    if a.radar.startswith('http'):
        radar = get_json(a.radar)     # 从 insum-cloud raw 读 radar 报告
    else:
        with open(a.radar, encoding='utf-8') as f:
            radar = json.load(f)
    with open(a.events, encoding='utf-8') as f:
        events = json.load(f)
    cards = events.setdefault('cards', {})

    # 给 genuine_new 补 CF 佐证标题（供 Gemini）
    all_cands = {c['title']: c for c in radar.get('candidates', [])}
    genuine = []
    for g in radar.get('genuine_new', []):
        src = all_cands.get(g['title'], {})
        g = dict(g)
        g['cf_titles'] = ' / '.join(h.get('title', '') for h in (src.get('cf') or {}).get('hits', []))
        genuine.append(g)

    confirms = read_confirms()
    archive_confirms = read_archive_confirms()
    log('净新增候选', len(genuine), 'CF确认回执', len(confirms), '淘汰确认回执', len(archive_confirms))

    # 1. 骨架入队（v538：用智谱预整理名称/看点/新闻，不调 Gemini）
    added = 0
    for g in genuine:
        cid = cid_for(g['title'])
        if cid in cards or any(c.get('title') == g['title'] for c in cards.values()):
            continue
        prepared = prepare_skeleton_info(g)   # v538 智谱预整理
        cards[cid] = make_skeleton(g, prepared)
        added += 1
        log('骨架入队', cid, cards[cid]['title'], '(名称来源:', (prepared or {}).get('channel', '原始'), ')')
    log('骨架新增', added)

    # 2. 确认补全（只对已确认的调 Gemini；应用用户改名）
    completed = 0
    for cid, receipt in confirms.items():
        c = cards.get(cid)
        if not c or not c.get('_radar_pending'):
            continue
        g = next((x for x in genuine if cid_for(x['title']) == cid), None)
        # v538：用户在APP改的名，覆盖骨架名（Gemini 写内容时用改后的名）
        user_name = str(receipt.get('name') or '').strip()
        if user_name:
            c['title'] = user_name[:16]
            c['proposed_name'] = user_name[:16]
            log('应用用户改名', cid, '→', c['title'])
        cards[cid], _ = gemini_complete(c, g) if g else (c, 'none')
        completed += 1
    log('确认补全', completed, 'Gemini调用', completed)

    # 3. 末位淘汰（v538：不再自动归档；活跃>5 标记候选，推APP确认后才归档）
    active = [cid for cid, c in cards.items() if not c.get('archived')]
    archive_candidates = []
    if len(active) > MAX_ACTIVE:
        cold = sorted(active, key=lambda cid: heat(cards[cid]))
        archive_candidates = cold[:len(active) - MAX_ACTIVE]
        for cid in archive_candidates:
            cards[cid]['_archive_candidate'] = True
            log('末位淘汰候选(待APP确认)', cid, cards[cid].get('title'))
    # 只对已在APP确认淘汰的候选执行归档
    archived = []
    for cid in archive_confirms:
        c = cards.get(cid)
        if c and c.get('_archive_candidate'):
            c['archived'] = True
            c['_auto_archived'] = True
            c['archived_at'] = time.strftime('%Y-%m-%d')
            c.pop('_archive_candidate', None)
            archived.append(cid)
            log('APP确认归档', cid)
    # 清掉未被确认的淘汰候选标记（下一班重新评估）
    for cid in archive_candidates:
        if cid not in archived and cid in cards:
            cards[cid].pop('_archive_candidate', None)

    events['generated'] = bjnow()
    events['radar_publish'] = {'generated': bjnow(), 'skeleton_added': added,
                               'gemini_completed': completed, 'archive_candidates': archive_candidates,
                               'archived': archived}
    if not a.dry_run:
        with open(a.events, 'w', encoding='utf-8') as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
        log('已写出', a.events)
    else:
        log('dry-run 未写文件')


if __name__ == '__main__':
    main()
