/**
 * **一次问答的时间都去哪了** —— 逐事件打时间戳，给出分阶段耗时。
 *
 * 为什么要有它：速度这条线一直只有**总数**（`multihop-live-probe` 打每题秒数），
 * 没有分布。而"该优化哪一段"完全取决于分布 ——
 * 已修过的大头是**规划与评估**（强制结构化输出：plan 51.5s → 0.94s、assess 31.4s → 1.6s），
 * 剩下的大概率是**生成**（27~34 tok/s × 回答长度），但那是推测，没量过。
 *
 * 事件顺序（AgentEvent）：plan → retrieve → assess → answer/thinking… → done
 *   · **TTFT**（首字延迟）= 从发出到第一个 answer/thinking 事件 —— 用户感知最强的那个数
 *   · 生成时长 = done − 首字；再除以回答字数就是**有效 tok/s**
 *
 * 用法：node tools/latency-probe.mjs --n 5 --bench multihop-25 --model qwen3:4b
 *      也可以 python tools/eval.py speed --model qwen3:4b（测评平台的速度维度）
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const arg = (k, d) => {
  const i = process.argv.indexOf(`--${k}`);
  return i >= 0 ? process.argv[i + 1] : d;
};
const N = parseInt(arg('n', process.argv[2] || '5'), 10);
const SET = arg('bench', process.argv[3] || 'multihop-25');
const MODEL = arg('model', 'qwen3:4b');
const MODEL_REF = MODEL.includes('/') ? MODEL : `local/${MODEL}`;
const cfg = JSON.parse(fs.readFileSync(`tools/cases/${SET}.json`, 'utf8'));
const cases = cfg.cases.slice(0, N);

console.log(`跑 ${cases.length} 道（agent 模式，真实链路）· 题目集 ${SET}\n`);
console.log('  题                                          plan  retrieve  assess   TTFT   生成   总');

const rows = [];
for (const cs of cases) {
  const body = { question: cs.q, strategy: 'agent', model: MODEL_REF };
  const t0 = Date.now();
  let tPlan = 0, tRetr = 0, tAssess = 0, tFirst = 0, tDone = 0;
  let ansChars = 0, nPlan = 0, nRetr = 0, nAssess = 0;
  try {
    const st = await c.openStream('POST', '/api/chat/stream', body, { token: TOKEN });
    await readSse(st.response, (name, raw) => {
      const now = Date.now() - t0;
      let d; try { d = JSON.parse(raw); } catch { d = raw; }
      const dec = st.decrypt(d);
      if (name === 'plan') { nPlan++; tPlan = now; }
      else if (name === 'retrieve') { nRetr++; tRetr = now; }
      else if (name === 'assess') { nAssess++; tAssess = now; }
      else if (name === 'answer') {
        if (!tFirst) tFirst = now;
        // 逐块累加正文长度，用来算有效 tok/s
        ansChars += String(dec.t ?? dec.text ?? "").length;
      } else if (name === 'thinking') { if (!tFirst) tFirst = now; }
      else if (name === 'done') { tDone = now; }
    });
  } catch (e) {
    console.log(`  ✗ ${String(e.message || e).slice(0, 60)}`);
    continue;
  }
  const row = { q: cs.q, tPlan, tRetr, tAssess, tFirst, tDone, ansChars, nPlan, nRetr, nAssess };
  rows.push(row);
  const seg = (a, b) => (b && a ? `${((b - a) / 1000).toFixed(1)}s` : '—');
  console.log(`  ${cs.q.slice(0, 40).padEnd(42)}${String(tPlan / 1000 || 0).slice(0, 5).padStart(5)}s`
    + `${seg(tPlan, tRetr).padStart(9)}${seg(tRetr, tAssess).padStart(8)}`
    + `${(tFirst / 1000).toFixed(1).padStart(7)}s${seg(tFirst, tDone).padStart(8)}`
    + `${(tDone / 1000).toFixed(1).padStart(7)}s`);
}

if (!rows.length) { console.log('\n没有跑成功的样本'); process.exit(1); }
const med = (f) => {
  const v = rows.map(f).filter((x) => x > 0).sort((a, b) => a - b);
  return v.length ? v[Math.floor(v.length / 2)] : 0;
};
const s = (ms) => (ms / 1000).toFixed(1) + 's';
console.log('\n—— 中位数（n=' + rows.length + '）——');
console.log(`  规划      ${s(med((r) => r.tPlan))}   （${rows[0].nPlan} 轮）`);
console.log(`  检索      ${s(med((r) => r.tRetr - r.tPlan))}`);
console.log(`  评估      ${s(med((r) => r.tAssess - r.tRetr))}   （${rows[0].nAssess} 次）`);
console.log(`  **TTFT**  ${s(med((r) => r.tFirst))}   ← 用户感知最强的那个数`);
console.log(`  生成      ${s(med((r) => r.tDone - r.tFirst))}`);
console.log(`  总计      ${s(med((r) => r.tDone))}`);
const chars = med((r) => r.ansChars);
const gen = med((r) => r.tDone - r.tFirst);
console.log(`\n  回答正文中位 ${chars} 字；生成阶段 ${s(gen)} ⇒ 约 **${(chars / (gen / 1000)).toFixed(0)} 字/秒**`);
console.log('  （中文 1 字 ≈ 1.5~2 token ⇒ 折算 token 速率时再乘 1.5~2）');
