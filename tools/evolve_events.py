# -*- coding: utf-8 -*-
"""事件卡片每日态势演进（GHA 云端跑，LLM 提纯三段式）。

流程：
  1) 内置 4 张卡的 Scope Schema（core_subject / stage_focus / exclude_noise）
  2) 读今日关联新闻（news.json，由抓取步骤产出）+ 昨日卡片状态（events_daily.json 或 events.js 基线）
  3) 每张卡：LLM 按 Scope 过滤噪音 → 提取 Verified Facts → 三段式重写
     （新气泡 status / 进度栏 progress_line / 简介 summary / archived / model_channel）
  4) 输出 events_daily.json（前端读它覆盖快照字段）

LLM 走 llm_pool 四通道降级；全挂则保留昨日卡片并标 channel=local-rule(未重写)。
凭据从环境变量读，不打印。
"""
import os, sys, json, time, argparse, re
from datetime import datetime, timezone, timedelta
# v461【时间bug】GHA runner 是 UTC，time.strftime 写出 UTC 时钟（如北京11:56写成03:56），
# 前端按北京时间解析→显示"8小时前"。统一用北京时间写入。
def bj_now_str():
    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_pool import chat, chat_heavy, _chat_heavy_once, AllModelsFailed, status as model_status

# 4 张卡的 Scope Schema（v2 演练定稿）
SCOPES = {
    'iran': {
        'title': '美伊冲突 · 霍尔木兹海峡', 'icon': '🛢️',
        'core_subject': '美伊围绕霍尔木兹海峡封锁与开放谈判的进程',
        'stage_focus': ['封锁/通航状态变化', '双方提案与回应', '斡旋方动作', '油轮安全事件', '核议题进展'],
        'exclude_noise': ['泛中东局势评论', '油价技术面分析', '其他地区冲突', '无新动作的旧闻复述'],
        'baseline_status': '僵持中',
    },
    'fed': {
        'title': '美联储政策路径', 'icon': '🏛️',
        'core_subject': '美联储 12 月 FOMC 利率决议的市场定价与官方表态',
        'stage_focus': ['利率决议/纪要/点阵图', 'FOMC 日程', '主席及票委表态', 'CME/Polymarket 概率变化', '关键经济数据'],
        'exclude_noise': ['欧央行/日银等其他央行', '泛市场评论', '股票个股新闻', '与利率无关的美股/科技新闻'],
        'baseline_status': '跟踪中',
    },
    'ukraine': {
        'title': '俄乌会谈', 'icon': '🕊️',
        'core_subject': '阿布扎比俄美乌三方会谈进程及其后续排期',
        'stage_focus': ['会谈场次与成果', '战俘/人道交换协议', '下轮会谈排期与地点', '参与方变化(欧洲上桌)', '核心分歧变化'],
        'exclude_noise': ['前线零星互袭无谈判关联', '各国立场表态无行动', '领导人其他行程', '市场评论'],
        'baseline_status': '跟踪中',
    },
    'apec': {
        'title': 'APEC 领导人会议', 'icon': '🌐',
        'core_subject': '2026 APEC 第33次领导人会议（深圳）进程与成果',
        'stage_focus': ['领导人抵离与会晤安排', '中俄美元首会晤等重大多边互动', '宣言/自贸区等成果文件', '经贸数智合作落地', '下届东道主交接'],
        'exclude_noise': ['泛APEC历史科普', '地方文旅/安保花絮', '与本次会议无关的双边旧闻', '无实质动作的官员泛论'],
        'baseline_status': '进行中',
    },
    'huang': {
        'title': '黄仁勋事件跟踪', 'icon': '🧑‍💼',
        'core_subject': '黄仁勋相关调查进展、其后续动向与英伟达 CEO 接任安排',
        'stage_focus': ['相关调查的后续进展', '黄仁勋本人后续计划/回归动向', '英伟达 CEO 接任或管理层调整', '董事会与核心人才流失动态', '官方声明与权威媒体跟进'],
        'exclude_noise': ['英伟达股价日常波动', 'GPU产品发布/技术科普', '与黄仁勋本人无关的公司新闻', '未经证实的传闻与股吧爆料(除非权威媒体跟进)'],
        # v503【输入层确定性去噪】用户实录:同公司常规新闻(微软合作/收购Inception)会把
        # 15:41「筹资500亿创办新公司」的核心进展挤出摘要。提示词的否定约束不可靠,
        # 这里在喂给LLM前直接剔除,模型看不到就不会写、也不会编成「被否决/搁置」。
        'input_drop': ['微软', 'Azure', 'Windows PC', 'Inception', '扩散式AI', '收购',
                       'Vera Rubin', 'GPU', '调仓', 'Palantir', 'SpaceX', '特朗普.*买入', '买入.*英伟达', '盘前跌幅', '股价转跌',
                       '市值累计蒸发', '股价转跌', '财报', 'Vera', '一文穿透'],
        'baseline_status': '跟踪中',
    },
}

