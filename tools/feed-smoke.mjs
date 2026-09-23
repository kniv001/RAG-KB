/**
 * **信息流方向的烟测/尺子**：搜索 → 抓取 → 过闸 → 去重 → 读数。
 *
 * 为什么要有它（而不是只在应用里打日志）：这一支的第一个关键数是**近重复率** ——
 * 它决定"事件归并层"要不要做、做多细。而重复率只能在**真实抓取**上量：
 * 自己造样本等于把结论也一起造了。
 *
 * 用法：
 *   node tools/feed-smoke.mjs "关键词1" "关键词2" ...      # 每个词取前 N 条结果去抓
 *   node tools/feed-smoke.mjs --limit 6 "关键词"           # 每个词只抓 6 条
 *   node tools/feed-smoke.mjs --stats-only                 # 只看读数，不抓
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const args = process.argv.slice(2);
const arg = (k, d) => {
  const i = args.indexOf(`--${k}`);
  return i >= 0 ? args[i + 1] : d;
};
const has = (k) => args.includes(`--${k}`);
const PER_QUERY = parseInt(arg('limit', '5'), 10);
const QUERIES = args.filter((a, i) => !a.startsWith('--') && !(i > 0 && args[i - 1] === '--limit'));

const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
if (login.status !== 200) throw new Error(`登录失败 HTTP ${login.status}`);
const TOKEN = `Bearer ${login.body.data.accessToken}`;

async function stats() {
  // ⚠️ payload 必须是 `undefined` 而不是 `null`：`kb-client` 判的是 `!== undefined`，
  // 传 null 会照样编出一个 body ⇒ GET 带 body，fetch 直接抛错。
  const r = await c.call('GET', '/api/feed/stats', undefined, { token: TOKEN });
  const d = r.body?.data || {};
  console.log(`读数：总 ${d['总条目']} · 首见 ${d['首见']} · 近重复 ${d['近重复']}` +
    `（${d['近重复率']}）· 可召回 ${d['可召回']} · 冷存 ${d['冷存']} · 来源 ${d['来源数']}`);
}

if (has('stats-only') || QUERIES.length === 0) {
  await stats();
  process.exit(0);
}

// ── 搜 → 抓 ──────────────────────────────────────────────────────────────
const urls = [];
for (const q of QUERIES) {
  const r = await c.call('POST', '/api/web/search', { query: q, count: PER_QUERY }, { token: TOKEN });
  const hits = r.body?.data?.results || [];
  console.log(`搜索「${q}」→ ${hits.length} 条`);
  for (const h of hits) urls.push(h.url);
}

const uniq = [...new Set(urls)];
console.log(`\n待抓 ${uniq.length} 个网址（去掉了 ${urls.length - uniq.length} 个重复网址）\n`);

const t0 = Date.now();
const r = await c.call('POST', '/api/feed/urls', { urls: uniq }, { token: TOKEN });
if (r.status !== 200) throw new Error(`入库失败 HTTP ${r.status}：${JSON.stringify(r.body).slice(0, 300)}`);
const items = r.body?.data?.items || [];
for (const it of items) {
  const tag = it.error ? `✗ ${it.error.slice(0, 50)}`
    : (it.duplicateOf ? `↺ 近重复于 #${it.duplicateOf}` : '＋ 首见');
  console.log(`  ${tag}　${it.chars || 0} 字　${(it.title || it.url).slice(0, 40)}`);
}
console.log(`\n抓取耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s\n`);
await stats();
