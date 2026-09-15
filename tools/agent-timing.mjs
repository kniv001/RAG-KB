/**
 * 端到端测 agent 各阶段耗时：给每个 SSE 事件打时间戳，算出阶段间隔。
 *
 * 重点是 plan 和 assess —— 这两步原本各要 20~50 秒（模型为吐一行 JSON
 * 先写几千 token 的推理），开启结构化输出后应当降到 1~2 秒。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const QUESTIONS = process.argv.slice(2);
const qs = QUESTIONS.length
  ? QUESTIONS
  : [
      '三层缓存分别省掉了什么开销？它们的键是怎么设计的，为什么改了输入就自动不命中？',
      '为什么要显式指定 num_ctx？不指定会怎样？',
    ];

for (const question of qs) {
  console.log('\n' + '='.repeat(76));
  console.log(`问题：${question}`);
  console.log('='.repeat(76));

  const t0 = Date.now();
  const marks = [];
  const st = await c.openStream('POST', '/api/ask/stream',
    { question, strategy: 'agent', model: 'local/qwen3:4b' }, { token: TOKEN });

  let answerChars = 0;
  await readSse(st.response, (name, raw) => {
    let d; try { d = JSON.parse(raw); } catch { d = raw; }
    const dec = st.decrypt(d);
    const at = Date.now() - t0;
    if (name === 'plan') {
      marks.push({ at, label: 'plan', detail: (dec.queries || []).join(' | ') });
    } else if (name === 'retrieve') {
      marks.push({ at, label: 'retrieve', detail: `${dec.hitCount} 命中` });
    } else if (name === 'assess') {
      marks.push({ at, label: 'assess', detail: `enough=${dec.enough} ${dec.reason || ''}`.slice(0, 60) });
    } else if (name === 'answer') {
      answerChars += (dec.t || '').length;
      if (!marks.some((m) => m.label === 'answer-first')) {
        marks.push({ at, label: 'answer-first', detail: '首个 token' });
      }
    }
  });
  const end = Date.now() - t0;

  let prev = 0;
  console.log('阶段            时刻      本段耗时   说明');
  console.log('-'.repeat(76));
  for (const m of marks) {
    const seg = m.at - prev;
    prev = m.at;
    const flag = seg > 5000 ? '  ← 慢' : '';
    console.log(
      `${m.label.padEnd(14)} ${String(m.at + 'ms').padStart(8)} ${String(seg + 'ms').padStart(10)}   ${m.detail}${flag}`
    );
  }
  const tail = end - prev;
  console.log(`${'生成'.padEnd(14)} ${String(end + 'ms').padStart(8)} ${String(tail + 'ms').padStart(10)}   ${answerChars} 字`);
  console.log('-'.repeat(76));
  console.log(`总计 ${end}ms`);
}
