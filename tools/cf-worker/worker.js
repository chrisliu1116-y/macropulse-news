/* 宏观脉冲 · 统一加速层（Cloudflare Worker）
 *
 * 路由：
 *   /breadth.json              → insum-cloud 的 breadth.json（缓存 5 分钟）
 *   /sse.json                  → insum-cloud 的 sse.json（缓存 5 分钟）
 *   /h5/<path>                 → macropulse-mirror 仓库静态文件（缓存 1 小时）
 *   /health                    → 健康检查
 *   /__cron                    → 手动 Cron 自检（需 CRON_KEY）
 *   /account/*                 → 账号 / 设置备份（Writers 用 Workers KV，见下）
 * 其它路径一律 404（白名单，避免被当成开放代理滥用）。
 */

const RAW = 'https://raw.githubusercontent.com/chrisliu1116-y/insum-cloud/main/';
const MIRROR = 'https://raw.githubusercontent.com/chrisliu1116-y/macropulse-mirror/main/';

/* 云端独立时钟源：该 GitHub 账号下所有仓库的 Actions schedule 事件均不触发
 * （实测全账号 schedule=0），所以由 Worker 的 Cron Trigger 主动敲 workflow_dispatch。
 * v278 增加「半新半旧」：宽度今日但 sse 旧/缺时立即补跑，不等下一个 Cron 窗口。 */
const GH_DISPATCH = 'https://api.github.com/repos/chrisliu1116-y/insum-cloud/actions/workflows/breadth.yml/dispatches';
/* v432：事件卡片每日态势演进的 dispatch（北京 08:00/20:00 = UTC 00:00/12:00） */
const GH_EVOLVE_DISPATCH = 'https://api.github.com/repos/chrisliu1116-y/insum-cloud/actions/workflows/evolve-events.yml/dispatches';

async function triggerEvolve(env) {
  if (!env.GH_TOKEN) return 0;
  try {
    const r = await fetch(GH_EVOLVE_DISPATCH, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + env.GH_TOKEN,
        'Accept': 'application/vnd.github+json', 'User-Agent': 'macropulse-edge',
        'Content-Type': 'application/json' },
      body: JSON.stringify({ ref: 'main', inputs: { mode: 'full' } })
    });
    return r.status;
  } catch (e) { return 0; }
}

/* ============================================================
 * v446【CF 边缘 2 分钟快讯增量驱动】
 * 每 2 分钟：抓全量可用源(非东财) → 无新增短路 → 智谱微批分级 → 写 KV mp_news_all。
 * 严守东财红线：这里绝不请求 eastmoney 任何域名（东财跑批在 GHA 独立出口）。
 * 前端读 Pages /__news（KV 成品），异步覆盖，不等待本任务。
 * ============================================================ */
const NEWS_KV_KEY = 'mp_news_all';
const NEWS_FP_KEY = 'mp_news_fingerprint';

