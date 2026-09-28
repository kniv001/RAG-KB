/**
 * **只采材料、不等生成**的探针 —— 用来量"这一题最终注入了什么"。
 *
 * ## 为什么要有它
 *
 * 一次 `eval.py run` 是 **21 题 × ~25s ≈ 9 分钟**，而里面 **80% 以上的时间是生成**
 * （decode 84~86% 是思考）。可"材料"这件事**在生成之前就定完了**：检索 → 选句 → 地板，
 * 服务端在那一刻就把读数写进了日志：
 *
 *     材料地板 0.65（<题目前 20 字>）：过滤后剩 N 句 / M 块
 *     相邻块补全（±1）：命中 N 段 → 新增 M 段 → 候选 K 段
 *
 * 所以本探针**把请求发出去、等材料算完就断开**，一个字的生成都不等 ——
 * 21 题从 9 分钟降到 **1~2 分钟**。这是"量材料"与"量答案"必须分开的又一次应用：
 * 判据要什么就采什么，多采的部分不只是浪费，还会**把噪声引进来**。
 *
 * ## 为什么不能直接用 curl / Python
 *
 * 这条链路的**每个请求体与每个事件都是加密信封**（RSA 封装 AES，见 `kb-client.mjs`）。
 * 重写一份 Python 的等于把协议再实现一遍，没必要 —— 所以复用 `kb-client`，
 * 只是**不调 `readSse`**（不解密、不读正文），到点直接 `body.cancel()`。
 *
 * 用法：node tools/eval/material-probe.mjs --bench answer-quality --model qwen3:4b [--wait 9000] [--only a,b]
 * 读数：`python data/_nb_material.py`（它读日志、按题对表）
 */
import fs from 'node:fs';
import path from 'node:path';
import { makeClient } from '../kb-client.mjs';

const arg = (k, d) => {
  const i = process.argv.indexOf(`--${k}`);
  return i >= 0 ? process.argv[i + 1] : d;
};
const BENCH = arg('bench', 'answer-quality');
const MODEL = arg('model', 'qwen3:4b');
// 等多久再断开：要盖住「规划(1 次模型调用) + 检索 + 选句(1 次 embedding) + Jev 判定(1 次)」，
// 实测这条在 3~6s 之间。给到 9s 留余量；**地板那行日志一写出来其实就可以走了**，
// 但客户端解不开事件（不读流），所以只能用时间兜。
const WAIT = parseInt(arg('wait', '9000'), 10);
const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';

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
console.log(`材料探针：${BENCH} ${cases.length} 题 × ${modelRef}（每题等 ${WAIT}ms 后断开）\n`);

for (const cs of cases) {
  const t0 = Date.now();
  try {
    const st = await c.openStream('POST', '/api/chat/stream',
      { question: cs.q, strategy: 'agent', model: modelRef }, { token: TOKEN });
    await new Promise((r) => setTimeout(r, WAIT));
    // **断开**：服务端写事件时会撞上关闭的连接。即使它不立刻停，
    // 我们要的那两行日志也已经落盘了（材料在生成之前就定完）。
    try { await st.response.body.cancel(); } catch { /* 已经关了就算了 */ }
  } catch (e) {
    console.log(`  ！${cs.id} 发不出去：${e.message}`);
  }
  console.log(`  ${cs.id}  ${Date.now() - t0}ms  ${cs.q.slice(0, 26)}`);
}
console.log('\n（材料已采完 —— 用 python data/_nb_material.py 读日志对表）');
