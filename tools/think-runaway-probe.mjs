/**
 * **跑飞检测**：问一句，把思考/正文**边收边落盘**，超时就掐断。
 *
 * 为什么单独写一个（`ask-once.mjs` 不够）：
 *   2026-09-23 实测到一次**跑飞** —— 单个请求在 Ollama 里生成到 **25756 token 还在继续**
 *   （`slot context shift, n_keep = 4, n_discard = 8189` 反复出现，即已经越过 num_ctx
 *   在丢窗口继续生成）。此前记录的最大思考是 4133 token。
 *   而现有探针都是**跑完才写盘** ⇒ 掐断的那一刻，恰恰是唯一能证明"它跑飞了、
 *   飞成什么样"的证据丢掉的时刻。
 *
 * 所以本探针做三件与其它探针不同的事：
 *   ① **边收边 append**（思考与正文分两个文件），掐断也留得下
 *   ② **打印进度**：每收 2000 字打一行（含已用秒数）—— 跑飞时你能看着它涨
 *   ③ **墙钟硬上限**（默认 120s）到点主动 abort，不把整台机器占住
 *
 * 用法：node tools/think-runaway-probe.mjs "问题" [秒数上限]
 * 产物：$KB_RUN_DIR（没设就当前目录）下 runaway-thinking.txt / runaway-answer.txt
 */
import fs from 'node:fs';
import path from 'node:path';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const OUT = process.env.KB_RUN_DIR || '.';
const question = process.argv[2];
const CAP = parseInt(process.argv[3] || '120', 10);
if (!question) {
  console.error('用法: node tools/think-runaway-probe.mjs "问题" [秒数上限]');
  process.exit(1);
}

const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const tf = path.join(OUT, 'runaway-thinking.txt');
const af = path.join(OUT, 'runaway-answer.txt');
fs.writeFileSync(tf, '');
fs.writeFileSync(af, '');

const t0 = Date.now();
const st = await c.openStream('POST', '/api/chat/stream',
  { question, strategy: 'agent', model: 'local/qwen3:4b' }, { token: TOKEN });

const ctrl = new AbortController();
const timer = setTimeout(() => {
  console.log(`\n!! 到 ${CAP}s 上限，主动掐断（已落盘的内容仍在 ${tf} / ${af}）`);
  ctrl.abort();
}, CAP * 1000);

let think = 0, ans = 0, lastMark = 0;
const counts = {};
const reader = st.response.body.getReader();
const dec = new TextDecoder();
let buf = '';
try {
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let name = 'message';
      const data = [];
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) name = line.slice(6).trim();
        else if (line.startsWith('data:')) data.push(line.slice(5).trim());
      }
      if (!data.length) continue;
      counts[name] = (counts[name] || 0) + 1;
      let d; try { d = JSON.parse(data.join('\n')); } catch { d = {}; }
      const p = st.decrypt(d);
      if (name === 'thinking') {
        const s = String(p.t ?? '');
        think += s.length;
        fs.appendFileSync(tf, s);            // **边收边落**
      } else if (name === 'answer') {
        const s = String(p.t ?? '');
        ans += s.length;
        fs.appendFileSync(af, s);
      }
      const tot = think + ans;
      if (tot - lastMark >= 2000) {
        lastMark = tot;
        console.log(`  ${((Date.now() - t0) / 1000).toFixed(0)}s　思考 ${think} 字 / 正文 ${ans} 字`);
      }
    }
  }
} catch (e) {
  console.log(`  连接中断：${String(e.message || e).slice(0, 60)}`);
}
clearTimeout(timer);
console.log(`\n结束：${((Date.now() - t0) / 1000).toFixed(0)}s　`
  + `思考 ${think} 字 / 正文 ${ans} 字　事件 ${JSON.stringify(counts)}`);
console.log(`产物：${tf}　${af}`);