// 抓新浪 7×24（JSONP→JSON）。非东财。
async function fetchSinaEdge() {
  const url = 'https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=30&zhibo_id=152&tag_id=0&dire=f&dpc=1';
  const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 12000);
  try {
  const r = await fetch(url, { signal: ctl.signal, headers: { 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/7x24/' }, cf: { cacheTtl: 0 } });
  const raw = await r.text();
  const m = raw.match(/\(\s*(\{[\s\S]*\})\s*\)/);
  const d = JSON.parse(m ? m[1] : raw);
  const feed = (((d || {}).result || {}).data || {}).feed || {};
  return ((feed.list) || []).map(it => {
    const text = String(it.rich_text || it.content || '').replace(/<[^>]+>/g, '').trim();
    if (!text) return null;
    const mm = text.match(/^【(.+?)】\s*([\s\S]*)$/);
    return { id: 'sn-' + (it.id || ''), title: (mm ? mm[1] : text).trim().slice(0, 60),
      brief: (mm ? mm[2] : '').trim().slice(0, 200), ts: it.create_time || '', src: '新浪财经' };
  }).filter(Boolean);
  } finally { clearTimeout(tm); }
}

// 抓同花顺 7×24（CORS 全开，JSON）。非东财。
async function fetchTHSEdge() {
  const ctl = new AbortController(); const tm = setTimeout(() => ctl.abort(), 12000);
  try {
  const r = await fetch('https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize=30',
    { signal: ctl.signal, headers: { 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://news.10jqka.com.cn/' }, cf: { cacheTtl: 0 } });
  const d = await r.json();
  const list = (((d || {}).data || {}).list) || d.data || [];
  return (Array.isArray(list) ? list : []).map(it => ({
    id: 'ths-' + (it.id || Math.random().toString(36).slice(2)),
    title: String(it.title || '').trim().slice(0, 60),
    brief: String(it.digest || '').trim().slice(0, 200), ts: it.ctime || it.rtime || '', src: '同花顺',
  })).filter(x => x.title);
  } finally { clearTimeout(tm); }
}

function parseTs(s) {
  const str = String(s || '').trim();
  let t = /^\d+$/.test(str) ? (str.length > 11 ? +str : +str * 1000)
    : Date.parse(str.replace(/-/g, '/'));
  return isFinite(t) && t > 0 ? t : Date.now();
}

// 智谱微批分级（JSON 输出）：复用 prompt_rules 快照核心，ID 锚定，S/A/B/C。
async function llmGradeBatch(items, key, rules) {
  const batch = items.map((it, i) => ({ id: i, title: it.title, brief: it.brief }));
  const sys = '你是快讯审核编辑。用规则给每条快讯判级。只输出JSON数组，不要markdown。\n'
    + (rules || '').slice(0, 6000)
    + '\n每条格式：{"id":0,"level":"S/A/B/C","category":"简短分类","reason":"≤15字"}'
    + '\nid必须原样回传，理由只针对本条。S只认已发生重大事件，纯行情波动=最B或C。';
  // v448：24条微批实测本地16.5s、CF边缘更慢，20s太紧会abort→null。放宽到45s。
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 45000);
  try {
    const r = await fetch('https://open.bigmodel.cn/api/paas/v4/chat/completions', {
      method: 'POST', signal: ctrl.signal,
      headers: { 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'glm-4-flash-250414',
        messages: [{ role: 'system', content: sys }, { role: 'user', content: JSON.stringify(batch) }],
        max_tokens: 1200, thinking: { type: 'disabled' } }),
    });
    const d = await r.json();
    const txt = ((d.choices || [])[0] || {}).message?.content || '';
    const m = txt.match(/\[[\s\S]*\]/);
    if (!m) return null;
    return JSON.parse(m[0]);
  } catch (e) { console.log('[news-cron] LLM 分级失败: ' + String(e)); return null; }
  finally { clearTimeout(timer); }
}

async function driveNewsCron(env) {
  if (!env.ACCT_KV) { console.log('[news-cron] KV 未绑定，跳过'); return; }
  const t0 = Date.now();
  const log = [];
  const saveLog = async (msg) => {
    log.push(msg);
    try { await env.ACCT_KV.put('mp_news_cronlog', JSON.stringify({ ts: new Date().toISOString(), lines: log })); } catch (e) {}
  };
  await saveLog('start cron=' + (env.__cron || ''));
  let raw = [];
  try { const s = await fetchSinaEdge(); raw = raw.concat(s); await saveLog('sina=' + s.length); }
  catch (e) { await saveLog('sina ERR ' + String(e)); }
  try { const t = await fetchTHSEdge(); raw = raw.concat(t); await saveLog('ths=' + t.length); }
  catch (e) { await saveLog('ths ERR ' + String(e)); }
  if (!raw.length) { await saveLog('无源可用短路'); return; }
  // 去重（按 id）
  const seen = new Set();
  raw = raw.filter(x => { if (seen.has(x.id)) return false; seen.add(x.id); return true; });
  // 无新增短路：指纹 = 前20条 id 拼接
  const fp = raw.slice(0, 20).map(x => x.id).join('|');
  const oldFp = await env.ACCT_KV.get(NEWS_FP_KEY);
  const hasProduct = await env.ACCT_KV.get(NEWS_KV_KEY);
  await saveLog('fp=' + fp.slice(0, 40) + ' old=' + (oldFp || '').slice(0, 40) + ' hasProduct=' + !!hasProduct);
  // 无新增短路：仅当已有产物且指纹相同时才跳过。首次(无产物)必须强制写一次。
  if (oldFp === fp && hasProduct) { await saveLog('无新增短路'); return; }
  const key = env.ZHIPU_KEY;   // 只用 Worker secret，KV 不存明文 key
  if (!key) { await saveLog('无ZHIPU_KEY(secret)'); return; }
  await saveLog('有key,开始LLM');
  const rules = await env.ACCT_KV.get('mp_prompt_rules') || '';
  // v448：取最近 12 条微批分级（实测24条15.5s、小批1.4s，12条约7s）。
  // 前端首屏可见≈10-15条，12条足够覆盖；条数越少 LLM 越快，端到端延迟越低。
  const target = raw.slice(0, 12);
  const graded = await llmGradeBatch(target, key, rules);
  await saveLog('LLM结果=' + (graded ? graded.length : 'null'));
  // 【全断红灯状态】LLM 失败 = 降级本地规则；记录状态跃迁并触发 webhook（仅 up→down 一次）
  try {
    const prev = JSON.parse((await env.ACCT_KV.get('mp_llm_status')) || '{"down":false}');
    const down = !graded;
    if (down !== !!prev.down) {
      await env.ACCT_KV.put('mp_llm_status', JSON.stringify({ down, ts: Date.now(),
        reason: down ? '智谱+Google全部失效，回退本地规则' : 'LLM已恢复' }));
      const hook = env.ALERT_WEBHOOK_URL;
      if (hook && down) {
        try { await fetch(hook, { method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: '🚨 MACRO PULSE：AI已断开·规则兜底（智谱+Google全失效，事件卡片与快讯分级暂用本地规则）', ts: Date.now() }) }); }
        catch (e) { console.log('[news-cron] webhook失败: ' + e); }
      }
      await saveLog('LLM状态跃迁 down=' + down);
    }
  } catch (e) { await saveLog('状态写入异常 ' + e); }
  if (!graded) { await saveLog('LLM无结果保留旧产物'); return; }
  const byId = {};
  graded.forEach(g => { byId[g.id] = g; });
  const items = target.map((it, i) => {
    const g = byId[i] || {};
    const lv = 'SABC'.includes(g.level) ? g.level : 'B';
    return { ...it, ts: parseTs(it.ts), level: lv, category: g.category || '快讯', reason: g.reason || '' };
  });
  await env.ACCT_KV.put(NEWS_KV_KEY, JSON.stringify({
    generated: new Date(Date.now() + 8 * 3600e3).toISOString().replace('T', ' ').slice(0, 19),
    src: '新浪财经+同花顺', engine: 'CF边缘·智谱微批', items,
  }));
  await env.ACCT_KV.put(NEWS_FP_KEY, fp);
  await saveLog('写入完成 n=' + items.length + ' ms=' + (Date.now() - t0));
}

function bjToday() {
  return new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10);
}

