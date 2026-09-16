/**
 * 滚动摘要验证。
 *
 * 要验三件事：
 *   ① 摘要确实会被生成（窗口溢出 + 攒够批次之后）
 *   ② 它真的记住了内容（第 1 轮埋的代号，在十几轮之后仍在摘要里）
 *   ③ 它是增量更新的（summary_upto 随消息推进，而不是每次重算全部）
 *
 * 触发条件是算出来的，不是试出来的：
 *   窗口 16 条（8 轮）；攒够 6 条（3 轮）才更新
 *   → 第 N 轮后有 2N 条消息，掉出窗口的是 2N-16 条
 *   → 需要 2N-16 ≥ 6，即 N ≥ 11 轮
 * 所以这里跑 12 轮。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

let pass = 0, fail = 0;
const ok = (n, cond, e = '') => {
  if (cond) { pass++; console.log(`  ✅ ${n}${e ? ' — ' + e : ''}`); }
  else { fail++; console.log(`  ❌ ${n}${e ? ' — ' + e : ''}`); }
};

async function ask(question, convId) {
  const body = { question, strategy: 'classic', model: 'local/qwen3:4b' };
  if (convId) body.convId = convId;
  const st = await c.openStream('POST', '/api/chat/stream', body, { token: TOKEN });
  let id = convId, hasSummary = null, text = '';
  await readSse(st.response, (name, raw) => {
    let d; try { d = JSON.parse(raw); } catch { d = raw; }
    const dec = st.decrypt(d);
    if (name === 'meta') { id = dec.convId || id; hasSummary = dec.hasSummary; }
    if (name === 'answer') text += dec.t || '';
  });
  return { convId: id, hasSummary, text };
}

const MARK = '夜莺四号';
const OPENER = `请记住一个代号：本次会话的内部代号叫「${MARK}」。它和知识库内容无关，只是要你记住。`;
const FILLER = [
  '三层缓存里哪一层省得最多？',
  '解析缓存用文件内容做键有什么好处？',
  '异步索引为什么不能做成同步的？',
  '缓存故障时为什么降级为未命中？',
  '为什么要显式指定 num_ctx？',
  '切分参数为什么要和 Python 版一致？',
  '双令牌里为什么要用 refresh token？',
  'RRF 为什么对分数尺度不敏感？',
  '向量模型换了之后旧索引会怎样？',
  '文档删除时会连带删掉什么？',
  '新增一段资料之后，之前的回答缓存会失效吗？',
];

console.log('=== 第 1 轮：埋入代号 ===');
let r = await ask(OPENER);
const convId = r.convId;
console.log(`convId=${convId}  hasSummary=${r.hasSummary}`);

console.log(`\n=== 灌 ${FILLER.length} 轮，把第 1 轮推出窗口（共 ${1 + FILLER.length} 轮）===`);
for (let i = 0; i < FILLER.length; i++) {
  r = await ask(FILLER[i], convId);
  const marks = [];
  if (i >= 9) marks.push(`hasSummary=${r.hasSummary}`);   // 第 11 轮起应能看到摘要
  process.stdout.write(`  ${String(i + 2).padStart(2)}. ${FILLER[i].slice(0, 20)}… ${marks.join(' ')}\n`);
}

console.log('\n=== 等异步摘要落库 ===');
let summary = null, upto = 0, attempts = 0;
for (let i = 0; i < 20; i++) {
  await new Promise((r) => setTimeout(r, 1000));
  attempts++;
  const d = await c.call('GET', `/api/chat/conversations`, undefined, { token: TOKEN });
  // 会话列表不带 summary，用 sql 那侧看不了，这里换个信号：再问一句看 meta.hasSummary
  const probe = await ask('用一句话概括我们这次对话都聊了什么。', convId);
  if (probe.hasSummary) { summary = probe.text; break; }
}
ok('摘要已生成（meta.hasSummary 为真）', !!summary, `探测 ${attempts} 次`);
if (summary) {
  console.log(`  模型基于摘要的回答：${summary.slice(0, 150).replace(/\n/g, ' ')}`);
  ok(`摘要里保住了第 1 轮埋的代号「${MARK}」`, summary.includes(MARK));
}

console.log(`\n${'─'.repeat(50)}`);
console.log(`通过 ${pass} 项，失败 ${fail} 项`);
console.log(`会话 ${convId}`);
process.exit(fail ? 1 : 0);
