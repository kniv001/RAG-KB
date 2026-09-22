/**
 * **跨会话记忆的验收探针** —— 换个新会话，看它还记不记得。
 *
 * 这是「记忆独立于会话摘要」这件事**唯一算数的验收**：
 * 会话摘要按会话隔离（`conversations.summary`），长期记忆是全局的
 * （`memory_items`）。所以必须**不给 convId**、开一个全新会话去问 ——
 * 在同一个会话里问是测不出区别的（那时两边都有）。
 *
 * 判据：答案里出现只可能来自长期记忆的内容（知识库里没有的事实）。
 *
 * 用法：node tools/cross-conv-probe.mjs "问题" [期望出现的字面量]
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const q = process.argv[2] || '我之前在别的会话里定过一个内部代号，你还记得是什么吗？';
const want = process.argv[3] || null;
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(process.env.KB_BASE || 'http://127.0.0.1:8080');
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
const T = `Bearer ${login.body.data.accessToken}`;

// **不给 convId** ⇒ 全新会话
const st = await c.openStream('POST', '/api/chat/stream',
  { question: q, strategy: 'classic', model: 'local/qwen3:4b' }, { token: T });
let txt = '';
await readSse(st.response, (n, raw) => {
  let d; try { d = JSON.parse(raw); } catch { d = raw; }
  const x = st.decrypt(d);
  if (n === 'answer') txt += x.t || '';
  if (n === 'meta') console.log(`新会话 ${x.convId}　hasSummary=${x.hasSummary}`);
});
console.log('--- 回答 ---\n' + txt.slice(0, 500));
if (want) {
  const ok = txt.includes(want);
  console.log(`\n${ok ? '✅' : '❌'} ${ok ? '命中' : '没命中'}期望字面量「${want}」`);
  process.exit(ok ? 0 : 1);
}
