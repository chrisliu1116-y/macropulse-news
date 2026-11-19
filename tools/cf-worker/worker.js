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
// v456：卡片/新闻归属 macropulse-news 仓库（按项目分仓，不再混在 insum-cloud 跑批仓库）
const GH_EVOLVE_DISPATCH = 'https://api.github.com/repos/chrisliu1116-y/macropulse-news/actions/workflows/evolve-events.yml/dispatches';

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

/*__NEWS_SOURCES__*/   // v452：部署时由 deploy_api.py 内联 news-sources.js（10源云端抓取）

function parseTs(s) {
  const str = String(s || '').trim();
  let t = /^\d+$/.test(str) ? (str.length > 11 ? +str : +str * 1000)
    : Date.parse(str.replace(/-/g, '/'));
  return isFinite(t) && t > 0 ? t : Date.now();
}

/* v450【双通道微批分级】智谱(主,快) → Gemini(备)，两路都失败才返回 null(才算全断)。
 * 修 v449 实现偏差：之前快讯链路只用了智谱一个源，智谱一挂就误报"AI已断开"。
 * 现与卡片链路一致：智谱+Gemini 有序降级，只有两个都挂才红灯/本地兜底。
 * 每次失败记详细原因到 log，供排查智谱频繁失败的根因。 */
async function llmGradeBatch(items, zhipuKey, geminiKey, rules, logFn, force) {
  const note = logFn || (() => {});
  const batch = items.map((it, i) => ({ id: i, title: it.title, brief: it.brief }));
  const sys = '你是快讯审核编辑。用规则给每条快讯判级。只输出JSON数组，不要markdown。\n'
    + (rules || '').slice(0, 6000)
    + '\n每条格式：{"id":0,"level":"S/A/B/C","category":"简短分类","reason":"≤15字"}'
    + '\nid必须原样回传，理由只针对本条。S只认已发生重大事件，纯行情波动=最B或C。';
  const userTxt = JSON.stringify(batch);

  // 通道1：智谱 glm-4-flash（主，实测最快）。v453：429/5xx 退避2s重试一次（军令状）
  async function viaZhipu() {
    if (!zhipuKey) throw new Error('no ZHIPU_KEY');
    for (let attempt = 0; attempt < 2; attempt++) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 45000);
      try {
        const r = await fetch('https://open.bigmodel.cn/api/paas/v4/chat/completions', {
          method: 'POST', signal: ctrl.signal,
          headers: { 'Authorization': 'Bearer ' + zhipuKey, 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: 'glm-4-flash-250414',
            messages: [{ role: 'system', content: sys }, { role: 'user', content: userTxt }],
            max_tokens: 1200, thinking: { type: 'disabled' } }),
        });
        const txt = await r.text();
        // 429 或 5xx：退避后重试一次；其它错误直接抛出（交给 Gemini 兜底）
        if (r.status === 429 || r.status >= 500) {
          if (attempt === 0) { note('智谱HTTP' + r.status + ' 退避2s重试'); await new Promise(ok => setTimeout(ok, 2000)); continue; }
          throw new Error('HTTP ' + r.status + ' ' + txt.slice(0, 100));
        }
        if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + txt.slice(0, 100));
        const d = JSON.parse(txt);
        const content = ((d.choices || [])[0] || {}).message?.content || '';
        const m = content.match(/\[[\s\S]*\]/);
        if (!m) throw new Error('no JSON array in reply: ' + content.slice(0, 80));
        return { result: JSON.parse(m[0]), channel: 'zhipu' };
      } finally { clearTimeout(timer); }
    }
    throw new Error('zhipu exhausted');
  }

  // 通道2：Gemini 3.1 flash lite（备，带 thinkingLevel:minimal 防深度思考）
  async function viaGemini() {
    if (!geminiKey) throw new Error('no GEMINI_KEY');
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 45000);
    try {
      const body = { contents: [{ role: 'user', parts: [{ text: sys + '\n\n' + userTxt }] }],
        generationConfig: { maxOutputTokens: 1200, thinkingConfig: { thinkingLevel: 'minimal' } } };
      const url = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key=' + geminiKey;
      const r = await fetch(url, { method: 'POST', signal: ctrl.signal,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const txt = await r.text();
      if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + txt.slice(0, 100));
      const d = JSON.parse(txt);
      const content = d.candidates?.[0]?.content?.parts?.[0]?.text || '';
      const m = content.match(/\[[\s\S]*\]/);
      if (!m) throw new Error('no JSON array in reply: ' + content.slice(0, 80));
      return { result: JSON.parse(m[0]), channel: 'gemini' };
    } finally { clearTimeout(timer); }
  }

  // 有序降级：智谱失败→Gemini；两路都失败→返回 null
  // force='gemini' 用于强制验收兜底通道（不写KV，仅测试端点用）
  if (force === 'gemini') {
    const out = await viaGemini();
    note('LLM通道=gemini(强制验收)');
    return out;
  }
  try {
    const out = await viaZhipu();
    note('LLM通道=zhipu');
    return out;
  } catch (e1) {
    note('智谱失败:' + String(e1.message || e1).slice(0, 90));
    try {
      const out = await viaGemini();
      note('LLM通道=gemini(智谱兜底)');
      return out;
    } catch (e2) {
      note('Gemini也失败:' + String(e2.message || e2).slice(0, 90));
      return null;   // 两路全断
    }
  }
}

