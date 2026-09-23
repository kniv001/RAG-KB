/**
 * **decode 再细分**：那 20 秒生成里，时间到底怎么走。
 *
 * 上游已经定了两件事（见 2026-09-20-思考占decode八成 / 今天复测）：
 *   · 一次问答 86% 的时间在 decode，其中 **81% 的 token 是思考**
 *   · prefill 只占 1.5s ⇒「少装资料」的天花板就这么高
 * 所以再往下只剩一个问题：**decode 内部的时间是怎么分布的**。本脚本量两层：
 *
 *   ① **通道 × 时间**：思考窗口 / 正文窗口，各自的字数、秒数、字/秒
 *   ② **速率随时间**：按累计生成长度五等分，看每段的字/秒 ——
 *      decode 是逐 token 串行的，而 KV 随生成长大，**后面的 token 该更慢**。
 *      若真在衰减，"砍掉后半段"的收益就比按 token 比例算出来的更大。
 *
 * ⚠️ **仪器本身的三条限制（读数字前先看这个）**
 *   1. **SSE 到达时刻 ≠ 模型吐字时刻**。thinking 是**攒够 60 字**（THINKING_FLUSH_CHARS）
 *      才推一帧 ⇒ 思考侧的分辨率就是"每 60 字一个点"。正文侧不攒，逐 token 推。
 *   2. **一次 TCP read 里可能塞着好几帧** ⇒ 它们的时间戳几乎相同。所以记 `read` 序号：
 *      某帧的 read 序号与前帧相同 ⇒ 这一段时间被压缩过，**只能读窗口平均，
 *      不能读瞬时速率**。脚本会把这种帧的比例打出来。
 *   3. **答案缓存命中时既没有 thinking 也没有 stats** ⇒ 那种样本量不出速度，
 *      脚本直接标 ✗ 并拒收（本项目踩过一次：21 题里 14 题是缓存命中，
 *      整轮 A/B 的"耗时"其实是回放）。
 *
 * 产物（写进 run.py 给的 KB_RUN_DIR，供 decode-split-read.py 接着读）：
 *   <i>-thinking.txt  该题思考全文
 *   <i>-answer.txt    该题正文全文
 *   <i>-frames.json   [{ch:'think'|'ans', len, t, read}, …]
 *
 * 用法：python tools/run.py decode-split -- node tools/decode-split-probe.mjs --n 3 --offset 110
 */
import fs from 'node:fs';
import path from 'node:path';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const OUT = process.env.KB_RUN_DIR || '.';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const arg = (k, d) => {
  const i = process.argv.indexOf(`--${k}`);
  return i >= 0 ? process.argv[i + 1] : d;
};
const N = parseInt(arg('n', '3'), 10);
const OFF = parseInt(arg('offset', '0'), 10);
const SET = arg('bench', 'multihop-127');
const MODEL = arg('model', 'qwen3:4b');
const MODEL_REF = MODEL.includes('/') ? MODEL : `local/${MODEL}`;
const cases = JSON.parse(fs.readFileSync(`tools/cases/${SET}.json`, 'utf8')).cases.slice(OFF, OFF + N);

/**
 * 自己走一遍 SSE 解析（不用 kb-client 的 readSse）—— 唯一的原因是**要记 read 序号**。
 * readSse 在回调里拿不到"这一帧跟上一帧是不是同一次网络读"，
 * 而没有这个信息，多帧折叠时算出来的瞬时速率是假的。
 */
async function readSseTracked(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let read = 0;                       // 第几次底层 read
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    read++;
    const t = Date.now();
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let name = 'message';
      const dataLines = [];
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) name = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) onEvent(name, dataLines.join('\n'), t, read);
    }
  }
}