/** 'ok' 都已发布 / 'half' 宽度新 sse 旧 / 'stale' 宽度未更新。
 *  探测失败一律按 'stale' → 照常 dispatch，宁可多跑不可漏跑。 */
async function todayPublished() {
  try {
    const r = await fetch(RAW + 'breadth/data/breadth.json?t=' + Date.now(),
      { cf: { cacheTtl: 0 } });
    if (!r.ok) return 'stale';
    const txt = await r.text();
    const i = txt.indexOf('"trade_date":"');
    if (i < 0 || txt.slice(i + 14, i + 24) !== bjToday()) return 'stale';
    try {
      const s = await fetch(RAW + 'breadth/data/sse.json?t=' + Date.now(),
        { cf: { cacheTtl: 0 } });
      if (!s.ok) return 'half';
      const st = await s.text();
      const j = st.indexOf('"to":"');
      if (j < 0 || st.slice(j + 6, j + 16) !== bjToday()) return 'half';
      return 'ok';
    } catch (e) { return 'half'; }
  } catch (e) {
    return 'stale';
  }
}

async function triggerRun(env, recheck) {
  if (!env.GH_TOKEN) return 0;
  /* v291：recheck 轮让 workflow 走 --recheck（忽略幂等闸、覆盖当日行），
   * 用于收盘后 15:30 / 15:40 / 16:00 的清算修正复核。 */
  const body = JSON.stringify({ ref: 'main',
    inputs: { no_wait: 'false', recheck: recheck ? 'true' : 'false' } });
  let status = 0;
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(GH_DISPATCH, {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + env.GH_TOKEN,
          'Accept': 'application/vnd.github+json',
          'User-Agent': 'MacroPulse-Edge/2.0',
          'Content-Type': 'application/json'
        },
        body: body
      });
      status = r.status;
      if (r.ok) return status;
      if (status >= 400 && status < 500 && status !== 429) return status;
    } catch (e) { status = -1; }
    if (i < 2) await new Promise(z => setTimeout(z, 1500 * (i + 1)));
  }
  return status;
}