async function driveNewsCron(env) {
  if (!env.ACCT_KV) { console.log('[news-cron] KV 未绑定，跳过'); return; }
  const t0 = Date.now();
  const log = [];
  /* v457【省写卫生】saveLog 只累积内存，不每次写 KV（原来一轮写10+次）。
   * 只在真正写产物时 flushLog 一次；无新增短路时也可用 ?debug 强制看日志，
   * 但默认不写 → 大幅降低 KV 写入（免费版 1000次/天硬上限）。 */
  const saveLog = (msg) => { log.push(msg); };
  const flushLog = async () => {
    try { await env.ACCT_KV.put('mp_news_cronlog', JSON.stringify({ ts: new Date().toISOString(), lines: log })); } catch (e) {}
  };
  saveLog('start cron=' + (env.__cron || ''));
  /* v452【10源云端抓取】Worker 海外IP直接抓全部10源，不再依赖前端。
   * 前端关闭也不影响云端持续攒池。源级容错：单源失败不影响其余。 */
  let raw = [], srcDetail = [], okCount = 0;
  try {
    const res = await fetchAllCloudSources();
    raw = res.items; srcDetail = res.detail; okCount = res.okCount;
    await saveLog('10源抓到=' + raw.length + ' 成功源=' + okCount + '/10');
    await saveLog('源明细:' + srcDetail.slice(0, 10).join(' '));
  } catch (e) { await saveLog('10源抓取异常 ' + String(e)); }
  if (!raw.length) { await saveLog('10源全空，短路'); return; }
  /* v453【双轨独立】轨道B的 raw 只来自 Worker 自己云端抓的10源，
   * 绝不消费前端 ingest 队列（前端已彻底不上报，零耦合）。 */
  if (!raw.length) { await saveLog('无源可用短路'); return; }
  // 去重（按 id）
  const seen = new Set();
  raw = raw.filter(x => { if (seen.has(x.id)) return false; seen.add(x.id); return true; });
  /* v449【纯增量 + 60条滚动池】读旧池子 → 只挑池里没有的新条目送 LLM。
   * 池子按 id 索引，保留最近 60 条已分级新闻。无新增 → 0 调用直接短路。 */
  const POOL_KEY = 'mp_news_pool';
  // v451：扩容至 1000 条（覆盖24h全量），保留 24 小时内的，超期按 TTL 淘汰
  const POOL_MAX = 1000;
  const POOL_TTL_MS = 24 * 3600 * 1000;
  let pool = [];
  try { const p = await env.ACCT_KV.get(POOL_KEY); if (p) pool = JSON.parse(p); } catch (e) { pool = []; }
  /* v455【一次性净化】清掉时区bug(UTC当北京)留下的未来时间条目——这些脏数据
   * ts 在 8 小时后，按倒序截断时永远排最前，把正确时间的新数据挤掉。
   * 过滤条件：ts 超过当前时间 5 分钟即判定污染。 */
  const nowMs = Date.now();
  const cleanPool = pool.filter(x => (x.ts || 0) <= nowMs + 5 * 60 * 1000);
  if (cleanPool.length !== pool.length) {
    await saveLog('净化未来时间脏数据 ' + (pool.length - cleanPool.length) + '条');
    pool = cleanPool;
  }
  await saveLog('池子=' + pool.length + '条');
  const poolIds = new Set(pool.map(x => x.id));
  const freshAll = raw.filter(x => !poolIds.has(x.id));
  const fresh = freshAll.slice(0, 40);   // v453：放宽至40条/轮，加快24h存量攒池（军令状）
  await saveLog('fresh前5=' + fresh.slice(0, 5).map(x => x.id).join(',') + ' 总新增=' + freshAll.length);
  if (!fresh.length && pool.length) { await saveLog('无新增,0调用短路'); return; }
  const zhipuKey = env.ZHIPU_KEY, geminiKey = env.GEMINI_KEY;   // Worker secret，KV 不存明文 key
  if (!zhipuKey && !geminiKey) { await saveLog('无LLM key(secret)'); return; }
  const rules = await env.ACCT_KV.get('mp_prompt_rules') || '';
  // 首次(池空)取前12条建档；之后只送真正新增的 1~4 条 → LLM 2s 内
  const target = fresh.length ? fresh : raw.slice(0, 12);
  await saveLog('送LLM=' + target.length + '条(纯增量)');
  const out = await llmGradeBatch(target, zhipuKey, geminiKey, rules, saveLog);
  const graded = out ? out.result : null;
  await saveLog('LLM结果=' + (graded ? graded.length + '@' + out.channel : 'null(双通道全断)'));
  /* 【全断红灯状态】v450：两路 LLM 都失败才算 down。只写 KV 供 App 顶部红灯读取，
   * 不再发任何外部 webhook（Server酱免费额度被2分钟连发2条耗尽，这条路已彻底断开）。 */
  try {
    const prev = JSON.parse((await env.ACCT_KV.get('mp_llm_status')) || '{"down":false}');
    const down = !graded;
    if (down !== !!prev.down) {
      await env.ACCT_KV.put('mp_llm_status', JSON.stringify({ down, ts: Date.now(),
        reason: down ? '智谱+Gemini双通道全断，回退本地规则' : 'LLM已恢复',
        channel: down ? '' : (out && out.channel || '') }));
      await saveLog('LLM状态跃迁 down=' + down + '(仅KV,不发webhook)');
    }
  } catch (e) { await saveLog('状态写入异常 ' + e); }
  if (!graded) { await saveLog('LLM无结果保留旧产物'); return; }
  const byId = {};
  graded.forEach(g => { byId[g.id] = g; });
  const freshItems = target.map((it, i) => {
    const g = byId[i] || {};
    const lv = 'SABC'.includes(g.level) ? g.level : 'B';
    return { ...it, ts: parseTs(it.ts), level: lv, category: g.category || '快讯', reason: g.reason || '' };
  });
  // 合并进滚动池：新条目在前，去重，按 ts 倒序，截断 60 条
  const merged = freshItems.concat(pool.filter(p => !freshItems.some(f => f.id === p.id)));
  const uniq = [];
  const seen2 = new Set();
  merged.forEach(x => { if (!seen2.has(x.id)) { seen2.add(x.id); uniq.push(x); } });
  uniq.sort((a, b) => (b.ts || 0) - (a.ts || 0));
  // 24h TTL 淘汰：超过 24 小时的旧条目直接丢弃；再按上限 1000 截断
  const cutoff = Date.now() - POOL_TTL_MS;
  const inWindow = uniq.filter(x => (x.ts || 0) >= cutoff);
  const newPool = inWindow.slice(0, POOL_MAX);
  await env.ACCT_KV.put(POOL_KEY, JSON.stringify(newPool));
  // 产物 = 整个池子（前端一次拿到60条映射，首屏0ms覆盖更全）
  await env.ACCT_KV.put(NEWS_KV_KEY, JSON.stringify({
    generated: new Date(Date.now() + 8 * 3600e3).toISOString().replace('T', ' ').slice(0, 19),
    src: '新浪财经+同花顺', engine: 'CF边缘·智谱增量池', items: newPool,
    newCount: freshItems.length, poolSize: newPool.length,
  }));
  saveLog('写入完成 新增' + freshItems.length + ' 池' + newPool.length + ' ms=' + (Date.now() - t0));
  await flushLog();   // 本轮有真实写入 → 才写一次日志 KV（省写卫生）
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
    /* v456【秒级直算】POST /__news/fast-grade：前端对未命中池的最新突发新闻，
     * 并发送几条标题过来，当场调智谱分级并直接返回（不写KV，下轮cron自然覆盖）。
     * 硬约束：8秒总超时 + aborted 静默失败，绝不卡住前端。 */
    if (u.pathname === '/__news/fast-grade' && req.method === 'POST') {
      const t0 = Date.now();
      const hardTimer = setTimeout(() => {}, 1000); // 占位，真正超时用 Promise.race
      clearTimeout(hardTimer);
      try {
        let payload = { items: [] };
        try { payload = await req.json(); } catch (e) {}
        const items = Array.isArray(payload.items) ? payload.items.slice(0, 15) : [];
        if (!items.length) return aj({ levels: [] });
        // 先查池子已分过的（标题前20字），命中的直接返回，不重复调 LLM
        const poolRaw = await env.ACCT_KV.get(NEWS_KV_KEY);
        let pool = [];
        try { pool = poolRaw ? JSON.parse(poolRaw).items || [] : []; } catch (e) {}
        const byKey = {};
        pool.forEach(x => { byKey[String(x.title || '').replace(/\s+/g, '').slice(0, 20)] = x; });
        const todo = [], hit = [];
        items.forEach((it, i) => {
          const key = String(it.title || '').replace(/\s+/g, '').slice(0, 20);
          if (byKey[key]) hit.push({ i: i, level: byKey[key].level, category: byKey[key].category });
          else todo.push({ id: todo.length, title: it.title, brief: it.brief || '' });
        });
        let graded = [];
        if (todo.length) {
          const rules = await env.ACCT_KV.get('mp_prompt_rules') || '';
          const out = await Promise.race([
            llmGradeBatch(todo, env.ZHIPU_KEY, env.GEMINI_KEY, rules),
            new Promise((_, rej) => setTimeout(() => rej(new Error('fast-grade 8s timeout')), 8000)),
          ]);
          if (out && out.result) graded = out.result.map(g => {
            const src = todo[g.id] || {};
            const origIdx = items.findIndex(x => String(x.title || '').replace(/\s+/g, '').slice(0, 20)
              === String(src.title || '').replace(/\s+/g, '').slice(0, 20));
            return { i: origIdx, level: g.level, category: g.category, aiGraded: true };
          });
        }
        const levels = hit.concat(graded);
        return aj({ levels: levels, ms: Date.now() - t0 });
      } catch (e) {
        // 超时/aborted/任何异常 → 静默返回空，前端保持本地规则，绝不卡住
        return aj({ levels: [], ms: Date.now() - t0, err: String(e && e.message || e).slice(0, 60) });
      }
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
    /* 【验收端点】强制测 LLM 兜底通道：?force=gemini 绕开智谱直接走 Gemini，
     * 证明双通道不是名义上的（需 CRON_KEY）。不写 KV。 */
    if (u.pathname === '/__testllm') {
      if (!env.CRON_KEY || req.headers.get('X-MP-Key') !== env.CRON_KEY) {
        return new Response('forbidden', { status: 403, headers: CORS });
      }
      const force = u.searchParams.get('force') || '';
      const t0 = Date.now();
      try {
        const rules = await env.ACCT_KV.get('mp_prompt_rules') || '';
        const out = await llmGradeBatch(
          [{ id: 0, title: '美联储意外降息50个基点', brief: '议息会议决定降息50bp' }],
          env.ZHIPU_KEY, env.GEMINI_KEY, rules, () => {}, force);
        return new Response(JSON.stringify({ force: force || 'zhipu-first',
          channel: out && out.channel, ms: Date.now() - t0,
          levels: out ? out.result.map(x => x.level) : null }),
          { headers: { 'Content-Type': 'application/json', ...CORS } });
      } catch (e) {
        return new Response(JSON.stringify({ force, error: String(e && e.message || e), ms: Date.now() - t0 }),
          { status: 500, headers: { 'Content-Type': 'application/json', ...CORS } });
      }
    }
    if (u.pathname === '/__drivenews') {
      if (!env.CRON_KEY || req.headers.get('X-MP-Key') !== env.CRON_KEY) {
        return new Response('forbidden', { status: 403, headers: CORS });
      }
      const t0 = Date.now();
      try {
        let probe = {};
        try { const r = await fetchAllCloudSources(); probe.sources = r.detail; probe.total = r.items.length; }
        catch (e) { probe.err = String(e); }
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
