/**
 * 判定 SSE 到底有没有在流式：给每个 answer token 打时间戳，看分布。
 * 均匀分布 = 真流式；挤在最后 = 中间被缓冲。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const t0 = Date.now();
const st = await c.openStream('POST', '/api/ask/stream',
  { question: '请用大约两百字介绍三层缓存的设计', strategy: 'classic', model: 'local/qwen3:4b' },
  { token: TOKEN });

const marks = [];
await readSse(st.response, (name, raw) => {
  let d; try { d = JSON.parse(raw); } catch { d = raw; }
  const dec = st.decrypt(d);
  if (name === 'answer') marks.push({ at: Date.now() - t0, len: (dec.t || '').length });
});

const total = marks.length;
if (total === 0) { console.log('没有收到 answer 事件'); process.exit(1); }

const first = marks[0].at;
const last = marks[total - 1].at;
const span = last - first;

console.log(`\ntoken 事件数: ${total}`);
console.log(`首个 token: ${first}ms`);
console.log(`末个 token: ${last}ms`);
console.log(`跨度: ${span}ms`);
if (span > 0) console.log(`平均间隔: ${(span / Math.max(1, total - 1)).toFixed(1)}ms`);

// 分布：把跨度分成 10 段，看事件落在哪
console.log('\n时间分布（跨度均分 10 段）:');
const buckets = new Array(10).fill(0);
if (span > 0) {
  for (const m of marks) {
    const idx = Math.min(9, Math.floor((m.at - first) / span * 10));
    buckets[idx]++;
  }
} else {
  buckets[9] = total;
}
buckets.forEach((n, i) => {
  const pct = (n / total * 100).toFixed(0);
  console.log(`  段${i + 1} ${String(n).padStart(4)} 个 (${pct.padStart(3)}%)  ${'█'.repeat(Math.round(n / total * 50))}`);
});

// 前 20 个 token 的间隔明细
console.log('\n前 20 个 token 的到达时刻:');
console.log('  ' + marks.slice(0, 20).map(m => m.at + 'ms').join(', '));

const lastFifth = marks.filter(m => m.at > first + span * 0.8).length;
console.log(`\n结论: ${lastFifth > total * 0.6
  ? '❌ 大部分 token 挤在最后 20% 时间里 —— 有缓冲，不是真流式'
  : '✅ 分布相对均匀 —— 是真流式'}`);
