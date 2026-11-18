/**
 * MACRO PULSE 边缘实时网关（Cloudflare Pages Functions · 免费 · pages.dev 国内可达）
 *
 * 端点：
 *   /rules        → 从 ACCT_KV 读 mp_prompt_rules（LLM 判级去重规则快照，实时）
 *   /__news       → 从 ACCT_KV 读 mp_news_all（CF边缘2分钟增量产物，前端异步覆盖）
 *   /__proxy?u=   → 快讯源抓取代理（白名单，含 CORS）
 *
 * 绑定：ACCT_KV（已通过 PATCH deployment_configs.kv_namespaces map 格式绑定）
 * 安全：Access-Control-Allow-Origin: *；东财绝不经此（红线：CF跑批直连，浏览器代理不碰）
 */
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

// 快讯源白名单（不含东财——东财由CF边缘定时直连，不经浏览器代理）
const NEWS_ALLOW = /(jin10\.com|cls\.cn|10jqka\.com\.cn|wallstreetcn\.com|sina\.com\.cn|sina\.cn|ithome\.com|qq\.com|gtimg\.cn)/i;

const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { ...CORS, 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' },
});

export default {
  async fetch(request, env, ctx) {
    const u = new URL(request.url);
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: CORS });
    // /rules —— 从 KV 实时读规则快照
    if (u.pathname === '/rules') {
      if (!env.ACCT_KV) return new Response('KV not bound', { status: 503, headers: CORS });
      const rules = await env.ACCT_KV.get('mp_prompt_rules');
      if (!rules) return new Response('rules not synced', { status: 404,
        headers: { ...CORS, 'Content-Type': 'text/plain; charset=utf-8' } });
      return new Response(rules, { headers: { ...CORS,
        'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' } });
    }

    /* /__ingest —— 前端把抓到的全源新闻(id/title/brief/src)异步喂进待分级队列。
     * 解决"边缘池子只有新浪+同花顺、财联社/金十等源永远没AI角标"的问题。
     * 只写 KV 队列，不做 LLM（不阻塞前端）；Worker 每分钟从队列取新增分级。
     * 限流：单次≤40条、标题≤120字，防滥用。 */
    if (u.pathname === '/__ingest' && request.method === 'POST') {
      if (!env.ACCT_KV) return json({ error: 'KV not bound' }, 503);
      let items = [];
      try { const body = await request.json(); items = Array.isArray(body.items) ? body.items : []; }
      catch (e) { return json({ error: 'bad json' }, 400); }
      items = items.slice(0, 50).filter(x => x && x.id && x.title)
        .map(x => ({ id: String(x.id).slice(0, 80), title: String(x.title).slice(0, 120),
          brief: String(x.brief || '').slice(0, 200), src: String(x.src || '').slice(0, 30),
          ts: Number(x.ts) || Date.now() }));
      if (!items.length) return json({ ok: true, n: 0 });
      // 去重合并进队列（队列本身也限 200 条）
      let queue = [];
      try { queue = JSON.parse((await env.ACCT_KV.get('mp_news_ingest_queue')) || '[]'); } catch (e) {}
      const have = new Set(queue.map(x => x.id));
      items.forEach(x => { if (!have.has(x.id)) { queue.push(x); have.add(x.id); } });
      if (queue.length > 1000) queue = queue.slice(-1000);
      await env.ACCT_KV.put('mp_news_ingest_queue', JSON.stringify(queue));
      return json({ ok: true, accepted: items.length, queue: queue.length });
    }

    // /__news —— 从 KV 读 CF 边缘 2 分钟增量的 LLM 分级成品（前端毫秒级异步覆盖）
    if (u.pathname === '/__news' || u.pathname === '/news.json') {
      if (!env.ACCT_KV) return json({ error: 'KV not bound', items: [] }, 503);
      const raw = await env.ACCT_KV.get('mp_news_all');
      if (!raw) return json({ error: 'news not synced', items: [] }, 404);
      try {
        const data = JSON.parse(raw);
        // 带上 LLM 全断状态（前端据此亮红灯 🚨 AI已断开·规则兜底）
        try { data.llm_status = JSON.parse((await env.ACCT_KV.get('mp_llm_status')) || '{"down":false}'); }
        catch (e) { data.llm_status = { down: false }; }
        return json(data);
      } catch (e) { return json({ error: 'bad news json', items: [] }, 500); }
    }

    // /__proxy?u= —— 快讯源抓取（实时微批接口数据入口）
    if (u.pathname === '/__proxy' || u.pathname.startsWith('/__proxy/')) {
      const target = u.searchParams.get('u') || '';
      if (!/^https?:\/\//i.test(target)) return new Response('bad target', { status: 400, headers: CORS });
      if (!NEWS_ALLOW.test(target)) return new Response('forbidden host', { status: 403, headers: CORS });
      try {
        const r = await fetch(target, {
          headers: {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36',
            'Accept': '*/*',
          },
          cf: { cacheTtl: 5, cacheEverything: false },
        });
        return new Response(await r.arrayBuffer(), { status: r.status, headers: {
          ...CORS,
          'Content-Type': r.headers.get('content-type') || 'application/json; charset=utf-8',
          'Cache-Control': 'no-store',
        } });
      } catch (e) {
        return new Response('upstream error: ' + String(e && e.message || e), { status: 502, headers: CORS });
      }
    }

    return json({ ok: true, service: 'macropulse-edge-gateway', kv: !!env.ACCT_KV, ts: Date.now() });
  },
};