/* 前端拉 breadth/sse 时顺带探测一次并立即补跑，不等下一个固定 Cron 窗口。
 * 模块级 10 分钟节流，避免每个请求都打 GitHub RAW / dispatch。 */
let _lastHalfRepair = 0;
async function halfRepair(env) {
  const now = Date.now();
  if (now - _lastHalfRepair < 600e3) return;
  _lastHalfRepair = now;
  try {
    if (await todayPublished() === 'half') {
      console.log('[fetch] 半新半旧：宽度今日但 sse 旧/缺，立即补跑（不等 Cron 窗口）');
      await triggerRun(env);
    }
  } catch (e) { /* 探测/补跑失败不影响正常代理响应 */ }
}

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
  'Access-Control-Max-Age': '86400',
  'Vary': 'Origin'
};

function reply(resp, seconds, req) {
  const r = new Response(resp.body, resp);
  Object.entries(CORS).forEach(([k, v]) => r.headers.set(k, v));
  r.headers.set('Cache-Control', 'public, max-age=' + seconds + ', s-maxage=' + seconds);
  r.headers.set('X-MP-Edge', 'cf-worker');
  return r;
}

async function proxy(origin, path, seconds, req) {
  const url = origin + path;
  const cache = caches.default;
  const key = new Request(url, { method: 'GET' });
  const hit = await cache.match(key);
  if (hit && req.method !== 'HEAD') return reply(hit, seconds, req);
  const upstream = await fetch(url, {
    method: 'GET',
    headers: { 'User-Agent': 'MacroPulse-Edge/1.0' },
    cf: { cacheTtl: seconds, cacheEverything: true }
  });
  if (!upstream.ok) return new Response('upstream ' + upstream.status, { status: 502, headers: CORS });
  const out = reply(upstream, seconds, req);
  if (upstream.headers.get('content-length') === null ||
      Number(upstream.headers.get('content-length')) < 2 * 1024 * 1024) {
    await cache.put(key, out.clone());
  }
  return out;
}

/* ============================================================
 * 账号 / 设置备份代理（v6）
 * APP 前端绝不接触任何 token：GitHub PAT 只在 Worker Secret 里（GH_TOKEN / GH_PAT，
 * 供 Cron dispatch 用）。账号数据存在 Workers KV（ACCT_KV）里 —— 该 GitHub PAT
 * 实测没有 Gist 写权限（create 403），且账号下两个仓库都是 public，
 * 不能把凭据放进公开仓库，因此改用 CF 原生的 KV 作为备选通道存储。
 * GitHub 备份通道做成「可配置开关（GH_BACKUP_ENABLED）+ 未启用健康状态」：
 * 未拿到可写 token 前 /account/health 如实返回 github.enabled=false，绝不假装可用。
 * 前端只调下面 5 个白名单路由（契约不变），请求体有大小与字段校验。
 * ============================================================ */
const ACCT_CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Access-Control-Max-Age': '86400',
  'Vary': 'Origin'
};

function aj(obj, status) {
  return new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { 'Content-Type': 'application/json; charset=utf-8',
               'Cache-Control': 'no-store', ...ACCT_CORS }
  });
}

function ghPat(env) { return env.GH_TOKEN || env.GH_PAT || ''; }

async function kvJson(env, key, fallback) {
  if (!env.ACCT_KV) return fallback;
  const raw = await env.ACCT_KV.get(key);
  if (!raw) return fallback;
  try { return JSON.parse(raw); } catch (e) { return fallback; }
}

