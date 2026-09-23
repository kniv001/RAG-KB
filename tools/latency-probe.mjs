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
const N = parseInt(arg('n', '5'), 10);
const OFF = parseInt(arg('offset', '0'), 10);
const SET = arg('bench', 'multihop-25');
const MODEL = arg('model', 'qwen3:4b');
const MODEL_REF = MODEL.includes('/') ? MODEL : `local/${MODEL}`;
const cfg = JSON.parse(fs.readFileSync(`tools/cases/${SET}.json`, 'utf8'));
// **错开起点**：同一批题问第二遍会命中答案缓存（实测 4/5 命中，每题 2 秒就返回），
// 那种样本量不出生成速度。
const cases = cfg.cases.slice(OFF, OFF + N);

console.log(`跑 ${cases.length} 道（agent 模式，真实链路）· 题目集 ${SET}\n`);
console.log('  题                                          plan  retrieve  assess   TTFT   生成   总'
  + '   prefill  decode  tok/s  装入tok');

const rows = [];
for (const cs of cases) {
  const body = { question: cs.q, strategy: 'agent', model: MODEL_REF };
  const t0 = Date.now();
  let tPlan = 0, tRetr = 0, tAssess = 0, tFirst = 0, tDone = 0;
  let ansChars = 0, thinkChars = 0, nPlan = 0, nRetr = 0, nAssess = 0;
  let stats = null;          // 注意：**别叫 st** —— 下面 const st = openStream() 会遮蔽它
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
      } else if (name === 'thinking') {
        if (!tFirst) tFirst = now;
        thinkChars += String(dec.t ?? '').length;   // 思考也占 decode 时间
      }
      else if (name === 'stats') { stats = dec; }    // 模型侧计时（prefill / decode 分开）
      else if (name === 'done') { tDone = now; }
    });
  } catch (e) {
    console.log(`  ✗ ${String(e.message || e).slice(0, 60)}`);
    continue;
  }
  const row = { q: cs.q, tPlan, tRetr, tAssess, tFirst, tDone, ansChars, thinkChars, nPlan, nRetr, nAssess, stats };
  rows.push(row);
  const seg = (a, b) => (b && a ? `${((b - a) / 1000).toFixed(1)}s` : '—');
  console.log(`  ${cs.q.slice(0, 40).padEnd(42)}${String(tPlan / 1000 || 0).slice(0, 5).padStart(5)}s`
    + `${seg(tPlan, tRetr).padStart(9)}${seg(tRetr, tAssess).padStart(8)}`
    + `${(tFirst / 1000).toFixed(1).padStart(7)}s${seg(tFirst, tDone).padStart(8)}`
    + `${(tDone / 1000).toFixed(1).padStart(7)}s`
    + (stats ? `${(stats.promptMs / 1000).toFixed(1).padStart(9)}s`
          + `${(stats.evalMs / 1000).toFixed(1).padStart(7)}s`
          + `${String(stats.tokPerSec).padStart(7)}`
          + `${String(stats.promptTokens).padStart(9)}`
        : '        —       —       —         —'));
}

if (!rows.length) { console.log('\n没有跑成功的样本'); process.exit(1); }
const cached = rows.filter((r) => !r.stats);
const live = rows.filter((r) => r.stats);
if (cached.length) {
  console.log('\n  ⚠ ' + cached.length + '/' + rows.length + ' 题命中**答案缓存**（没走模型，量不出速度）'
    + ' —— 换 --offset 错开题目');
}
const base = live.length ? live : rows;
const med = (f) => {
  const v = base.map(f).filter((x) => x > 0).sort((a, b) => a - b);
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

const withSt = rows.filter((r) => r.stats);
if (withSt.length) {
  const m = (f) => {
    const v = withSt.map(f).filter((x) => x > 0).sort((a, b) => a - b);
    return v.length ? v[Math.floor(v.length / 2)] : 0;
  };
  const pt = m((r) => r.stats.promptTokens), pm = m((r) => r.stats.promptMs);
  const et = m((r) => r.stats.evalTokens), em = m((r) => r.stats.evalMs);
  console.log('\n—— 模型侧（来自 Ollama 末帧的计时，n=' + withSt.length + '）——');
  console.log(`  **prefill**  ${s(pm)}　装机 ${pt} token ⇒ **${(pt / (pm / 1000)).toFixed(0)} token/秒**（并行，随装入量涨）`);
  console.log(`  **decode**   ${s(em)}　生成 ${et} token ⇒ **${(et / (em / 1000)).toFixed(1)} token/秒**（串行，随回答长度涨）`);
  console.log(`  模型侧合计   ${s(m((r) => r.stats.promptMs + r.stats.evalMs))}`
    + `　加载 ${s(m((r) => r.stats.loadMs))}（换模型后第一次会很大）`);
  const ac = m((r) => r.ansChars), tc = m((r) => r.thinkChars);
  const per = (c) => Math.round(c * et / Math.max(1, ac + tc));
  console.log('\n—— decode 里花在哪 ——');
  console.log(`  正文 ${ac} 字 ≈ ${per(ac)} token　思考 ${tc} 字 ≈ ${per(tc)} token`
    + `　⇒ 思考约占 **${Math.round(100 * tc / Math.max(1, ac + tc))}%**`);

  console.log('\n  读法：**prefill 大 ⇒ 少装资料；decode 是瓶颈 ⇒ 只能少写**。'
    + '两者混在"TTFT"里就分不出来。');
}
const chars = med((r) => r.ansChars);
const gen = med((r) => r.tDone - r.tFirst);
console.log(`\n  回答正文中位 ${chars} 字；生成阶段 ${s(gen)} ⇒ 约 **${(chars / (gen / 1000)).toFixed(0)} 字/秒**`);
// **方向别写反**（2026-09-22 修）：此处原来写的是「1 字 ≈ 1.5~2 token」，
// 与同一段自己算出来的数**互相矛盾** —— 上面正是拿 `字 ÷ 1.88` 折出 token 的。
// 实测（decode-split-probe，6 问逐题对账）**1 token ≈ 1.5~1.9 个中文字**（中位 ~1.7）：
// qwen 的词表里常见词整词一个 token，所以一个 token 覆盖**不止一个字**。
console.log('  （实测 1 token ≈ 1.5~1.9 字 ⇒ 上面 565 字 ≈ 300 token，与 token 速率对得上）');
