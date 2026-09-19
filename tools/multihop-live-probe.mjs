/**
 * 把多跳尺子**接到真实链路**上。
 *
 * 为什么要这一步：`multihop-probe.py` 复刻的只有**向量通道 + 原始问句**，
 * 而真实链路还有规划器改写查询、关键词通道、RRF 融合、距离门槛。
 * 不接真实链路就不知道"50%"是真实水平还是影子 —— 而"低估了多少"决定了这把尺子能不能用。
 *
 * 做法：跑 `/api/chat/stream`（agent 模式），收下两样东西：
 *   · plan 事件里的 **改写后查询**（后面用同一批查询跑向量复刻，做同口径对比）
 *   · retrieve 事件里的 **来源**（`docName#seq`，每次查询前 5 条）
 * 结果写 tools/_multihop-live.json。
 *
 * 用法：node tools/multihop-live-probe.mjs
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const cfg = JSON.parse(fs.readFileSync('tools/multihop-questions.json', 'utf8'));
const out = [];
console.log(`跑 ${cfg.cases.length} 道多跳题（agent 模式，真实链路）\n`);

for (const cs of cfg.cases) {
  const body = { question: cs.q, strategy: 'agent', model: 'local/qwen3:4b' };
  const t0 = Date.now();
  const rec = { q: cs.q, targets: cs.targets, queries: [], sources: [], rounds: 0 };
  try {
    const st = await c.openStream('POST', '/api/chat/stream', body, { token: TOKEN });
    await readSse(st.response, (name, raw) => {
      let d; try { d = JSON.parse(raw); } catch { d = raw; }
      const dec = st.decrypt(d);
      if (name === 'plan') { rec.queries = dec.queries || []; rec.rounds++; }
      if (name === 'retrieve') rec.sources.push(...(dec.sources || []));
      // **收尾事件的 sources 才是"真正进提示词"的那一批**（retrieve 每次查询只报前 5 条显示用）。
      // 量 top-k 这类"每条查询看多宽"的改动，必须用这个，否则前 5 条不变、量不出差别。
      if (name === 'done') rec.final = dec.sources || [];
    });
  } catch (e) {
    rec.error = String(e.message || e);
  }
  out.push(rec);
  console.log(`${rec.error ? '✗' : '✔'} ${((Date.now() - t0) / 1000).toFixed(0)}s  `
    + `查询 ${rec.queries.length}　来源 ${rec.sources.length}　${cs.q.slice(0, 34)}`);
}
fs.writeFileSync('tools/_multihop-live.json', JSON.stringify(out, null, 1));
console.log(`\n写入 tools/_multihop-live.json`);
