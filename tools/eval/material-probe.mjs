/**
 * **只采材料、不等生成**的探针 —— 用来量"这一题最终注入了什么"。
 *
 * ## 2026-09-29 改：读数从**日志**换成**诊断端点**
 *
 * 旧的做法是"发请求 → 睡够 9 秒 → 断开 → 让 Python 去 grep 日志"。它有三个脆弱点，
 * 每个在本项目都真实发生过：
 *   1. **睡短了就静默漏题** —— 那条链实测 3~6 秒，给 9 秒是留余量；某题一慢，
 *      读数就是"没采到"，而它**长得像"这题没问题"**；
 *   2. **日志会轮转**（10MB）—— 证据被搬走，只读到后半段；
 *   3. **按"问题前 20 字"在日志里找** —— 找不到时的样子同样是"没采到"。
 *
 * 现在服务端把那几个数**记在内存里**并用 `GET /api/diag/material?q=…` 只读地吐出来
 * （见 `DiagMaterial` / `DiagController`）。于是本探针：
 *   · **材料一算完立刻走**（实测 3~6 秒，不再固定等 9 秒），顺带早一点掐掉生成、让出 GPU；
 *   · **"没有"是一个明确的回答**（端点回 40400）⇒ 漏题会被记成漏题，不会被当成 0。
 *
 * ⚠️ **先证能对上，再谈数字**（本项目的老规矩）：`DiagMaterial` 的键与日志那行
 * **完全同一个**（问题前 20 字），所以换尺子那天可以**同一臂两种读法各读一遍**去比。
 * `--emit json` 落盘 `data/_material_<bench>_<时刻>.json`，留给 Python 读。
 *
 * ## 为什么不能直接用 curl / Python
 *
 * 这条链路的**每个请求体与每个事件都是加密信封**（RSA 封装 AES，见 `kb-client.mjs`）。
 * 重写一份 Python 的等于把协议再实现一遍，没必要 —— 所以复用 `kb-client`。
 * **端点在信封之内**（没有为它放宽鉴权）：这是刻意的，Python 那一侧只消费**落盘文件**。
 *
 * 用法：node tools/eval/material-probe.mjs --bench answer-quality --model qwen3:4b
 *                                        [--timeout 20000] [--only a,b] [--no-json]
 * ⚠️ 跑之前要清答案缓存（否则整条链短路、没有读数）—— `bench-material-probe.py` 会清。
 */
import fs from 'node:fs';
import path from 'node:path';
import { makeClient } from '../kb-client.mjs';

const arg = (k, d) => {
  const i = process.argv.indexOf(`--${k}`);
  return i >= 0 ? process.argv[i + 1] : d;
};
const has = (k) => process.argv.includes(`--${k}`);
const BENCH = arg('bench', 'answer-quality');
const MODEL = arg('model', 'qwen3:4b');
// **这是超时、不是等待**：轮询到读数就立刻走。给 20 秒是为了容忍冷启动（首次要灌模型）。
const TIMEOUT = parseInt(arg('timeout', arg('wait', '20000')), 10);
const POLL = parseInt(arg('poll', '250'), 10);
const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const NO_JSON = has('no-json');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const bench = JSON.parse(fs.readFileSync(`tools/eval/benches/${BENCH}.json`, 'utf8'));
let cases = bench.cases;
const ONLY = arg('only', '');
if (ONLY) {
  const keys = ONLY.split(',').map((s) => s.trim()).filter(Boolean);
  cases = cases.filter((cs) => keys.some((k) => cs.id === k || cs.kind === k));
}
const modelRef = MODEL.includes('/') ? MODEL : `local/${MODEL}`;
console.log(`材料探针：${BENCH} ${cases.length} 题 × ${modelRef}`
  + `（轮询诊断端点，最多等 ${TIMEOUT}ms）\n`);

const items = [];
let miss = 0;
for (const cs of cases) {
  const t0 = Date.now();
  let rec = null;
  let why = null;
  try {
    // 请求发出去就**不等流**：材料在生成之前就定完了，端点一有读数就撤。
    const st = await c.openStream('POST', '/api/chat/stream',
      { question: cs.q, strategy: 'agent', model: modelRef }, { token: TOKEN });
    try {
      while (Date.now() - t0 < TIMEOUT) {
        const r = await c.call('GET',
          `/api/diag/material?q=${encodeURIComponent(cs.q)}`, undefined, { token: TOKEN });
        const d = r.body && r.body.code === 0 ? r.body.data : null;
        // ⚠️ 两个条件都要：**有 aliveSent**（不是"端点上有一条记录"）且
        // **时间戳在本轮之内**（服务端每轮 `begin()` 会把旧读数清空，双保险）。
        if (d && d.aliveSent != null && d.at >= t0) { rec = d; break; }
        await sleep(POLL);
      }
    } finally {
      // 断开：服务端写事件时会撞上关闭的连接。**即使它不立刻停，我们要的读数也已经到手了。**
      try { await st.response.body.cancel(); } catch { /* 已经关了就算了 */ }
    }
  } catch (e) {
    why = `发不出去：${e.message}`;
  }
  if (!rec && !why) why = `等满 ${TIMEOUT}ms 端点仍无读数`;
  if (!rec) miss++;
  const ms = Date.now() - t0;
  const show = rec ? `${rec.aliveSent} 句 / ${rec.aliveChunks} 块`
    : `！没有读数（${why}）`;
  console.log(`  ${cs.id}  ${String(ms).padStart(5)}ms  ${show}  ${cs.q.slice(0, 22)}`);
  items.push({ id: cs.id, kind: cs.kind, q: cs.q, ms, rec, miss: why || null });
}

console.log(`\n（采完 ${items.length} 题，其中**没有读数** ${miss} 题）`);
if (!NO_JSON) {
  const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
  const out = path.join('data', `_material_${BENCH}_${stamp}.json`);
  fs.writeFileSync(out, JSON.stringify({
    bench: BENCH, model: modelRef, probe: 'diag-endpoint',
    at: new Date().toISOString(), count: items.length, miss, items,
  }, null, 1));
  console.log(`（落盘：${out}）`);
}
