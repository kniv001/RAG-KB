/**
 * Agent 模式耗时分解探针：给每个 SSE 事件打时间戳，看清 168 秒到底花在哪。
 * 用完即删。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const CRED = 'D:/vs/rag-kb/data/auth.json.migrated';

const cred = JSON.parse(fs.readFileSync(CRED, 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const t0 = Date.now();
const st = await c.openStream('POST', '/api/ask/stream',
  { question: '校园网为什么拒绝 IPv6 入站？依据是什么？', strategy: 'agent' }, { token: TOKEN });

const marks = [];
let lastAnswerAt = null;
await readSse(st.response, (name, raw) => {
  let data;
  try { data = JSON.parse(raw); } catch { data = raw; }
  const at = Date.now() - t0;
  if (name === 'answer') {
    // 只记录第一个 answer token 的时间，之后不再刷屏
    if (lastAnswerAt === null) { lastAnswerAt = at; marks.push({ name: 'answer(首个)', at }); }
    return;
  }
  marks.push({ name, at, note: summarize(name, st.decrypt(data)) });
});

function summarize(name, d) {
  if (name === 'plan') return `round=${d.round} queries=${JSON.stringify(d.queries)}`;
  if (name === 'retrieve') return `round=${d.round} hits=${d.hits}`;
  if (name === 'assess') return `round=${d.round} enough=${d.enough}`;
  if (name === 'done') return `rounds=${d.rounds} contexts=${d.contexts} elapsed=${d.elapsedMs}ms`;
  return '';
}

console.log('时间线：\n');
let prev = 0;
for (const m of marks) {
  console.log(`  ${String(m.at).padStart(7)}ms  (+${String(m.at - prev).padStart(6)}ms)  ${m.name.padEnd(14)} ${m.note || ''}`);
  prev = m.at;
}
console.log(`\n  总计 ${Date.now() - t0}ms`);

// 分段统计
const seg = (from, to) => marks.filter(m => m.name === from || (to && m.name === to));
const planAssess = marks.filter(m => m.name === 'plan' || m.name === 'assess');
if (planAssess.length) {
  console.log(`\n  plan/assess 事件共 ${planAssess.length} 个（每个背后是一次完整的模型调用）`);
}
console.log(`  首个 answer token 出现在 ${lastAnswerAt}ms`);
console.log(`  → 答案自身从开始生成到吐出第一个 token 用了约 ${Date.now() - t0 - (marks.filter(m=>m.name==='assess').slice(-1)[0]?.at ?? 0)}ms 量级`);