async function handleAccount(req, env, path) {
  if (path === '/account/health') {
    /* GitHub 备份通道：默认「未启用」，绝不假装可用。
     * 现有 PAT 无 Gist 写权限（POST /gists 403）且两仓库均 public，不能落盘账号凭据。
     * 要启用需用户先提供其一并配置 GH_BACKUP_ENABLED：
     *   classic PAT 的 gist scope；fine-grained 的 Gists: Read and write；
     *   私有仓的 Contents: Read and write。
     * 保持既有 ok/gh/kv/ts 字段不变，仅新增 github 状态对象。 */
    const ghEnabled = !!env.GH_BACKUP_ENABLED;
    return aj({
      ok: true,
      gh: !!ghPat(env),
      kv: !!env.ACCT_KV,
      github: {
        enabled: ghEnabled,
        writable: ghEnabled && !!ghPat(env),
        reason: ghEnabled ? '' : '未启用：需 classic gist / fine-grained Gists:Read+Write / 私有仓 Contents:Read+Write 的可写 token，并配置 GH_BACKUP_ENABLED'
      },
      ts: Date.now()
    });
  }
  if (!env.ACCT_KV) return aj({ error: '备份通道未配置（KV 未绑定）' }, 503);
  if (!['/account/register', '/account/login', '/account/backup', '/account/restore'].includes(path)) {
    return aj({ error: 'not found' }, 404);
  }
  let body = {};
  if (req.method === 'POST') {
    const raw = await req.text();
    if (raw.length > 512 * 1024) return aj({ error: 'payload too large' }, 413);
    try { body = JSON.parse(raw || '{}'); } catch (e) { return aj({ error: 'invalid json' }, 400); }
  } else if (req.method !== 'GET') {
    return aj({ error: 'method not allowed' }, 405);
  }
  const acct = String(body.account || '');
  if (acct && !/^[A-Za-z0-9_.@-]{3,64}$/.test(acct)) return aj({ error: 'invalid account' }, 400);
  try {
    if (path === '/account/register') {
      if (!acct) return aj({ error: 'account required' }, 400);
      const key = 'acct:' + acct;
      if (await env.ACCT_KV.get(key)) return aj({ error: '账号已存在' }, 409);
      await env.ACCT_KV.put(key, JSON.stringify(
        Object.assign({}, body, { updated: new Date().toISOString() })));
      return aj({ ok: true, account: acct });
    }
    if (path === '/account/login') {
      if (!acct) return aj({ error: 'account required' }, 400);
      const c = await kvJson(env, 'acct:' + acct, null);
      if (!c) return aj({ error: '账号不存在' }, 404);
      return aj({ cred: c });
    }
    if (path === '/account/backup') {
      if (!body.snapshot || typeof body.snapshot !== 'object') {
        return aj({ error: 'snapshot required' }, 400);
      }
      await env.ACCT_KV.put('conf:global', JSON.stringify(body.snapshot));
      return aj({ ok: true, updated: new Date().toISOString() });
    }
    if (path === '/account/restore') {
      return aj({ snapshot: (await kvJson(env, 'conf:global', {})) || {} });
    }
  } catch (e) {
    return aj({ error: '备份通道异常：' + (e && e.message ? e.message : String(e)) }, 502);
  }
  return aj({ error: 'not found' }, 404);
}

/* v301【浏览器/PWA 代理】api.js 走 /__proxy?u=<url>，news 模块走 /__proxy/<key>/<b64>。
 * 线上静态托管没有 server.py，这两个路径此前 404 —— 浏览器端分时/K线全空。
 * Worker 天然能 fetch，这里补代理路由（只放行行情/新闻源白名单，避免被当开放代理）。 */
/* v440：加快讯源白名单（实时分级链路）；东财绝不经此(红线,按端点限流)。 */
const PROXY_ALLOW = /(eastmoney\.com|gtimg\.cn|qq\.com|sinajs\.cn|sina\.com\.cn|sina\.cn|yahoo\.com|binance\.com|twse\.com\.tw|naver\.com|tradingview\.com|cboe\.com|cnbc\.com|nasdaq\.com|hyperliquid\.xyz|dexscreener\.com|jin10\.com|cls\.cn|10jqka\.com\.cn|wallstreetcn\.com|ithome\.com)/i;
async function handleProxy(req, u) {
  let target = '';
  if (u.pathname === '/__proxy') {
    target = u.searchParams.get('u') || '';
  } else {
    const parts = u.pathname.split('/').filter(Boolean);
    if (parts.length >= 3) {
      let b = parts[parts.length - 1].replace(/-/g, '+').replace(/_/g, '/');
      while (b.length % 4) b += '=';
      try { target = atob(b); } catch (e) { target = ''; }
    }
  }
  if (!target || !/^https?:\/\//i.test(target)) {
    return new Response('bad target', { status: 400, headers: CORS });
  }
  if (!PROXY_ALLOW.test(target)) {
    return new Response('forbidden host', { status: 403, headers: CORS });
  }
  const ref = /eastmoney/.test(target) ? 'https://quote.eastmoney.com/'
    : /qq\.com|gtimg/.test(target) ? 'https://gu.qq.com/'
    : /sina/.test(target) ? 'https://finance.sina.com.cn/'
    : /yahoo/.test(target) ? 'https://finance.yahoo.com/'
    : 'https://www.google.com/';
  try {
    const r = await fetch(target, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36',
        'Referer': ref,
        'Accept': '*/*'
      },
      cf: { cacheTtl: 5, cacheEverything: false }
    });
    const body = await r.arrayBuffer();
    return new Response(body, {
      status: r.status,
      headers: {
        ...CORS,
        'Content-Type': r.headers.get('content-type') || 'application/json; charset=utf-8',
        'Cache-Control': 'no-store'
      }
    });
  } catch (e) {
    return new Response('upstream error: ' + String(e && e.message || e), { status: 502, headers: CORS });
  }
}