SYS = """你是金融终端的事件情报编辑。请结合【昨日卡片状态】与【今日关联新闻（按时间倒序）】，按 Scope Schema（core_subject 核心主体 / stage_focus 阶段焦点 / exclude_noise 排除噪音），将事件提纯为以下严格 JSON（不要 markdown 代码块，不要额外解释）：

{
  "status": "4~6字阶段气泡，直指当前真实阶段",
  "progress_line": "当前态势：阶段定性 + 最新核心动作",
  "lead": "首句≤45字：当前阶段定性 + 最新最核心的实质结果",
  "rest": "后续句：展开分歧卡点、关键表态或细节，与 lead 拼成完整简介",
  "facts": ["推进主线的有效事实1", "有效事实2"],
  "noisy_dropped": ["被剔除的无关标题(最多3条)"],
  "archived": false
}

【核心铁律】
1. 主线强去噪：只保留直接推进 core_subject 核心冲突的实质进展。仅含同一公司/人物名称的常规业务合作、无关并购、常规财报、A股产业链蹭热点、券商观点、外部持仓变动，一律丢进 noisy_dropped，绝不在正文中提及（也不要写"已过滤"等元评论）。
2. 记忆继承与防编造：每条事实必须能在【今日新闻】或【昨日卡片】中找到原文依据，严禁脑补未出现的动作、会议或声明。若今日无主线实质进展，直接沿用昨日最新态势与阶段，不写"今日无实质进展"等空话；仅当昨日也无有效事实时，才写"主线暂无权威进展"。
3. 数字规则（区分两类）：
   - 实时行情/涨跌幅/赔率/股价数字（如 96.8%、涨5%、布伦特100美元）一律不写，交给下方实时块；
   - 事件本身的事实数字（如筹资金额、协议项数、关键日期）必须严格按原文保留，严禁跨新闻串台或编造。
4. 排版与时态：
   - 倒金字塔结构：lead 必须≤45字且包含最新核心动作；lead+rest 拼成的全文控制在 40~160 字（有重大进展写足细节，无新进展 40~60 字精炼交代现状），用中文句号连成一段，禁用 ①②③ 或列表符号；
   - 严格以输入的"当前日期"为准，区分"将举行/进行中/已闭幕"，绝不把未来或进行中的事写成已完成；仅当事件彻底落幕（闭幕/签署/合约终局）时 archived=true。"""

def build_user_prompt(card, facts_text, odds_text, yesterday):
    sc = SCOPES[card]
    # 不再把 odds 传给 LLM——小字不写数字，赔率交给前端实时块；
    # 新闻标题里的数字也脱敏，避免 LLM 照抄。
    from datetime import datetime
    _today = datetime.now().strftime('%Y-%m-%d')
    return (f"当前日期：{_today}（所有时态判断以此为准）\n"
            f"事件：{sc['title']}\n"
            f"core_subject：{sc['core_subject']}\n"
            f"stage_focus：{'、'.join(sc['stage_focus'])}\n"
            f"exclude_noise：{'、'.join(sc['exclude_noise'])}\n\n"
            f"昨日卡片：{yesterday}\n\n"
            + f"今日关联新闻（标题列表，按时间倒序）：\n{facts_text}\n\n"
            f"请严格按 SYS 的 JSON 格式输出。")

