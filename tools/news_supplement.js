// 方案A：GHA 轻量快讯补抓 —— 从最稳定的3个核心源拉最近24h，
// 写 data/news_supplement.json，供 build_news_map.py / radar 与 CF __news 池合并。
// 复用现成 tools/cf-worker/news-sources.js（金十/华尔街见闻/财联社三源已实现）。
// 不写 CF KV（无贴标、无 LLM，零 Gemini/智谱消耗），只读源站。
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HERE = __dirname;
const SRC = path.join(HERE, 'cf-worker', 'news-sources.js');
const OUT = path.join(HERE, '..', 'data', 'news_supplement.json');
const WINDOW_H = 24;

function loadSources() {
  let code = fs.readFileSync(SRC, 'utf8');
  // 模块无 export：拼一段把三个函数挂到全局，再在 vm context 里取
  code += '\n;globalThis.__fetchers = { fetchJin10, fetchCls, fetchWSCN };';
  const ctx = {
    fetch: global.fetch, setTimeout, clearTimeout, console,
    AbortController, crypto: require('crypto').webcrypto, URL,
    TextEncoder, TextDecoder, Buffer,
    Date, Math, String, Array, JSON, Promise, Error, Object, RegExp, Number, Boolean,
    globalThis: {},
  };
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { timeout: 15000 });
  return ctx.globalThis.__fetchers;
}

function keyOf(title) {
  return String(title || '').replace(/\s+/g, '').slice(0, 20);
}

async function main() {
  const f = loadSources();
  const jobs = [
    ['金十数据', f.fetchJin10],
    ['华尔街见闻', f.fetchWSCN],
    ['财联社', f.fetchCls],
  ];
  const cutoff = Date.now() - WINDOW_H * 3600 * 1000;
  const all = [], detail = [];
  for (const [name, fn] of jobs) {
    try {
      const items = await Promise.race([
        fn(),
        new Promise((_, rej) => setTimeout(() => rej(new Error('超时25s')), 25000)),
      ]);
      const fresh = (items || []).filter(x => {
        const ts = Number(x.ts) || 0;
        return ts >= cutoff && ts <= Date.now() + 5 * 60 * 1000 && x.title;
      });
      all.push(...fresh);
      detail.push(name + ':' + fresh.length);
    } catch (e) {
      detail.push(name + ':FAIL(' + String(e.message || e).slice(0, 50) + ')');
    }
  }
  // 标题前20字去重（与 CF ingest 同口径）
  const seen = new Set(), items = [];
  all.sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0));
  for (const x of all) {
    const k = keyOf(x.title);
    if (seen.has(k)) continue;
    seen.add(k);
    items.push({ id: x.id || ('supp-' + k + '-' + x.ts), ts: Number(x.ts) || Date.now(),
                 title: String(x.title).slice(0, 200), brief: String(x.brief || '').slice(0, 300),
                 src: x.src || '快讯补抓', supplement: true });
  }
  const out = { generated: new Date(Date.now() + 8 * 3600e3).toISOString().replace('T', ' ').slice(0, 19),
                window_hours: WINDOW_H, sources: detail, n: items.length, items };
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(out, null, 2));
  console.log('[news_supplement] 抓取完成:', detail.join(' | '), '→ 去重后', items.length, '条');
  console.log('[news_supplement] wrote', OUT);
}

main().catch(e => {
  console.error('[news_supplement] 失败:', e.message);
  // 失败不覆盖旧文件——保留上次成功产物，避免把合并池打空
  process.exit(0);
});