const rows = [];
for (const cs of cases) {
  const body = { question: cs.q, strategy: 'agent', model: MODEL_REF };
  const t0 = Date.now();
  const frames = [];
  let thinkText = '', answerText = '', stats = null, done = 0, sources = [];
  const st = await c.openStream('POST', '/api/chat/stream', body, { token: TOKEN });
  await readSseTracked(st.response, (name, raw, t, read) => {
    let d; try { d = JSON.parse(raw); } catch { d = raw; }
    const dec = st.decrypt(d);
    const at = t - t0;
    if (name === 'thinking') {
      const s = String(dec.t ?? '');
      if (s) { thinkText += s; frames.push({ ch: 'think', len: s.length, t: at, read }); }
    } else if (name === 'answer') {
      const s = String(dec.t ?? dec.text ?? '');
      if (s) { answerText += s; frames.push({ ch: 'ans', len: s.length, t: at, read }); }
    } else if (name === 'stats') { stats = dec; }
    else if (name === 'done') {
      done = at;
      // **来源清单必须存下来** —— 只有 done 里才有。存了它，读原文那步才能
      // 从库里反查**实际注入的那几块**，于是"思考里有多少字是在复述已经在提示词里的资料"
      // 才算得出来（这是"结构"这件事里最要紧的一维：复述资料 = 纯开销）。
      sources = dec.sources || [];
    }
  });

  const row = { q: cs.q, frames, thinkText, answerText, stats, done, sources };
  rows.push(row);
  if (!frames.length || !stats) {
    console.log(`✗ ${cs.q.slice(0, 34)} —— 没有帧或没有 stats（多半是**答案缓存命中**），不计入`);
    continue;
  }
  const i = rows.filter((r) => r.frames.length && r.stats).length - 1;
  fs.writeFileSync(path.join(OUT, `${String(i).padStart(2, '0')}-thinking.txt`), thinkText);
  fs.writeFileSync(path.join(OUT, `${String(i).padStart(2, '0')}-answer.txt`), answerText);
  fs.writeFileSync(path.join(OUT, `${String(i).padStart(2, '0')}-frames.json`),
    JSON.stringify({ q: cs.q, stats, done, frames, sources }, null, 1));
  console.log(`✓ ${cs.q.slice(0, 34)}　思考 ${thinkText.length} 字 / 正文 ${answerText.length} 字`
    + `　帧 ${frames.length}（思考 ${frames.filter((f) => f.ch === 'think').length}）`);
}

// ── 汇总：只在"真跑"的样本上算 ────────────────────────────────────────────
const live = rows.filter((r) => r.frames.length && r.stats);
if (!live.length) {
  console.log('\n没有活样本 —— 全部命中答案缓存。换 --offset 错开题目。');
  process.exit(1);
}

const med = (v) => {
  const a = v.filter((x) => x != null && isFinite(x)).sort((x, y) => x - y);
  return a.length ? a[Math.floor(a.length / 2)] : NaN;
};
const f1 = (x) => (isFinite(x) ? x.toFixed(1) : '—');

/**
 * **分段：以「第一帧正文」为界，而不是以「最后一帧思考」为界。**
 *
 * 踩到的坑（读代码才看出来）：思考缓冲区**剩余不足 60 字的那一帧是在整段生成结束后
 * 才补推的** —— `AgenticRagService` 里 `chatStream` 返回之后才 `if (thinkBuf.length()>0) flush`。
 * 也就是说：**最后一帧 thinking 事件的到达时刻在全部正文之后**。
 * 用"思考末帧"当边界会把整个正文窗口也算进思考里（实测差 6~9 秒）。
 *
 * 正确的边界是**第一次出现正文帧的时刻**：模型在一条 decode 流里先吐思考后吐正文，
 * 一旦转正文就不会再回头。（脚本顺带数一下"转正文之后还有几帧思考"——
 * 正常应当是 0 或 1，那 1 帧就是上面那个补推。）
 */
function split(frames) {
  const ai = frames.findIndex((f) => f.ch === 'ans');
  if (ai < 0) return null;
  const ti = frames.findIndex((f) => f.ch === 'think');
  if (ti < 0) return null;
  const firstAns = frames[ai].t, firstThink = frames[ti].t, lastAns = frames[frames.length - 1].t;
  const thinkChars = frames.filter((f) => f.ch === 'think').reduce((s, f) => s + f.len, 0);
  const ansChars = frames.filter((f) => f.ch === 'ans').reduce((s, f) => s + f.len, 0);
  const stragglers = frames.slice(ai).filter((f) => f.ch === 'think').length;
  // 首帧思考自带的 60 字是在 firstThink **之前**生成的（攒够才推）——
  // 这部分时间落在 TTFT 里。补回来才能和 Ollama 的 eval_ms 对上。
  const thinkSec = (firstAns - firstThink) / 1000;
  const ansSec = (lastAns - firstAns) / 1000;
  return { firstThink, firstAns, lastAns, thinkChars, ansChars, stragglers,
           thinkSec, ansSec,
           thinkRate: thinkSec > 0 ? thinkChars / thinkSec : NaN,
           ansRate: ansSec > 0 ? ansChars / ansSec : NaN };
}