def extract_json(text):
    """v472【弃用贪婪正则】按序：①剥离<think>标签 ②剥离markdown围栏 ③JSONDecoder.raw_decode括号配对提取。
    杜绝思考过程中的 {} 干扰，容忍前后废话。"""
    if not text:
        return None
    t = str(text)
    # ① 剥离 <think>...</think>（含未闭合的 <think> 开头）
    t = re.sub(r'<think>[\s\S]*?</think>', '', t, flags=re.I)
    t = re.sub(r'<think>[\s\S]*$', '', t, flags=re.I)
    # 也兼容 <|thought|> / 【思考】 等变体
    t = re.sub(r'【思考】[\s\S]*?【/思考】', '', t)
    # ② 剥离 markdown 代码围栏（```json ... ``` / ``` ... ```）
    t = re.sub(r'```(?:json|JSON)?\s*', '', t)
    t = t.replace('```', '')
    # ③ 用 JSONDecoder.raw_decode 逐个候选 `{` 尝试，取第一个合法对象
    dec = json.JSONDecoder()
    for m in re.finditer(r'\{', t):
        try:
            obj, _ = dec.raw_decode(t[m.start():])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def evolve(card, news, odds, yesterday, do_llm=True, prev_card=None):
    sc = SCOPES[card]
    # v503 输入层确定性去噪:命中的新闻不喂给LLM(防模型提及或编造后续)
    _drop_pats = sc.get('input_drop') or []
    _rule_dropped = [n for n in news if any(re.search(p, n, re.I) for p in _drop_pats)]
    news = [n for n in news if not any(re.search(p, n, re.I) for p in _drop_pats)] if _drop_pats else news
    facts_text = '\n'.join('- ' + n for n in news[:30]) if news else '（今日无关联新闻）'
    prompt = build_user_prompt(card, facts_text, odds, yesterday)
    # v472【保留旧状态】fallback 必须沿用上一轮真实状态，严禁硬编码"进行中/archived=false"
    # 强行覆盖——否则已结束/已归档的事件会被重新激活。
    _prev = prev_card if isinstance(prev_card, dict) else None
    fallback = {
        'status': (_prev or {}).get('status') or sc['baseline_status'],
        'progress_line': (_prev or {}).get('progress_line') or '当前态势：等待今日数据',
        'summary': (_prev or {}).get('summary') or sc['core_subject'],
        'facts': (_prev or {}).get('facts') or [],
        'noisy_dropped': [],
        # 关键：保留上一轮 archived/dormant，绝不因为解析失败而复活事件
        'archived': bool((_prev or {}).get('archived', False)),
        'dormant': bool((_prev or {}).get('dormant', False)),
        'model_channel': 'local-rule',
        # v565【每卡独立更新时间】fallback 表示"本轮没更新成功、沿用旧内容"，
        # 绝不能刷成当前时间（否则前端显示"刚刚更新"却是旧内容=假报）。
        # 保留上一轮 true updated；老数据没有该字段则留空，前端回落全局 generated。
        'updated': (_prev or {}).get('updated') or '',
    }
    if not do_llm:
        return fallback
    try:
        msgs = [{'role': 'system', 'content': SYS}, {'role': 'user', 'content': prompt}]
        # v464：单次60s + 累计退避≤180s，避免LLM全挂时睡满60分钟霸占workflow并发锁
        text, ch = chat_heavy(msgs, max_tokens=4000, timeout=60, temperature=0.15,
                              allow_zhipu=False, groq_fallback=True, retry_total=180)
        out = extract_json(text)
        # v501【用户拍板:严禁降级轻量模型】解析失败也用重型通道重试,
        # 不再调 chat()(含 glm-4-flash/flash-lite),换一条重型重试1次。
        if not out:
            try:
                text2, ch2 = _chat_heavy_once(msgs, max_tokens=4000, timeout=60, temperature=0.15, allow_zhipu=False, groq_fallback=True)
                out = extract_json(text2)
                if out:
                    ch = ch2 + '(heavy-reparse-ok)'
            except Exception:
                pass
        if not out:
            fallback['model_channel'] = ch + '(parse-failed)'
            return fallback
        out['model_channel'] = ch
        # v565【每卡独立更新时间】只有真跑成功 LLM 的卡才写 updated。
        # 之前全批共用顶层 generated，前端只能拿它显示 → 看上去"所有卡同一时间一起更新"，
        # 掩盖了"某张卡其实掉 fallback 没更新"的事实。单卡模式下其余卡保留各自旧值。
        out['updated'] = bj_now_str()
        # v504 清理元评论:重型模型偶尔把「因不涉及核心议题被过滤」写进facts,
        # 那是编辑过程说明而非事实,确定性剔除。
        out['facts'] = [f for f in (out.get('facts') or [])
                        if not re.search(r'被过滤|已剔除|与本次.*无关|不涉及.*核心', str(f))]
        if _rule_dropped:
            out['noisy_dropped'] = (out.get('noisy_dropped') or []) + [n[:40] for n in _rule_dropped[:3]]
        # v432：优先用 lead(首句) + rest 拼接，代码层强制首句≤45字铁律
        lead = re.sub(r'[①②③④⑤]', '', out.get('lead', '')).strip()
        rest = re.sub(r'[①②③④⑤]', '', out.get('rest', '')).strip()
        if lead:
            if len(lead) > 45:
                # 超长：按逗号/顿号截到最后一个≤45字的语义点，不硬切词
                cut = None
                for sep in ['，', '；', '、', ' ']:
                    idx = lead.rfind(sep, 0, 46)
                    if idx > 15:
                        cut = lead[:idx]
                        break
                lead = (cut or lead[:45]).rstrip('，；、 ') + '…'
            if not lead.endswith(('。', '！', '？', '…')):
                lead += '。'
            out['_lead_truncated'] = len(out.get('lead','')) > 45
            summary = (lead + rest).strip()
        else:
            summary = re.sub(r'[①②③④⑤]', '', out.get('summary', '')).strip()
        out['summary'] = summary
        # 弹性长度校验：40~160 字；首句铁律：第一句≤45字且含核心结果
        slen = len(summary)
        first_sent = re.split(r'[。！？]', summary)[0]
        first_len = len(first_sent)
        need_fix = not (40 <= slen <= 160) or first_len > 45
        if need_fix:
            problems = []
            if slen < 40: problems.append('全文只有%d字(太短,至少40)' % slen)
            if slen > 160: problems.append('全文%d字(太长,最多160)' % slen)
            if first_len > 45: problems.append('第一句%d字(超过45字铁律,首句必须直接给核心结果)' % first_len)
            fix_prompt = (prompt + '\n\n【重写指令】你上次输出有问题：' + '；'.join(problems)
                          + '。请按倒金字塔重写：第一句≤45字直接给核心结果，全文40~160字按信息密度自适应，只输出完整JSON。')
            try:
                text2, ch2 = chat_heavy([{'role': 'system', 'content': SYS},
                                   {'role': 'user', 'content': fix_prompt}], max_tokens=4000, timeout=60, temperature=0.15, allow_zhipu=False, groq_fallback=True)
                out2 = extract_json(text2)
                if out2 and out2.get('summary'):
                    s2 = re.sub(r'[①②③④⑤]', '', out2['summary']).strip()
                    fs2 = len(re.split(r'[。！？]', s2)[0])
                    if 40 <= len(s2) <= 160 and fs2 <= 45:
                        out['summary'] = s2
                        out['model_channel'] = ch2 + '(rewritten)'
                        slen, first_len = len(s2), fs2
            except Exception:
                pass
        # v461【代码层硬兜底】重型模型偶尔连续两次输出过短摘要(19字)，重写也算不达标。
        # 用 facts 事实清单补足到≥30字（事实是已核实内容，不是编造），再做首句≤45截断。
        if len(out['summary']) < 30:
            facts = out.get('facts') or []
            extra = ''
            for f in facts:
                f = re.sub(r'[①②③④⑤]', '', str(f)).strip()
                if f and f not in out['summary']:
                    extra += ('。' if extra else '') + f
                if len(out['summary']) + len(extra) >= 42:
                    break
            if extra:
                cand = out['summary'].rstrip('。') + '。' + extra
                if len(cand) <= 160:
                    out['summary'] = cand
                    slen = len(cand)
        # 【提交护栏安全网】GHA 的 assert len(first)<=48 / len(s)<=160 会整轮拒绝提交,
        # 这里幂等兜底:首句超48就在最近的句号断开,全文超160就截到完整句,绝不因
        # 单张卡的随机生成让整轮数据白跑。
        _s = out.get('summary', '')
        if len(_s) > 160:
            _cut = _s.rfind('。', 0, 160)
            _s = (_s[:_cut + 1] if _cut > 40 else _s[:160])
        _parts = re.split(r'(?<=。)', _s)
        if _parts and len(_parts[0]) > 48:
            _p = _parts[0]
            _c = _p.rfind('，', 0, 48)
            _parts[0] = (_p[:_c] + '。') if _c > 15 else (_p[:45] + '。')
            _s = ''.join(_parts)
        if _s != out.get('summary', ''):
            out['summary'] = _s
            slen = len(_s)
        out['_summary_len'] = slen
        out['_first_sent_len'] = len(re.split(r'[。！？]', out.get('summary', ''))[0])
        if not (40 <= slen <= 160) or first_len > 45:
            out['_warn'] = 'summary %d chars, first sentence %d chars (target 40-160, first<=45)' % (slen, first_len)
        # v487【归档单向锁·修APEC复活bug】LLM正常返回路径此前直接沿用LLM判的 archived，
        # 导致昨天已归档(峰会闭幕)的卡今天因新新闻被LLM重判false又弹回主列表。
        # 归档是用户/演进的终态决定：一旦上一轮 archived=true，本轮强制保持 true（内容仍可更新，
        # 但卡片留在折叠区）。同理 dormant。fallback 路径已有此逻辑，这里补齐正常路径。
        if _prev:
            if _prev.get('archived'):
                out['archived'] = True
            if _prev.get('dormant'):
                out['dormant'] = True
        return out
    except AllModelsFailed as e:
        fallback['model_channel'] = 'local-rule(all-llm-failed)'
        return fallback

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--news', default='E:/WorkBuddy工作区/2026-11-03-23-29-44/data/events_news_today.json',
                    help='今日关联新闻 JSON: {card_id: [title,...]}')
    ap.add_argument('--prev', default='E:/WorkBuddy工作区/2026-11-03-23-29-44/data/events_daily.json')
    ap.add_argument('--out', default='E:/WorkBuddy工作区/2026-11-03-23-29-44/data/events_daily.json')
    ap.add_argument('--no-llm', action='store_true')
    ap.add_argument('--card', default='',
                    help='v564:只更新指定卡(空=全部5卡),如 huang。APP详情页「更新」按钮走此参数')
    a = ap.parse_args()

    # v564【单卡模式】校验卡 id 必须在 SCOPES 内，防注入任意串
    single = (getattr(a, 'card', '') or '').strip().lower()
    if single and single not in SCOPES:
        print('unknown card: %s (可用: %s)' % (single, ','.join(SCOPES)))
        sys.exit(2)

    news_map = {}
    if os.path.exists(a.news):
        try: news_map = json.load(open(a.news, encoding='utf-8'))
        except Exception: news_map = {}
    prev = {}
    if os.path.exists(a.prev):
        try: prev = json.load(open(a.prev, encoding='utf-8'))
        except Exception: prev = {}
    # v502【独立持久锁·防"某轮漏锁→永久失锁"】
    # 原单向锁只读上一轮prev:一旦某轮(如并发run)漏锁写成false,后续prev全false,锁永久失效。
    # 这里读独立锁文件作为权威源,不受prev波动影响。内容与 events_daily.json 同目录。
    locks = {}
    lock_path = os.path.join(os.path.dirname(a.out) or '.', 'event_locks.json')
    if os.path.exists(lock_path):
        try: locks = json.load(open(lock_path, encoding='utf-8')) or {}
        except Exception: locks = {}

    odds_default = {
        'iran': 'Polymarket：解除封锁37% / 通行正常8.5%',
        'fed': 'Polymarket：12月加息25bp约97%；CME 96.8%；美债10Y约4.3%',
        'ukraine': 'Polymarket：停火合约无明确终局定价',
        'apec': 'Polymarket：暂无对应终局合约；亚太概念受会议预期提振',
        'huang': 'Polymarket：暂无对应终局合约；英伟达管理层事件跟踪',
    }
    result = {'generated': bj_now_str(), 'cards': {},
              'model_channels': model_status()}
    if single:
        # v564【单卡模式以 prev 为基底】输出整棵覆盖 events_daily.json，
        # 若只写目标卡会把其余卡清空——必须先复制 prev 全部卡再只 evolve 目标。
        for _k, _v in (prev.get('cards') or {}).items():
            result['cards'][_k] = dict(_v) if isinstance(_v, dict) else _v
        cards_iter = [single]
    else:
        cards_iter = list(SCOPES)
    for _ci, cid in enumerate(cards_iter):
        # v506【归档卡不更新】已归档/休眠的卡保留昨日内容,不再调LLM(省额度+防复活)
        _lk = locks.get(cid) or {}
        _pv = prev.get(cid) if isinstance(prev.get(cid), dict) else None
        if _lk.get('archived') or _lk.get('dormant') or (_pv and (_pv.get('archived') or _pv.get('dormant'))):
            if _pv:
                out = dict(_pv)
                out['archived'] = True
                out['model_channel'] = 'skip-archived'
                result['cards'][cid] = out
                print('[%s] SKIP archived/dormant (保留昨日内容)' % cid)
                continue
        # v506【防Gemini免费版429】卡间间隔：25→45→60秒(v509,用户拍板)。
        # 实测早班5卡连发、25秒间隔仍有3张撞429。60秒把每分钟请求压到1张，
        # 最大化给免费版RPM留余量；5卡总间隔4分钟，对timeout=60分无影响。
        # 注意：限流按GCP项目计(非按key)，根治需多项目多key或付费层，见RULES_运维发版.md。
        # v564：单卡模式无卡间间隔（只调 1 次 LLM）；全量5卡仍保持 60s 防 429
        if _ci > 0 and not single:
            time.sleep(60)
        yesterday = ''
        if cid in prev and isinstance(prev[cid], dict):
            yesterday = prev[cid].get('summary', '')
        if not yesterday:
            yesterday = SCOPES[cid]['core_subject']
        out = evolve(cid, news_map.get(cid, []), odds_default[cid], yesterday, not a.no_llm,
                     prev_card=prev.get(cid) if isinstance(prev.get(cid), dict) else None)
        # 持久锁最后一道闸:覆盖所有路径(LLM成功/fallback),不受prev波动影响
        lk = locks.get(cid) or {}
        if lk.get('archived'):
            out['archived'] = True
        if lk.get('dormant'):
            out['dormant'] = True
        result['cards'][cid] = out
        print('[%s] channel=%s status=%s len=%s first=%s' % (
            cid, out.get('model_channel'), out.get('status'),
            out.get('_summary_len'), out.get('_first_sent_len')))

    # v502 锁自愈: 本轮判定archived/dormant=true的卡写回锁文件,持续记住终态
    try:
        for _cid, _c in result.get('cards', {}).items():
            if _c.get('archived') or _c.get('dormant'):
                _e = locks.setdefault(_cid, {})
                if _c.get('archived'): _e['archived'] = True; _e.setdefault('locked_at', bj_now_str())
                if _c.get('dormant'): _e['dormant'] = True
        os.makedirs(os.path.dirname(lock_path) or '.', exist_ok=True)
        with open(lock_path, 'w', encoding='utf-8') as f:
            json.dump(locks, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print('[lock] 锁文件写入失败: %s' % e)
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print('已写', a.out)

if __name__ == '__main__':
    main()
