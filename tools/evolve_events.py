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
        # 已确认的历史成果:快讯池滚动会丢失,但它们是本事件的实质进展,必须常驻LLM输入
        'baseline_facts': ['第33次APEC领导人非正式会议11-18~19在深圳举行',
                           '习近平与普京、特朗普举行中俄美元首历史首次三边会晤',
                           '王毅宣布会议达成深圳展望,这是APEC中国年最重要成果',
                           '亚太经合组织互联互通蓝图正式发布',
                           'APEC中国年已达成近50项经贸务实成果,数智领域占半壁',
                           'APEC工商咨询理事会第四次会议闭幕,全年14项成果文件创历年新高'],
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
        # 用户明确指出的重要线索(15:41快讯):两池滚动后已丢失,固化为常驻基线
        'baseline_facts': ['据称黄仁勋将创办新的半导体公司,或筹资约500亿美元用于创业',
                           '英伟达董事会正评估CEO接任方案,市场担忧核心人才流失',
                           '白毛股神爆料黄仁勋若不回归将引发人才流失潮',
                           '英伟达董事会关闭黄仁勋回归大门的传闻引发争议'],
    },
}

SYS = """你是金融终端的事件情报编辑。严格按以下规则，把给定新闻提纯成一张事件卡的头部数据。

【Scope Schema】core_subject=核心追踪主体；stage_focus=重点阶段要素；exclude_noise=明确排除的噪音。
只保留真正推动该事件阶段进展的事实，剔除噪音与擦边新闻。若无实质进展，facts 写 ["今日无实质进展"]。

【三条硬约束·主线强去噪（2026-11-20 用户拍板）】
① 主线强去噪：先在思考阶段逐条过滤今日关联新闻，只保留【直接推进本卡 core_subject 核心冲突/主线】的实质进展。坚决剔除「仅含同一公司/实体名称、但不推进本事件主线」的常规新闻——典型如：日常业务合作（英伟达与微软合作部署AI）、无关并购（收购某AI开发商）、常规财报、A股产业链蹭热点、券商观点、外部机构持仓变动、股价涨跌播报。这些即使标题出现主角名字，也一律丢进 noisy_dropped，绝不写进 lead/rest/facts。被丢弃新闻中的公司名/项目名/机构名也不得出现在 lead/rest 里——即使写成「已过滤」「与主线无关」等元评论也不行，在 noisy_dropped 交代即可。
② 宁缺毋滥 + 禁止编造 + 无进展也要给有效信息：若当日【没有】新的实质主线进展，严禁写「今日无实质进展」这种空话——用户要的是有效信息。此时应【基于昨日卡片】写出该事件【当前已知的最新态势】：现在处于什么阶段、已确认的安排/计划/传闻是什么、卡点在哪（例：黄仁勋卡即使今天没新闻，也要写清「董事会正评估CEO接任、最新传闻其拟创办新半导体公司筹资、回归谈判僵持」这类已知状态；APEC 卡即使已归档，也要写清「已达成哪些成果文件、中美俄会晤发布了什么」）。这些来自【昨日卡片的事实】，不是编造，可以写。status 沿用昨日阶段。只有【昨日卡片也为空/只有「今日无实质进展」】时才写「主线暂无权威进展」。
   严禁【推断/补写/编造】输入标题与昨日卡片都不存在的人物、机构、动作、数字或官方声明——lead/rest/facts 的每一条都必须能在【今日输入 或 昨日卡片】中找到依据，不得把「特朗普」写成「特拉斯政府」、不得凭空生成「董事会紧急会议」「点阵图分歧」。宁可少写，不可编。【禁止推断升级】：不得把「担忧人才流失」推断成「已启动离职流程」、不得把「有望达成」写成「已签署」、不得凭空添加任何【基线事实和今日新闻里都没写明的动作/流程/声明】。摘要里的每一个动作都必须能在【已知基线事实】或【今日关联新闻】里逐字找到对应。【数字规则·区分来源】：【已知基线事实】里的数字（金额/项数/日期）是已核实事实，必须原样写进摘要（用户明确要求写出核心数字，如创办公司筹资额、APEC成果项数）；【今日关联新闻】与【昨日卡片】里的数字一律不得照抄或推算，严禁编造任何数字、比例、流失率、年份；只有【已知基线事实】里的数字可写。
③ 最新进展优先：输入新闻按时间倒序（最新在前）。在主线相关新闻中，优先提取【时间最新、对事件走向影响最大】的核心动作作为 lead 的首句结果。若最新一条是本事件的关键进展（如当事人拟创办新公司筹资、官方宣布调查结论、CEO接任落地），它必须出现在 lead，不得被更早的常规消息挤掉。核心动作里的关键数字（金额、比例、日期、人数）必须原样保留在 lead 或 facts 中，不得省略（如「筹资500亿」不能写成「筹资用于创业」）。

【输出格式】严格 JSON（不要 markdown、不要解释）：
{
  "status": "4~6字阶段气泡，直接反映当前真实阶段(如 多国政要抵华/首轮结束·休会/12月加息几成定局)",
  "progress_line": "当前态势：阶段定性 + 最新核心动作(废除机械天数累加)",
  "lead": "首句：≤45字，直接给当前阶段定性+最核心实质结果(例：谈判僵持：伊朗两日计划待美方回应)",
  "rest": "后续句：展开分歧卡点/细节/赔率，与lead拼成完整简介",
  "facts": ["有效事实1", "有效事实2"],
  "noisy_dropped": ["被剔除的噪音标题(最多3条)"],
  "archived": false
}
【倒金字塔·首句铁律】首页卡片列表只显示简介前两行(约42~45字)。lead 字段必须≤45字，是一个短判断句，直接给「当前阶段定性+最核心实质结果」。参考写法(照此风格)：
  · "12月加息几成定局：CME定价升至96.8%"
  · "战俘交换达成共识，下轮会谈日期仍未定"
  · "APEC今日开幕，19日中俄美元首将历史首会"
lead 绝不能塞进多个事实、绝不能超45字。rest 字段再展开分歧卡点、细节背景、关键人物的表态与倾向。代码会把 lead+rest 拼成完整 summary。绝不把最重要结果埋在后面。
【弹性长度·按信息密度自适应】全文 40~160 字，不要死卡固定字数：
  · 有实质进展/多方会谈/重大协议落地 → 写 130~160 字，把具体成果与分歧交代清楚；
  · 休会/无新进展 → 40~60 字精炼交代现状与最新定价即可，严禁为凑字数写空话套话。
  信息量决定长度，不是长度决定信息量。
【归档】仅当事实确认事件彻底落幕才 archived=true，包括：峰会闭幕/协议签署/领导人会议已成功举行且有下届接续安排/终局合约结算。注意「成功举行+下届已定」= 本届已落幕，应 archived=true；「进行中/即将/筹备」则否。
【小字只做叙事·硬要求】简介(lead+rest)是「每天更新的事件叙事」，**不写任何行情/概率/价格数字**（96.8%、77.5%、布伦特100.8、美债4.3%、JOLTS752.3万等）——这些由卡片下方的实时行情块/Polymarket承载，写进每日快照会打架且过时。
写什么：用**有信息量的事实与表态**填充，比如——
  · 美联储：写「沃什称要为超级智能时代制定政策预案」「施密德警告通胀仍高、需更多紧缩」这类**官员最新表态及对加息的倾向**，而不是利率概率数字；
  · 谈判类：写「谁提了什么方案」「卡在哪个具体分歧」「下轮在哪/何时」；
  · 数据类：写「就业数据超预期强化紧缩预期」这类**定性结论**，不写具体数值。
即：把新闻里「谁说了什么、做了什么、倾向什么」讲清楚，让不点详情也能看懂事件进展；数字一律交给下方实时块。这是叙事，不是行情播报。
【时态准确·硬要求】严格按新闻原文时态和日期，以 prompt 里给出的「当前日期」为基准：新闻写"某日将举行"而当前日期早于该日，就绝不能说成"已举行"；"有望达成"不能说成"已签署"。进行中的事件写明"进行中/待举行"，严禁把未来或进行中的事说成已完成——这是事实性错误。
【格式·硬要求】summary 是连续的一段话，用中文句号分隔，**绝不使用 ①②③ 或任何序号/列表符号**。
【长度自检】写完后自己数字数，必须落在 40~160 字区间；重点是首句≤45字且含核心结果，而不是凑到某个固定字数。"""

