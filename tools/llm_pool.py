# -*- coding: utf-8 -*-
"""多通道免费模型有序降级链（OpenAI 兼容），供 GHA 快讯/卡片链路共用。

设计要点（2026-11-18 实测）：
  · 免费档几乎都是 **1 并发**，所以不做"并发竞速"（会互抢配额触发 429），
    而是按实测速度排序做**有序故障转移**：快的当主，挂了才切下一个。
  · 通道顺序（实测）：
      1) 智谱 glm-4-flash-250414        → 200 / 0.6s（最快）
      2) Google gemini-flash-lite-latest → 200 / 快（v1beta 别名）
      3) Google gemini-3.1-flash-lite    → 200 / 快
      4) 本地规则兜底（本模块抛 AllModelsFailed，调用方回退）
  · 已淘汰：glm-4.7-flash（429 拥挤）、glm-4.5-flash（4.7s 空回复）、
    gemini-2.5-flash-lite / 2.5-flash（该 key 404 no longer available）。

凭据从环境变量读：ZHIPU_KEY / GEMINI_KEY；绝不打印 key 明文。
【v2 自动检测】每个通道在调用前检查自己的 key 环境变量：未配置则静默跳过该路
（不报错、不浪费超时），两路全不可用/全失败时抛 AllModelsFailed，调用方平滑回退
本地确定性规则。status() 可查每路"已配置/未配置"，供前端状态栏与自检页显示。
用法：
  from llm_pool import chat, status
  text, channel = chat("把这条新闻判为 S/A/B/C", max_tokens=200)  # 全挂抛 AllModelsFailed
  available = status()  # [{'channel':..., 'configured':bool}, ...]
"""
import os, json, urllib.request, urllib.error, time

ZHIPU_URL = 'https://open.bigmodel.cn/api/paas/v4/chat/completions'
ZHIPU_MODEL = 'glm-4-flash-250414'
GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta/models/'

class AllModelsFailed(Exception):
    pass

def _load_env_file():
    p = 'E:/WorkBuddy工作区/_tools/llm.env'
    if os.path.exists(p):
        for line in open(p, encoding='utf-8'):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k, v)
_load_env_file()

