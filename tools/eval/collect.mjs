/**
 * **模型测评的采集端**：把一套基准跑在一个模型上，原始结果落盘，判分交给 Python。
 *
 * 为什么采集在 Node：加密 SSE 客户端（`kb-client.mjs`）只在这里有；重写一份 Python 的
 * 等于把「每请求一把 AES 密钥 + RSA 封装」那条协议再实现一遍，没必要。
 * 这与项目已有的分工一致（`multihop-live-probe.mjs` 采集 / `multihop-live-score.py` 判分）。
 *
 * 用法：node tools/eval/collect.mjs --bench answer-quality --model qwen3:4b [--limit N] [--out 路径]
 */
import fs from 'node:fs';
import path from 'node:path';
import { makeClient, readSse } from '../kb-client.mjs';

const arg = (k, d) => {
  const i = process.argv.indexOf(`--${k}`);
  return i >= 0 ? process.argv[i + 1] : d;
};
const BENCH = arg('bench', 'answer-quality');
const MODEL = arg('model', 'qwen3:4b');
const LIMIT = parseInt(arg('limit', '0'), 10);
const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';

const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const bench = JSON.parse(fs.readFileSync(`tools/eval/benches/${BENCH}.json`, 'utf8'));
let cases = bench.cases;
if (LIMIT) cases = cases.slice(0, LIMIT);

const OUT = arg('out', `tools/eval/_runs/${BENCH}__${MODEL.replace(/[:/]/g, '-')}.json`);
fs.mkdirSync(path.dirname(OUT), { recursive: true });

const modelRef = MODEL.includes('/') ? MODEL : `local/${MODEL}`;
console.log(`基准 ${BENCH}（${cases.length} 题）× 模型 ${modelRef}\n`);

const results = [];
for (const cs of cases) {
  const body = { question: cs.q, strategy: 'agent', model: modelRef };
  const t0 = Date.now();
  let answer = '', thinking = '', ttft = 0, sources = [], err = null;
  // **把 ③ 评估的判定也存下来**：`enough` 是"资料够不够"的机械信号，
  // 而 2026-09-21 那个开关（类型判定交给代码）正是拿它当依据 ——
  // 它准不准，直接决定那条路成不成立。不存就只能靠答案反推。
  let enough = null, assessReason = null;
  try {
    const st = await c.openStream('POST', '/api/chat/stream', body, { token: TOKEN });
    await readSse(st.response, (name, raw) => {
      let d; try { d = JSON.parse(raw); } catch { d = raw; }
      const dec = st.decrypt(d);
      if (name === 'answer') {
        if (!ttft) ttft = Date.now() - t0;
        answer += String(dec.t ?? '');
      } else if (name === 'thinking') {
        if (!ttft) ttft = Date.now() - t0;
        // **把思考也存下来**：它是 decode 的 84~86%，不存就没法回答"它在想什么"，
        // 只能靠字数猜。第一版只拿它记 TTFT，正文丢掉了 —— 于是"少想"这条线
        // 一直在盲改（改了提示词，但看不到思考本身变没变）。
        thinking += String(dec.t ?? '');
      } else if (name === 'assess') {
        // 多轮时会有多条，取**最后一轮**的（那才是决定要不要继续找的那次）
        enough = dec.enough;
        assessReason = dec.reason ?? null;
      } else if (name === 'done') {
        sources = dec.sources || [];
      } else if (name === 'error') {
        err = String(dec.message || dec);
      }
    });
  } catch (e) {
    err = String(e.message || e);
  }
  const ms = Date.now() - t0;
  results.push({ ...cs, answer, thinking, ttftMs: ttft, ms, sources, enough, assessReason, error: err });
  console.log(`  ${err ? '✗' : '✔'} ${(ms / 1000).toFixed(1)}s  正文 ${answer.length} 字`
    + ` / 思考 ${thinking.length} 字  `
    + `来源 ${sources.length}  ${cs.q.slice(0, 30)}${err ? '  ' + err.slice(0, 40) : ''}`);
}

fs.writeFileSync(OUT, JSON.stringify({
  bench: BENCH, model: modelRef, at: new Date().toISOString(), results,
}, null, 1));
console.log(`\n原始结果 → ${OUT}`);