def _mask_numbers(s):
    """喂给 LLM 前把新闻里的行情/概率数字脱敏，从源头杜绝它抄进小字。
    只处理带明确单位/符号的数值，保留定性词句。"""
    s = re.sub(r'\d+(?:\.\d+)?\s*%', '（概率数字略）', s)
    s = re.sub(r'\d+(?:\.\d+)?\s*(万|亿|美元|基点|bp)', r'（数值略）\1', s)
    s = re.sub(r'\d+(?:\.\d+)?\s*(美元|元)', '（价格略）', s)
    return s

def _strip_untrusted_numbers(text, baseline_facts):
    """移除摘要里【不在基线事实中】的百分比/概率数字——LLM 会从昨日卡片偷抄旧数字,
    规则约束不可靠,这里确定性清洗。基线事实里的数字是已核实的可保留。"""
    allowed = set()
    for b in (baseline_facts or []):
        for m in re.finditer(r'\d+(?:\.\d+)?\s*(?:%|亿美元|亿元|亿|万项|项)', str(b)):
            allowed.add(m.group().replace(' ', ''))
    def repl(m):
        if m.group().replace(' ', '') in allowed:
            return m.group()
        # 连同紧贴的括号一起清掉,避免留下「（）」
        out = str(text or '')
        return ''
    cleaned = re.sub(r'（\d+(?:\.\d+)?\s*%?）', lambda m: m.group() if m.group()[1:-1].replace(' ', '') in allowed else '', str(text or ''))
    return re.sub(r'\d+(?:\.\d+)?\s*(?:%|概率)', repl, cleaned)

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
            + (f"【已知基线事实·本事件已确认的重要进展，必须纳入摘要，不得遗漏】\n"
               + '\n'.join('- ' + b for b in sc['baseline_facts']) + '\n\n' if sc.get('baseline_facts') else '')
            + f"今日关联新闻（标题列表，数字已脱敏，严禁编写任何数字/百分比/金额）：\n{_mask_numbers(facts_text)}\n\n"
            f"请输出该事件卡的新头部 JSON：用官员表态/谈判动作/倾向性等叙事写简介，严禁写任何数字、百分比、金额、年份。")

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
    }
    if not do_llm:
        return fallback
    try:
        msgs = [{'role': 'system', 'content': SYS}, {'role': 'user', 'content': prompt}]
        text, ch = chat_heavy(msgs, max_tokens=4000, timeout=100)
        out = extract_json(text)
        # v501【用户拍板:严禁降级轻量模型】解析失败也用重型通道重试,
        # 不再调 chat()(含 glm-4-flash/flash-lite),换一条重型重试1次。
        if not out:
            try:
                text2, ch2 = _chat_heavy_once(msgs, max_tokens=4000, timeout=100)
                out = extract_json(text2)
                if out:
                    ch = ch2 + '(heavy-reparse-ok)'
            except Exception:
                pass
        if not out:
            fallback['model_channel'] = ch + '(parse-failed)'
            return fallback
        out['model_channel'] = ch
        # v504 清理元评论:重型模型偶尔把「因不涉及核心议题被过滤」写进facts,
        # 那是编辑过程说明而非事实,确定性剔除。
        out['facts'] = [f for f in (out.get('facts') or [])
                        if not re.search(r'被过滤|已剔除|与本次.*无关|不涉及.*核心', str(f))]
        if _rule_dropped:
            out['noisy_dropped'] = (out.get('noisy_dropped') or []) + [n[:40] for n in _rule_dropped[:3]]
        # v503【反编造·代码级硬护栏】facts 明确写了「今日无实质进展」时,
        # 严禁 lead/rest 再编造任何人物/机构/动作(实测重型模型会一边写无进展、一边编
        # 「董事会紧急会议」「CFO接受评估」)。此处直接覆盖成无进展模板,并沿用昨日阶段。
        _facts0 = out.get('facts') or []
        _no_progress = any('今日无实质进展' in str(x) for x in _facts0)
        if _no_progress:
            # 用户拍板:无新进展也要给有效信息——沿用昨日摘要(最新已知态势),不写空话。
            # LLM 若已从昨日卡片写出实质回顾(lead/rest 非空且非「无实质进展」),尊重它;
            # 否则直接沿用昨日 summary,保证卡片永远有可读的最新状态。
            _l = str(out.get('lead') or '')
            # 空话特征:无实质进展/暂无权威/等待确认/主线待,或过短(<8字)
            _llm_said_nothing = (not _l.strip()) or len(_l) < 8 or \
                                any(k in _l for k in ['无实质进展', '暂无权威', '等待权威', '主线待', '暂无进展', '无新进展'])
            if _llm_said_nothing:
                _prev_sum = (_prev or {}).get('summary') or ''
                if _prev_sum and '无实质进展' not in _prev_sum:
                    out['lead'] = _prev_sum[:45]
                    out['rest'] = _prev_sum[45:] if len(_prev_sum) > 45 else ''
                    out['progress_line'] = '当前态势：' + _prev_sum[:40]
                out['facts'] = (_prev or {}).get('facts') or []
            out['status'] = (_prev or {}).get('status') or out.get('status') or sc['baseline_status']
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
        out['summary'] = _strip_untrusted_numbers(summary, sc.get('baseline_facts'))
        # v504【关键数字保真】实测重型模型会把「筹资500亿」改写成「筹资创业」丢掉数字。
        # 最新一条主线新闻里的金额/比例若没进摘要,这里确定性注入 lead,保证核心进展可辨认。
        _core = news[0] if news else ''
        _num = re.search(r'\d+(?:\.\d+)?\s*(?:万亿|亿美元|亿元|亿|%|基点|bp)', str(_core))
        if _num and not _no_progress:
            tok = _num.group().replace(' ', '')
            if tok not in str(out.get('summary', '')):
                lead = out.get('lead') or lead
                m = re.search(r'(筹资|融资|出资|投资|估值|规模|减持|增持)', lead)
                lead = (lead[:m.end()] + tok + lead[m.end():]) if m else (lead.rstrip('。') + '（' + tok + '）')
                if len(lead) > 45:
                    lead = lead[:45].rstrip('，；、 ') + '…'
                out['lead'] = lead
                out['summary'] = (lead + (out.get('rest') or '')).strip()
                summary = out['summary']
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
                                   {'role': 'user', 'content': fix_prompt}], max_tokens=4000, timeout=100)
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
        # 最终出口统一清洗:前面 v504 注入/长度重写会重新赋值 summary,必须在此兜底。
        _cleaned = _strip_untrusted_numbers(out.get('summary', ''), sc.get('baseline_facts'))
        if _cleaned != out.get('summary', ''):
            out['summary'] = _cleaned
            slen = len(_cleaned)
        # 硬下限60字:用户明确反馈太精简。不足时从【基线事实】补(已核实,非编造),
        # 不从不确定的今日新闻补,保证信息量与真实性兼得。
        if slen < 60 and sc.get('baseline_facts'):
            have = out['summary']
            for b in sc['baseline_facts']:
                b = str(b).strip()
                if not b or b in have:
                    continue
                cand = have.rstrip('。') + '。' + b
                if len(cand) <= 160:
                    have = cand
                    if len(have) >= 60:
                        break
            if have != out['summary']:
                out['summary'] = have
                slen = len(have)
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
    a = ap.parse_args()

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
    for cid in SCOPES:
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