def _zhipu(model, temperature=None):
    def call(messages, max_tokens, timeout):
        key = os.environ.get('ZHIPU_KEY', '')
        if not key: raise RuntimeError('no ZHIPU_KEY')
        body = {'model': model, 'messages': messages,
                'max_tokens': max_tokens, 'thinking': {'type': 'disabled'}}
        if temperature is not None:
            body['temperature'] = temperature
        req = urllib.request.Request(ZHIPU_URL, data=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        return d['choices'][0]['message']['content'] or '', 'zhipu/' + model
    return call

def _gemini(model):
    """普通档 Gemini（v539 多key轮询）：遇 429/报错必须把全部 key 轮流试完才抛，
    让上层 chat() 再切下一个模型——硬性要求：不能第一个 key 报错就跳走。"""
    def call(messages, max_tokens, timeout):
        keys = _gemini_keys()   # 与重型链共用：GEMINI_KEY/API_KEY/API_KEYS 逗号拆分
        if not keys: raise RuntimeError('no gemini key')
        parts = [{'role': 'user', 'parts': [{'text': m.get('content', '')}]} for m in messages]
        # v449：Gemini 3.x 关思考走轻量；flash-lite 绝不能传 thinkingConfig（400）
        gc = {'maxOutputTokens': max_tokens}
        if model.startswith('gemini-3'):
            gc['thinkingConfig'] = {'thinkingLevel': 'minimal'}
        elif model.startswith('gemini-2.5') and 'flash' in model:
            gc['thinkingConfig'] = {'thinkingBudget': 0}
        body = {'contents': parts, 'generationConfig': gc}
        last = None
        for ki, key in enumerate(keys):
            url = GEMINI_BASE + model + ':generateContent?key=' + key
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429: print('[llm] gemini key#%d 429 切下一个 (%d/%d)' % (ki+1,ki+1,len(keys)))
                else: print('[llm] gemini key#%d HTTP%d' % (ki+1, e.code))
                continue
            except Exception as e:
                last = e
                print('[llm] gemini key#%d %s' % (ki+1, type(e).__name__))
                continue
            text = (d.get('candidates') or [{}])[0].get('content', {}).get('parts', [{}])[0].get('text') or ''
            if not text:
                last = RuntimeError('empty text'); continue
            return text, 'gemini/' + model
        if isinstance(last, urllib.error.HTTPError):
            raise urllib.error.HTTPError(last.url, last.code, 'all %d gemini keys failed' % len(keys), last.headers, last.fp)
        raise RuntimeError('all %d gemini keys failed' % len(keys))
    return call

GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'


def _groq(model, heavy=False):
    """Groq 通道（OpenAI 兼容）。heavy=True 用 gpt-oss-120b + reasoning_effort=low，
    给足 max_tokens 防推理挤占正文（v538 验收结论）。普通档用 llama-3.3-70b。"""
    def call(messages, max_tokens, timeout):
        key = os.environ.get('GROQ_KEY', '') or os.environ.get('GROQ_API_KEY', '')
        if not key: raise RuntimeError('no GROQ_KEY')
        body = {'model': model, 'messages': messages, 'max_tokens': max_tokens}
        if heavy:
            body['reasoning_effort'] = 'low'
        # v539b：Groq 拒绝 Python-urllib 默认 UA（实测 403），必须显式带 UA（带后 200）
        req = urllib.request.Request(GROQ_URL, data=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                     'User-Agent': 'mp-llm/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        msg = (d.get('choices') or [{}])[0].get('message') or {}
        text = msg.get('content') or ''
        if not text: raise RuntimeError('groq empty content')
        return text, 'groq/' + model.replace('qwen/', '')
    return call


# 有序降级链（v539 四场景分工之普通档）：Groq 主 → Gemini 兜底 → 智谱最后。
# 每项 (通道名, 调用函数, 所需key环境变量)。
CHANNELS = [
    # v539 普通档主：Groq llama-3.3-70b（场景3新卡判定 / 场景4异动归因主通道）
    ('groq/qwen3.8-27b',           _groq('qwen/qwen3.8-27b'),           'GROQ_KEY'),  # 11-22实测llama-3.3已下架,gpt-oss-20b空内容,选qwen3.8
    # Gemini 普通兜底（内部会把全部 key 轮流试完才抛；含两个 lite 模型）
    ('gemini/gemini-flash-lite-latest', _gemini('gemini-flash-lite-latest'), 'GEMINI_KEY'),
    ('gemini/gemini-3.1-flash-lite',    _gemini('gemini-3.1-flash-lite'),    'GEMINI_KEY'),
    # 智谱最后兜底
    ('zhipu/' + ZHIPU_MODEL,            _zhipu(ZHIPU_MODEL),   'ZHIPU_KEY'),
]

def _configured(key_env):
    return bool(os.environ.get(key_env, '').strip())

def status():
    """返回每路配置状态(不含key明文)，供前端状态栏/自检页显示。
    返回 [{'channel':..., 'key_env':..., 'configured':bool}, ...] + 兜底项。"""
    seen = set()
    out = []
    for name, _, kv in CHANNELS:
        provider = name.split('/')[0]
        if provider in seen:  # 同一 key 的多模型只列一次配置状态
            continue
        seen.add(provider)
        out.append({'channel': provider, 'key_env': kv, 'configured': _configured(kv)})
    out.append({'channel': 'local-rule', 'key_env': None, 'configured': True})
    return out

def any_configured():
    """至少一路 key 已配置 → True。供调用方决定是否值得尝试 LLM。"""
    return any(_configured(kv) for _, _, kv in CHANNELS)

def chat(messages, max_tokens=300, timeout=30, verbose=True):
    """按序尝试，返回 (text, channel)。channel 供前端状态栏显示。
    · 未配置 key 的通道：静默跳过（不报错、不占超时）。
    · 两路全未配置 或 全部调用失败：抛 AllModelsFailed → 调用方平滑回退本地确定性规则。"""
    if isinstance(messages, str):
        messages = [{'role': 'user', 'content': messages}]
    errs, skipped = [], []
    for name, fn, key_env in CHANNELS:
        # 自动检测：该路 key 未配置 → 静默跳过
        if not _configured(key_env):
            skipped.append(name)
            continue
        t = time.time()
        try:
            text, ch = fn(messages, max_tokens, timeout)
            if verbose: print('[llm] %s ok %.1fs' % (ch, time.time() - t))
            return text, ch
        except urllib.error.HTTPError as e:
            errs.append('%s:HTTP%s' % (name, e.code))
        except Exception as e:
            errs.append('%s:%s' % (name, type(e).__name__))
        if verbose: print('[llm] %s failed, next' % name)
    # 区分「全未配置」与「配置了但都失败」，便于调用方日志明确
    if not errs and skipped:
        raise AllModelsFailed('all channels skipped (keys not configured): ' + ','.join(skipped))
    raise AllModelsFailed('; '.join(errs) if errs else 'no channels available')

# ============================================================
# v449【事件卡片专用·重型深度思考通道】与盘中快讯极速链严格隔离。
# 由 evolve_events.py / event_incubator.py 调用；盘中快讯仍走上面的 chat()。
# ============================================================

# 用户要求顺序 + 实测可用性(2026-11-19 探测 GEMINI_KEY /v1beta/models)：
#   名单里有 gemini-3.8-flash/3.6-flash/2.5-pro/2.5-flash，但实测：
#     · 3.8/3.6/3.5/3.7-flash → 503 高需求(免费层挤)
#     · 3.1-pro-preview       → 429 额度满
#     · 2.5-pro / 2.5-flash   → 404 no longer available to new users
#     · gemini-3-flash-preview → ✅ 200，thinkingLevel:high 生效(thought tokens=288, 3.8s)
# 故把实测可用的 3-flash-preview 放首位，其余按用户指定顺序兜底，智谱最后兜底。
# v501【用户拍板锁死重型思考模式】用户要求:事件卡每天仅3次、不追速度、
# 严禁降级 glm-4-flash/flash-lite 等快速模型。实测(2026-11-20):
#   · gemini-3.1-pro-preview → 429 额度满；gemini-2.5-pro → 404 新用户不可用
#   · 唯一可用且能深度思考的是 gemini-3-flash-preview(thinkingLevel:high)
# 故最终顺序:智谱 glm-z1-flash(真推理<think>) 主 → Gemini flash-preview 备(开high)。
# 其余 flash/pro 一律不进重型链。
HEAVY_CHAIN = [
    # (名字, 模型, 思考参数构造器) —— 仅留实测可用且开思考的 Gemini 备胎
    ('gemini/gemini-3-flash-preview', 'gemini-3-flash-preview', lambda: {'thinkingLevel': 'high'}),
]

def probe_models():
    """GET /v1beta/models 探测当前 KEY 实际可用模型名单（不写死旧别名）。
    返回 (all_names, heavy_names)；失败返回 ([], []) 不抛错。"""
    key = os.environ.get('GEMINI_KEY', '')
    if not key:
        return [], []
    try:
        req = urllib.request.Request('https://generativelanguage.googleapis.com/v1beta/models?key=' + key,
                                     headers={'User-Agent': 'mp'})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
        names = [m['name'].replace('models/', '') for m in d.get('models', [])]
        heavy = [n for n in names if ('gemini' in n and ('flash' in n or 'pro' in n))]
        return names, heavy
    except Exception:
        return [], []

def _gemini_keys():
    """v507 多Key轮询:兼容 GEMINI_KEY / GEMINI_API_KEY / GEMINI_API_KEYS(逗号分隔)。
    按出现顺序去重,遇到429切下一个Key。绝不打印key明文。"""
    raw = []
    for env in ('GEMINI_KEY', 'GEMINI_API_KEY', 'GEMINI_API_KEYS'):
        v = os.environ.get(env, '') or ''
        for k in v.split(','):
            k = k.strip()
            if k and k not in raw:
                raw.append(k)
    return raw

def _gemini_heavy(model, thinking_cfg, temperature=None):
    """重型 Gemini 单次调用(深度思考,多Key轮询防429)。
    内部按 _gemini_keys() 顺序试:429立即切下一个Key;全部429/失败才抛,
    让上层 _chat_heavy_once 进入60分钟阶梯退避。"""
    def call(messages, max_tokens, timeout):
        keys = _gemini_keys()
        if not keys: raise RuntimeError('no gemini key (GEMINI_KEY/GEMINI_API_KEY/GEMINI_API_KEYS)')
        parts = []
        for m in messages:
            parts.append({'text': m.get('content', '')})
        body = {'contents': [{'role': 'user', 'parts': parts}],
                'generationConfig': {'maxOutputTokens': max_tokens,
                                     'responseMimeType': 'application/json',
                                     'thinkingConfig': thinking_cfg() if callable(thinking_cfg) else thinking_cfg,
                                     **({'temperature': temperature} if temperature is not None else {})}}
        last = None
        for ki, key in enumerate(keys):
            url = GEMINI_BASE + model + ':generateContent?key=' + key
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                headers={'Content-Type': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                last = e
                code = e.code
                # v507 打印具体状态码(区分429限流与503过载),不打印key
                if code == 429:
                    print('[llm-heavy] gemini key#%d HTTP429 限流, 切下一个key (%d/%d)' % (ki+1, ki+1, len(keys)))
                    continue   # 429优先切下一个Key
                if code == 503:
                    print('[llm-heavy] gemini key#%d HTTP503 过载' % (ki+1))
                else:
                    print('[llm-heavy] gemini key#%d HTTP%d' % (ki+1, code))
                continue
            except Exception as e:
                last = e
                print('[llm-heavy] gemini key#%d 异常 %s' % (ki+1, type(e).__name__))
                continue
            # 解析返回
            ps = (d.get('candidates') or [{}])[0].get('content', {}).get('parts', [])
            text = ''
            for pp in ps:
                if pp.get('text') and not pp.get('thought'):
                    text = pp['text']; break
            if not text:
                last = RuntimeError('no text part (thinking only?)')
                continue
            return text, 'gemini/' + model + '(deep)'
        # 全部Key失败:保留最后一个异常的类型与状态码给上层退避判断
        if isinstance(last, urllib.error.HTTPError):
            raise urllib.error.HTTPError(last.url, last.code, 'all %d gemini keys failed: HTTP%d' % (len(keys), last.code), last.headers, last.fp)
        raise RuntimeError('all %d gemini keys failed: %s' % (len(keys), type(last).__name__ if last else 'none'))
    return call

def _chat_heavy_once(messages, max_tokens=2000, timeout=100, verbose=True, temperature=None, allow_zhipu=True, zhipu_first=False, groq_fallback=False):
    """【内部单轮】深度思考 + 重型主力有序降级 + 兜底。
    返回 (text, channel)；全挂抛 AllModelsFailed（调用方回退本地规则）。
    · 探测可用模型名单（GET /v1beta/models），不可用别名不写死在首位。
    · Gemini 3.x → thinkingLevel:high；2.5 → thinkingBudget:8192。
    · 404/429/503 自动跳下一路，绝不中断。
    · zhipu_first=False（默认）：Gemini 主 + 智谱兜底（事件卡 allow_zhipu=False 时纯 Gemini）。
    · zhipu_first=True（v537 用户拍板新卡判定用）：智谱 glm-z1-flash 主 + Gemini 兜底——
      新卡只是给人工看的候选，最终由用户判定要不要加入，智谱质量足够；省下 Gemini 额度。"""
    if isinstance(messages, str):
        messages = [{'role': 'user', 'content': messages}]
    names, heavy = probe_models()
    if verbose:
        print('[llm-heavy] 探测到 %d 模型, 可用重型 %d 个' % (len(names), len(heavy)))
    errs = []
    def _try_zhipu(why):
        if not (_configured('ZHIPU_KEY')): return None
        t = time.time()
        try:
            text, ch = _zhipu('glm-z1-flash', temperature)(messages, max_tokens, timeout)
            import re as _re
            text = _re.sub(r'<think>[\s\S]*?</think>', '', text).strip()
            if verbose: print('[llm-heavy] 智谱 glm-z1-flash(deep) ok %.1fs (%s)' % (time.time() - t, why))
            return text, ch + '(deep-' + why + ')'
        except Exception as e:
            errs.append('zhipu/glm-z1-flash:%s' % type(e).__name__)
            if verbose: print('[llm-heavy] 智谱 %s 失败' % why)
            return None
    def _try_groq_heavy(why):
        if not (os.environ.get('GROQ_KEY') or os.environ.get('GROQ_API_KEY')): return None
        t = time.time()
        try:
            text, ch = _groq('openai/gpt-oss-120b', heavy=True)(messages, max_tokens, timeout)
            if verbose: print('[llm-heavy] %s ok %.1fs (%s)' % (ch, time.time() - t, why))
            return text, ch + '(heavy-' + why + ')'
        except Exception as e:
            errs.append('groq/gpt-oss-120b:%s' % type(e).__name__)
            if verbose: print('[llm-heavy] Groq %s 失败: %s' % (why, type(e).__name__))
            return None
    def _try_gemini(why):
        for name, model, tcfg in HEAVY_CHAIN:
            if not _gemini_keys():
                break
            if heavy and model not in heavy:
                continue
            t = time.time()
            try:
                fn = _gemini_heavy(model, tcfg, temperature)
                text, ch = fn(messages, max_tokens, timeout)
                if verbose: print('[llm-heavy] %s ok %.1fs (deep thinking, %s)' % (ch, time.time() - t, why))
                return text, ch + '(deep-' + why + ')'
            except urllib.error.HTTPError as e:
                errs.append('%s:HTTP%s' % (name, e.code))
                if verbose: print('[llm-heavy] %s %s失败 HTTP%d (全部key试尽)' % (name, why, e.code))
            except Exception as e:
                errs.append('%s:%s' % (name, type(e).__name__))
                if verbose: print('[llm-heavy] %s %s失败 %s (全部key试尽)' % (name, why, type(e).__name__))
        return None
    # v539【用户最终拍板四场景】重型链顺序：
    #   groq_fallback=True  → Gemini 主（全部3个key试尽才失败）+ Groq 重型兜底；
    #                         场景1卡片更新/场景2确认后写卡调此模式，allow_zhipu=False 禁智谱
    #   zhipu_first=True    → 智谱主 + Gemini 兜底（v537 兼容，已无场景使用）
    #   默认                → Gemini 主 + 智谱兜底（兼容旧调用方）
    if groq_fallback:
        r = _try_gemini('primary')
        if r: return r
        r = _try_groq_heavy('fallback')
        if r: return r
        if allow_zhipu:   # 仅当调用方显式允许才用智谱（场景1/2 传 False 禁用）
            r = _try_zhipu('last-resort')
            if r: return r
    elif zhipu_first:
        r = _try_zhipu('primary') if allow_zhipu else None
        if r: return r
        r = _try_gemini('fallback')
        if r: return r
    else:
        r = _try_gemini('primary')
        if r: return r
        if allow_zhipu:
            r = _try_zhipu('fallback')
            if r: return r
    raise AllModelsFailed('; '.join(errs) if errs else 'no heavy channels available')

# v502【用户拍板:双通道全失败不跳过等下一班,阶梯退避重试】
# 接口断联通常短暂:宁可多等,也要把这班更新跑出来。
# 阶梯 2m→5m→10m→15m→20m,累计重试窗口上限60分钟(不含单轮调用自身耗时)。
# 期间任一重型思考模型恢复即刻返回;全程只调 _chat_heavy_once,绝不降级轻量模型。
HEAVY_RETRY_BACKOFFS = (120, 300, 600, 900, 1200)   # 秒
HEAVY_RETRY_TOTAL = 3600                             # 累计等待上限60分钟

def chat_heavy(messages, max_tokens=2000, timeout=100, verbose=True, temperature=None,
               allow_zhipu=True, retry_total=None, zhipu_first=False, groq_fallback=False):
    """事件卡片专用重型思考调用(带阶梯退避重试)。
    双通道全失败时按 2/5/10/15/20 分钟退避重试,累计等待≤60分钟;
    一旦智谱 glm-z1-flash 或 Gemini 思考模型恢复立即返回。绝不降级 glm-4-flash/flash-lite。
    v464：retry_total 可覆盖累计等待上限（秒）——event_incubator 等长跑链路传短值，
    避免 LLM 全挂时睡满60分钟霸占 workflow 并发锁。None=用全局默认3600（兼容其它调用方）。
    v537：zhipu_first=True 时智谱主 + Gemini 兜底（新卡判定用）。
    """
    cap = HEAVY_RETRY_TOTAL if retry_total is None else max(0, int(retry_total))
    waited = 0
    last_err = ''
    for i, backoff in enumerate((0,) + HEAVY_RETRY_BACKOFFS):
        if backoff:
            if waited + backoff > cap:
                if verbose: print('[llm-heavy-retry] 累计等待已达 %.0f 分钟上限,停止重试' % (waited / 60))
                break
            if verbose: print('[llm-heavy-retry] 第%d次退避 %d 分钟 (累计等待 %.0f/%.0f 分钟)' %
                              (i, backoff // 60, (waited + backoff) / 60, cap / 60))
            time.sleep(backoff)
            waited += backoff
        try:
            return _chat_heavy_once(messages, max_tokens=max_tokens, timeout=timeout, verbose=verbose, temperature=temperature, allow_zhipu=allow_zhipu, zhipu_first=zhipu_first, groq_fallback=groq_fallback)
        except AllModelsFailed as e:
            last_err = str(e)
            if verbose: print('[llm-heavy-retry] 本轮双通道全失败: %s' % last_err[:120])
    raise AllModelsFailed(last_err or 'all heavy channels failed after %d rounds (waited %.0f min)' %
                          (len(HEAVY_RETRY_BACKOFFS) + 1, waited / 60))

if __name__ == '__main__':
    print('channels:', status())
    names, heavy = probe_models()
    print('heavy models:', heavy[:12])
    text, ch = chat_heavy('深度思考后只回复：收到', max_tokens=30)
    print('HEAVY RESULT channel=%s text=%s' % (ch, text[:30]))
