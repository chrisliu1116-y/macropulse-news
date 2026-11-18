/**
 * MACRO PULSE 边缘 API 网关（Cloudflare Pages Functions · 免费 · 绕开 workers.dev 污染）
 *
 * 端点：
 *   /rules                → 从 ACCT_KV 读 mp_prompt_rules（LLM 判级去重规则快照）
 *   /__proxy?u=<url>      → 抓取快讯源（白名单，含 CORS 放行）
 *
 * 安全：
 *   · 白名单只放行快讯源，东财绝不经此（红线：东财由 GHA/CF 跑批直接请求，不走浏览器代理）
 *   · Access-Control-Allow-Origin: * 放行前端静态 H5 跨域直连
 *   · KV 绑定名 ACCT_KV（与现有 Worker 共用 namespace）
 */
const EMBEDDED_RULES_B64 = /*__RULES_B64__*/ ""; // 部署时内嵌规则的base64(纯ASCII避免转义)
const EMBEDDED_NEWS_B64 = /*__NEWS_B64__*/ "";   // 部署时内嵌新浪分级产物(base64)

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

// 快讯源白名单（不含东财——东财红线：不经浏览器侧代理）
const NEWS_ALLOW = /(jin10\.com|cls\.cn|10jqka\.com\.cn|wallstreetcn\.com|sina\.com\.cn|sina\.cn|ithome\.com|qq\.com|gtimg\.cn)/i;

export default {
  async fetch(request, env, ctx) {
    const u = new URL(request.url);
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS });
    }
    // /rules —— 读 KV 规则快照
    if (u.pathname === '/rules') {
      // 优先读环境变量注入的规则快照（GHA push 后由 sync-rules 更新）；
      // 备选读 KV（若绑定可用）。二者都没有才 404。
      let rules = null;
      if (EMBEDDED_RULES_B64) { try { rules = atob(EMBEDDED_RULES_B64).split('').map(c=>String.fromCharCode(c.charCodeAt(0))).join(''); } catch(e) {} }
      // atob 得到的是 latin1 字节，需按 UTF-8 解码
      if (rules) { try { rules = decodeURIComponent(escape(rules)); } catch(e) {} }
      if (!rules) rules = env.PROMPT_RULES || null;
      if (!rules && env.ACCT_KV) {
        try { rules = await env.ACCT_KV.get('mp_prompt_rules'); } catch (e) {}
      }
      if (!rules) return new Response('rules not synced', { status: 404,
        headers: { ...CORS, 'Content-Type': 'text/plain; charset=utf-8' } });
      return new Response(rules, { headers: { ...CORS,
        'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' } });
    }
    // /__proxy?u= —— 快讯增量抓取
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
        const body = await r.arrayBuffer();
        return new Response(body, { status: r.status, headers: {
          ...CORS,
          'Content-Type': r.headers.get('content-type') || 'application/json; charset=utf-8',
          'Cache-Control': 'no-store',
        } });
      } catch (e) {
        return new Response('upstream error: ' + String(e && e.message || e),
          { status: 502, headers: CORS });
      }
    }
    // 静态分级产物：部署时内嵌进 worker（零 binding 依赖，毫秒级返回）
    if (u.pathname === '/news.json') {
      const data = EMBEDDED_NEWS_B64
        ? JSON.parse(decodeURIComponent(escape(atob(EMBEDDED_NEWS_B64)))) : null;
      if (!data) return new Response('news not synced', { status: 404, headers: CORS });
      return new Response(JSON.stringify(data), { headers: { ...CORS,
        'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' } });
    }
    return new Response(JSON.stringify({ ok: true, service: 'macropulse-api', ts: Date.now() }),
      { headers: { ...CORS, 'Content-Type': 'application/json; charset=utf-8' } });
  },
};