console.log('\n' + '='.repeat(78));
console.log('① 通道 × 时间　（边界 = **第一帧正文**；思考侧每组 60 字一帧，正文侧逐 token）');
console.log('  题                                    思考   思考字  字/秒  │  正文   正文字  字/秒 │ 补推帧');
for (const r of live) {
  const s = split(r.frames);
  if (!s) { console.log(`  ${r.q.slice(0, 32)} —— 缺一路通道，跳过`); continue; }
  console.log(`  ${r.q.slice(0, 32).padEnd(34)}`
    + `${f1(s.thinkSec).padStart(7)}s${String(s.thinkChars).padStart(8)}${f1(s.thinkRate).padStart(7)}`
    + `  │${f1(s.ansSec).padStart(6)}s${String(s.ansChars).padStart(8)}${f1(s.ansRate).padStart(7)}`
    + `│${String(s.stragglers).padStart(6)}`);
}

console.log('\n② 速率随时间：把**整条 decode**（首帧思考 → 末帧正文）按累计字数五等分');
console.log('  （匀速 ⇒ 各段相等；递减 ⇒ KV 增长拖慢了后半段，砍尾段的收益比按 token 比例更大）');
console.log('  题                                    段1    段2    段3    段4    段5   趋势');
for (const r of live) {
  const s = split(r.frames);
  if (!s) continue;
  // 时间轴 → 累计字数 的阶梯：只取"转正文之前"的思考帧 + 全部正文帧
  const ai = r.frames.findIndex((f) => f.ch === 'ans');
  const pts = [];
  let cum = 0;
  for (const f of r.frames.slice(0, ai)) { cum += f.len; pts.push([f.t, cum]); }
  pts.push([s.firstAns, cum]);                      // 边界处：思考已全部产出
  for (const f of r.frames.slice(ai)) { cum += f.len; pts.push([f.t, cum]); }
  const total = cum, tA = s.firstThink, tB = s.lastAns;
  const span = (tB - tA) / 1000;
  const timeAt = (frac) => {
    const want = total * frac;
    for (const [t, c] of pts) if (c >= want) return t;
    return tB;
  };
  const marks = [0, 0.2, 0.4, 0.6, 0.8, 1].map(timeAt);
  const rates = [];
  for (let i = 0; i < 5; i++) rates.push((total * 0.2) / ((marks[i + 1] - marks[i]) / 1000));
  const trend = rates[4] / rates[0];
  console.log(`  ${r.q.slice(0, 32).padEnd(34)}`
    + rates.map((x) => f1(x).padStart(7)).join('')
    + `   ${trend >= 1 ? '+' : ''}${((trend - 1) * 100).toFixed(0)}%`);
  console.log(`  ${''.padEnd(34)}整条 decode 窗口 ${f1(span)}s（首帧思考 → 末帧正文）`);
}

console.log('\n③ 对账：窗口 vs 原生（差值就是"首帧思考自带的 60 字"那段，它落在 TTFT 里）');
for (const r of live) {
  const s = split(r.frames);
  if (!s) continue;
  const st = r.stats;
  console.log(`  ${r.q.slice(0, 32).padEnd(34)}`
    + `原生 eval ${f1(st.evalMs / 1000)}s / ${st.evalTokens} tok　`
    + `窗口合计 ${f1(s.thinkSec + s.ansSec)}s　差 ${f1(st.evalMs / 1000 - s.thinkSec - s.ansSec)}s`);
}

const rateAll = live.map((r) => split(r.frames)).filter(Boolean);
console.log(`\n  中位：思考 ${f1(med(rateAll.map((s) => s.thinkSec)))}s / ${med(rateAll.map((s) => s.thinkChars))} 字`
  + `　正文 ${f1(med(rateAll.map((s) => s.ansSec)))}s / ${med(rateAll.map((s) => s.ansChars))} 字`);
console.log(`  中位速率：思考 ${f1(med(rateAll.map((s) => s.thinkRate)))} 字/秒　`
  + `正文 ${f1(med(rateAll.map((s) => s.ansRate)))} 字/秒`
  + `　⇒ 若两者相近，说明**同一个 decode 速率、只是字数不同**，不存在"写正文更快"这回事`);
console.log(`  补推帧（转正文之后才到的思考帧）中位 ${med(rateAll.map((s) => s.stragglers))} ——`
  + `正常应当是 0 或 1；若是 1，它的字数（<60）已算进思考字数、但时间算不到，误差 <1.2s。`);
