// 方案A升级：GHA 快讯补抓 —— 并发调用 news-sources.js 全部10源，
// 每源独立超时/失败隔离（单源被封或超时跳过，不阻塞），与历史池滚动合并，
// 保留最近72h写 data/news_supplement.json，供 build_news_map.py / radar 合并使用。
// 不写 CF KV、不调 LLM，零 Gemini/智谱消耗。每源只拉最新一页（不翻页防封IP）。
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HERE = __dirname;
const SRC = path.join(HERE, 'cf-worker', 'news-sources.js');
const OUT = path.join(HERE, '..', 'data', 'news_supplement.json');
const WINDOW_H = 72;          // 滚动沉淀窗口：最近72小时

function loadSources() {
  let code = fs.readFileSync(SRC, 'utf8');
  // 模块无 export：把聚合函数挂到全局，在 vm context 里取
  code += '\n;globalThis.__all = { fetchAllCloudSources };';
  const ctx = {
    fetch: global.fetch, setTimeout, clearTimeout, console,
    AbortController, crypto: require('crypto').webcrypto, URL,
    TextEncoder, TextDecoder, Buffer,
    Date, Math, String, Array, JSON, Promise, Error, Object, RegExp, Number, Boolean,
    globalThis: {},
  };
  vm.createContext(ctx);
  vm.runInContext(code, ctx, { timeout: 15000 });
  return ctx.globalThis.__all;
}

function keyOf(title) {
  return String(title || '').replace(/\s+/g, '').slice(0, 20);
}

function loadOld() {
  try {
    return JSON.parse(fs.readFileSync(OUT, 'utf8')).items || [];
  } catch (e) {
    return [];
  }
}

async function main() {
  const { fetchAllCloudSources } = loadSources();
  // 全10源并发，每源内部20s超时 + Promise.allSettled 失败隔离；detail 打印各源条数
  const res = await fetchAllCloudSources();
  const detail = res.detail || [];
  const okSources = detail.filter(d => !d.includes('FAIL')).length;
  console.log('[news_supplement] 10源结果:', detail.join(' | '));

  /* v463【可观测性】把各源状态写成结构化统计，输出到 GITHUB_STEP_SUMMARY，
   * 防止「step 绿灯但核心源被 GHA IP 反爬返回0条」的假绿；核心源0条额外发
   * ::warning:: 注解（不中断 workflow）。本地运行时环境变量不存在则只打控制台。 */
  const CORE = ['金十数据', '华尔街见闻', '财联社', '财联社深度'];
  const stats = detail.map(d => {
    const m = String(d).match(/^(.*?):(.+)$/);
    const name = m ? m[1] : String(d);
    const rest = m ? m[2] : '';
    const failed = rest.startsWith('FAIL');
    const n = failed ? 0 : (parseInt(rest, 10) || 0);
    return { name, failed, n, core: CORE.includes(name), err: failed ? rest.slice(5, 45).replace(/[)）]+$/, '') : '' };
  });
  stats.forEach(s => {
    if (s.core && s.n === 0) {
      console.log(`::warning::核心源 ${s.name} 抓取条数为 0${s.err ? '（' + s.err + '）' : ''}`);
    }
  });
  const summary = [
    '## Polymarket/快讯补抓 · 10源抓取统计', '',
    '| 源 | 状态 | 条数 | 核心 |', '|---|---|---:|---|',
    ...stats.map(s => `| ${s.name} | ${s.failed ? '❌ 失败' : (s.n === 0 ? '⚠️ 0条' : '✅ 成功')} | ${s.n} | ${s.core ? (s.n === 0 ? '⚠️ **核心0条**' : '是') : ''} |`),
    '', `成功源 ${okSources}/${stats.length}；核心源0条 ${stats.filter(s => s.core && s.n === 0).length}/${CORE.length}。`,
  ].join('\n');
  const sumFile = process.env.GITHUB_STEP_SUMMARY;
  if (sumFile) {
    try { fs.appendFileSync(sumFile, summary + '\n'); }
    catch (e) { console.log('[news_supplement] 写 STEP_SUMMARY 失败:', e.message); }
  } else {
    console.log('[news_supplement] STEP_SUMMARY(本地预览):\n' + summary);
  }

  if (!okSources) {
    console.log('[news_supplement] 全部源失败，保留旧池文件（不覆盖）');
    return;
  }

  const now = Date.now();
  const cutoff = now - WINDOW_H * 3600 * 1000;
  // 旧池 + 新抓合并去重，按 ts 倒序，滚动保留72h
  const raw = loadOld().concat(res.items || []);
  const seen = new Set(), items = [];
  for (const x of raw.sort((a, b) => (Number(b.ts) || 0) - (Number(a.ts) || 0))) {
    const ts = Number(x.ts) || 0;
    if (ts < cutoff || ts > now + 5 * 60 * 1000) continue;   // 滚动：只留72h内
    if (!x.title) continue;
    const k = keyOf(x.title);
    if (seen.has(k)) continue;
    seen.add(k);
    items.push({ id: x.id || ('supp-' + k + '-' + ts), ts,
                 title: String(x.title).slice(0, 200), brief: String(x.brief || '').slice(0, 300),
                 src: x.src || '快讯补抓', supplement: true });
  }
  const out = {
    generated: new Date(now + 8 * 3600e3).toISOString().replace('T', ' ').slice(0, 19),
    window_hours: WINDOW_H, sources: detail, ok_sources: okSources,
    n: items.length, items,
  };
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(out, null, 2));
  console.log('[news_supplement] 滚动沉淀72h → 池内', items.length, '条（旧+' + (res.items || []).length + '新，去重+截窗）');
  console.log('[news_supplement] wrote', OUT);
}

main().catch(e => {
  console.error('[news_supplement] 失败:', e.message);
  // 失败不覆盖旧文件——保留上次成功产物，避免把池子打空
  process.exit(0);
});
