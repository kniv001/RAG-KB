/**
 * 工程功能端到端：上传 → 异步索引（含缓存）→ 列表 → 多轮对话 → 缓存统计。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const CRED = 'D:/vs/rag-kb/data/auth.json.migrated';

let pass = 0, fail = 0;
const check = (l, c, e = '') => { c ? (pass++, console.log(`  [PASS] ${l} ${e}`)) : (fail++, console.log(`  [FAIL] ${l} ${e}`)); };

const cred = JSON.parse(fs.readFileSync(CRED, 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
if (login.status !== 200) { console.log('登录失败'); process.exit(1); }
const TOKEN = `Bearer ${login.body.data.accessToken}`;
console.log(`已登录 ${cred.user}\n`);

// ---------- 1) 上传（multipart 不走加密过滤器，用明文 Authorization）----------
const DOC = `# Java 版工程功能验证文档

## 缓存设计

三层缓存：向量缓存按「文本 + 模型」哈希，解析缓存按「文件内容」哈希，
回答缓存按「问题 + 上下文哈希 + 历史哈希 + 模型」哈希。

键里含所有影响结果的参数，所以改了输入就自动不命中，不存在返回陈旧结果的窗口。

## 异步索引的原因

Cloudflare 免费版对源站响应有 100 秒硬超时且无法延长，
而大文档的解析加向量化很容易超过，同步接口必被掐断。
改成立即返回 202 加任务 id、前端轮询进度后，耗时就与超时无关了。
`;
const form = new FormData();
form.append('file', new Blob([DOC], { type: 'text/markdown' }), 'java-engineering-test.md');
const up = await fetch(`${BASE}/api/docs/upload`, { method: 'POST', headers: { Authorization: TOKEN }, body: form });
const upj = await up.json();
check('上传 -> 200', up.status === 200, `-> ${up.status} ${JSON.stringify(upj).slice(0, 100)}`);
const docId = upj?.data?.id;
check('拿到 docId', !!docId, docId);

// ---------- 2) 异步索引 ----------
console.log('\n--- 异步索引 ---');
const t0 = Date.now();
const idx = await c.call('POST', `/api/docs/${docId}/index`, undefined, { token: TOKEN });
check('建索引 -> 202（立即返回，不等模型）', idx.status === 202, `-> ${idx.status} ${Date.now() - t0}ms`);
const taskId = idx.body?.data?.taskId;
check('拿到 taskId', !!taskId, taskId);

let task = null;
for (let i = 0; i < 120; i++) {
  await new Promise(r => setTimeout(r, 1000));
  const t = await c.call('GET', `/api/docs/tasks/${taskId}`, undefined, { token: TOKEN });
  task = t.body?.data;
  process.stdout.write(`\r  轮询 ${i + 1}s: ${task?.status} ${task?.progress}/${task?.total} ${task?.message || ''}          `);
  if (task?.status !== 'running') break;
}
console.log('');
check('索引任务完成', task?.status === 'done', `-> ${task?.status} ${task?.message || ''}`);
console.log(`  结果: ${JSON.stringify(task?.result)}`);

// ---------- 3) 重建索引（应触发缓存命中，明显更快）----------
console.log('\n--- 重建索引（验证缓存）---');
const t1 = Date.now();
const idx2 = await c.call('POST', `/api/docs/${docId}/index`, undefined, { token: TOKEN });
let task2 = null;
for (let i = 0; i < 120; i++) {
  await new Promise(r => setTimeout(r, 500));
  const t = await c.call('GET', `/api/docs/tasks/${idx2.body.data.taskId}`, undefined, { token: TOKEN });
  task2 = t.body?.data;
  if (task2?.status !== 'running') break;
}
const rebuildMs = Date.now() - t1;
check('重建完成', task2?.status === 'done', `${rebuildMs}ms`);
console.log(`  重建耗时 ${rebuildMs}ms（首次 ${Date.now() - t0}ms 量级）`);

// ---------- 4) 文档列表 ----------
const list = await c.call('GET', '/api/docs', undefined, { token: TOKEN });
check('文档列表 -> 200', list.status === 200, `共 ${list.body?.data?.count} 篇`);
const mine = (list.body?.data?.docs || []).find(d => d.id === docId);
check('新文档状态为 indexed 且块数 > 0',
  mine?.status === 'indexed' && mine?.chunkCount > 0,
  `status=${mine?.status} chunks=${mine?.chunkCount} model=${mine?.embedModel}`);

// ---------- 5) 多轮对话 ----------
console.log('\n--- 多轮对话 ---');
const st1 = await c.openStream('POST', '/api/chat/stream',
  { question: '这个文档讲了哪三层缓存？', strategy: 'classic' }, { token: TOKEN });
let convId = null, ans1 = '';
await readSse(st1.response, (name, raw) => {
  const d = st1.decrypt(JSON.parse(raw));
  if (name === 'meta' && d?.convId) convId = d.convId;
  if (name === 'answer') ans1 += (d?.t || '');
});
check('第一轮对话拿到 convId', !!convId, convId);
check('第一轮回答非空', ans1.length > 0, `${ans1.length} 字`);
console.log(`  回答: ${ans1.slice(0, 100).replace(/\n/g, ' ')}…`);

const st2 = await c.openStream('POST', '/api/chat/stream',
  { question: '那异步索引是为什么？', convId, strategy: 'classic' }, { token: TOKEN });
let ans2 = '', convId2 = null;
await readSse(st2.response, (name, raw) => {
  const d = st2.decrypt(JSON.parse(raw));
  if (name === 'meta' && d?.convId) convId2 = d.convId;
  if (name === 'answer') ans2 += (d?.t || '');
});
check('追问接续同一会话', convId2 === convId, `${convId} -> ${convId2}`);
check('第二轮回答非空', ans2.length > 0, `${ans2.length} 字`);
console.log(`  追问回答: ${ans2.slice(0, 100).replace(/\n/g, ' ')}…`);

const convs = await c.call('GET', '/api/chat/conversations', undefined, { token: TOKEN });
check('会话列表含该会话', (convs.body?.data?.conversations || []).some(x => x.id === convId),
  `共 ${convs.body?.data?.count} 个`);
const detail = await c.call('GET', `/api/chat/conversations/${convId}`, undefined, { token: TOKEN });
check('会话明细含 4 条消息（2 问 2 答）', detail.body?.data?.messages?.length === 4,
  `${detail.body?.data?.messages?.length} 条`);

// ---------- 6) 缓存统计 ----------
const stats = await c.call('GET', '/api/cache/stats', undefined, { token: TOKEN });
check('缓存统计 -> 200', stats.status === 200, JSON.stringify(stats.body?.data?.stats));

console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
