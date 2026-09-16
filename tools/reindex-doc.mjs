/**
 * 对已有文档重新建索引，用来验证「重启后文件还在不在」。
 *
 *   node tools/reindex-doc.mjs <docId>
 *
 * 重启后重新索引之所以是个好判据：索引的每一步都要读磁盘上的原文件。
 * 文件若落在临时目录且随重启换了个新目录，这一步必然报「文件不存在」。
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const docId = process.argv[2];
if (!docId) {
  console.error('用法: node tools/reindex-doc.mjs <docId>');
  process.exit(1);
}

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const docs = (await c.call('GET', '/api/docs', undefined, { token: TOKEN })).body.data.docs || [];
const doc = docs.find((d) => d.id === docId);
console.log(`文档：${doc ? `${doc.name}（${doc.bytes} B，${doc.chunkCount} 块）` : '不存在'}`);
if (!doc) process.exit(1);

const t0 = Date.now();
const t = await c.call('POST', `/api/docs/${docId}/index`, undefined, { token: TOKEN });
if (t.body?.code !== 0) {
  console.log(`❌ 提交失败：${t.body?.message}`);
  process.exit(1);
}

let st = null;
for (let i = 0; i < 60; i++) {
  await new Promise((r) => setTimeout(r, 700));
  st = (await c.call('GET', `/api/docs/tasks/${t.body.data.taskId}`, undefined, { token: TOKEN })).body.data;
  if (st.status !== 'running') break;
}
console.log(`状态：${st?.status}  ${st?.message || ''}  （${Date.now() - t0}ms）`);

if (st?.status === 'done') {
  const ch = await c.call('GET', `/api/docs/${docId}/chunks`, undefined, { token: TOKEN });
  console.log(`✅ 文件可读，重建索引成功：${ch.body?.data?.chunks?.length ?? 0} 块`);
  process.exit(0);
}
console.log('❌ 重建索引失败 —— 文件多半不在磁盘上了');
process.exit(1);