export default {
  async fetch(req, env, ctx) {
    const u = new URL(req.url);
    if (req.method === 'OPTIONS') {
      return new Response(null, { status: 204,
        headers: u.pathname.startsWith('/account/') ? ACCT_CORS : CORS });
    }
    if (u.pathname.startsWith('/account/')) return handleAccount(req, env, u.pathname);
    if (u.pathname === '/health') {
      return new Response(JSON.stringify({ ok: true, ts: Date.now() }),
        { headers: { 'Content-Type': 'application/json', ...CORS } });
    }
    /* v440：规则快照边缘读取——LLM 实时链路从 CF KV 拿最新判级/去重规则，
     * 用户改文档 push 后由 GHA 自动写 KV，边缘零延迟生效。 */
    if (u.pathname === '/rules') {
      const rules = await env.ACCT_KV.get('mp_prompt_rules');
      if (!rules) return new Response('rules not synced', { status: 404, headers: CORS });
      return new Response(rules, { headers: { 'Content-Type': 'text/plain; charset=utf-8',
        'Cache-Control': 'no-store', ...CORS } });
    }
    if (u.pathname === '/__cron') {
      if (!env.CRON_KEY || req.headers.get('X-MP-Key') !== env.CRON_KEY) {
        return new Response('forbidden', { status: 403, headers: CORS });
      }
      const status = await triggerRun(env);
      return new Response(JSON.stringify({ dispatch: status, ts: Date.now() }),
        { headers: { 'Content-Type': 'application/json', ...CORS } });
    }
    /* v446：手动触发快讯驱动（排障用，需 CRON_KEY）。 */
    if (u.pathname === '/__drivenews') {
      if (!env.CRON_KEY || req.headers.get('X-MP-Key') !== env.CRON_KEY) {
        return new Response('forbidden', { status: 403, headers: CORS });
      }
      const t0 = Date.now();
      try {
        let probe = {};
        try { probe.sina = (await fetchSinaEdge()).length; } catch (e) { probe.sinaErr = String(e); }
        try { probe.ths = (await fetchTHSEdge()).length; } catch (e) { probe.thsErr = String(e); }
        await driveNewsCron(env);
        const raw = await env.ACCT_KV.get(NEWS_KV_KEY);
        return new Response(JSON.stringify({ probe, kvWritten: !!raw, ms: Date.now() - t0,
          n: raw ? (JSON.parse(raw).items || []).length : 0 }),
          { headers: { 'Content-Type': 'application/json', ...CORS } });
      } catch (e) {
        return new Response(JSON.stringify({ error: String(e && e.stack || e), ms: Date.now() - t0 }),
          { status: 500, headers: { 'Content-Type': 'application/json', ...CORS } });
      }
    }
    if (u.pathname === '/breadth.json' || u.pathname === '/sse.json') {
      ctx.waitUntil(halfRepair(env));
      return proxy(RAW, 'breadth/data/' + u.pathname.slice(1), 300, req);
    }
    if (u.pathname === '/__proxy' || u.pathname.startsWith('/__proxy/')) {
      return handleProxy(req, u);
    }
    if (u.pathname.startsWith('/h5/')) {
      const p = u.pathname.slice(4).replace(/^\/+/, '');
      if (!/\.(html|js|css|json|png|svg|webmanifest|ico)$/.test(p) || p.indexOf('..') >= 0) {
        return new Response('forbidden', { status: 403, headers: CORS });
      }
      return proxy(MIRROR, p + (u.search || ''), 3600, req);
    }
    return new Response('not found', { status: 404, headers: CORS });
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil((async () => {
      /* v446【CF 边缘 2 分钟快讯驱动】独立 cron，与 breadth/evolve 完全解耦。 */
      if (event && (event.cron === '*/1 * * * *' || event.cron === '*/2 * * * *')) {
        try { await driveNewsCron(env); } catch (e) { console.log('[news-cron] 异常: ' + e); }
        return;
      }
      /* v447【事件卡每日演进 · 黄金窗口】UTC 23:30（京07:30 早盘班）/ UTC 12:45（京20:45 晚盘班）。
       * 用 event.cron 精确定点（不再靠 utcH 窗口猜），KV 记录当天两班避免重复。
       * 早盘收割隔夜美股+美联储+欧美夜盘；晚盘覆盖20:30美国CPI/非农+白天亚太。
       * 建卡窗口=过去24h游离S/核心A，挂在演进后由 evolve-events workflow 内执行。 */
      const EVOLVE_CRONS = { '30 23 * * *': 'am', '45 12 * * *': 'pm' };
      const evolveSlot = event && EVOLVE_CRONS[event.cron];
      if (evolveSlot) {
        const day = new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10);
        const slot = day + '-' + evolveSlot;
        try {
          if (env.ACCT_KV && await env.ACCT_KV.get('mp_evolve_' + slot)) {
            console.log('[cron] 卡片演进当日' + evolveSlot + '班已触发，跳过');
            return;
          }
          const es = await triggerEvolve(env);
          console.log('[cron] 卡片演进 ' + evolveSlot + '班 dispatch status=' + es);
          if (env.ACCT_KV && (es >= 200 && es < 300)) {
            await env.ACCT_KV.put('mp_evolve_' + slot, String(Date.now()));
          }
        } catch (e) { console.log('[cron] 卡片演进触发异常: ' + e); }
        return;   // 卡片时段不跑 breadth 逻辑
      }
      /* v296【去掉 16:00 强制复核】A股收盘价由 15:00 收盘集合竞价确定，之后不再变；
       * 原 v341 的「16:00 无条件 recheck」对 A股宽度没有价值，只会让每天必然多跑
       * 一轮全量（5584 只 × 1 次 K 线）。现所有时点统一走 todayPublished()：
       * 15:00 起每 5 分钟一轮、有数据就停，16:00 只作为普通兜底时点
       * （当天还没发布才会真跑）。 */
      const state = await todayPublished();
      if (state === 'ok') {
        console.log('[cron] 宽度 + 上证都是今日已发布，跳过 dispatch');
        return;
      }
      /* v361【失败退避 4min → 15min】schedules 仍是 15:00–15:55 每 5 分钟
       * 触发 + 16:00 兜底，但只有距上次真 dispatch ≥15 分钟才会再发一轮。
       * 正常情况第 1 轮跑出数据后 todayPublished() 直接停（1 轮/天）；
       * 只有持续失败才会走到 15:00/15:15/15:30/15:45/16:00 最多 5 轮，
       * 避免 clist 部分可用时 13 轮 × 数千请求把东财打到硬封。
       * 初衷不变：失败继续兜底、成功立即停。 */
      try {
        if (env.ACCT_KV) {
          const last = +(await env.ACCT_KV.get('mp_last_breadth_dispatch') || 0);
          if (last && Date.now() - last < 15 * 60e3) {
            console.log('[cron] 冷却中（距上次 dispatch <15min），跳过');
            return;
          }
          await env.ACCT_KV.put('mp_last_breadth_dispatch', String(Date.now()));
        }
      } catch (e) { /* KV 异常不阻塞 dispatch */ }
      if (state === 'half') {
        console.log('[cron] 半新半旧：宽度今日但 sse 旧/缺，立即补跑');
        const s = await triggerRun(env);
        if (!(s >= 200 && s < 300)) console.log('[cron] 半新半旧补跑 dispatch failed status=' + s);
        return;
      }
      const s = await triggerRun(env);
      if (!(s >= 200 && s < 300)) console.log('[cron] dispatch failed status=' + s);
    })());
  }
};
