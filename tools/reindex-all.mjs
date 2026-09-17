/**
 * 全量重建索引（分块器改过之后必须做一次）。
 *
 * 索引是覆盖式的（IndexService 先 deleteByDoc 再插入），所以重复跑安全。
 * 逐个提交并等任务完成 —— 同时跑会一起抢同一块 GPU，反而更慢。
 *
 *   node tools/reindex-all.mjs
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);

const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
if (login.body?.code !== 0) { console.error('登录失败', login.body?.message); process.exit(1); }
const TOKEN = `Bearer ${login.body.data.accessToken}`;
const call = (m, p, b) => c.call(m, p, b, { token: TOKEN });

const docs = (await call('GET', '/api/docs')).body.data.docs || [];
console.log(`共 ${docs.length} 篇，逐篇重建…\n`);

let ok = 0, fail = 0, chunks = 0;
const t0 = Date.now();
for (const d of docs) {
  const t = await call('POST', `/api/docs/${d.id}/index`);
  const taskId = t.body?.data?.taskId;
  if (!taskId) { fail++; console.log(`❌ ${d.name.slice(0, 34)} 提交失败：${t.body?.message}`); continue; }

  let st = null;
  for (let i = 0; i < 300; i++) {
    await new Promise((r) => setTimeout(r, 500));
    const q = await call('GET', `/api/docs/tasks/${taskId}`);
    st = q.body?.data;
    if (st && (st.status === 'done' || st.status === 'error')) break;
  }
  if (st?.status === 'done') {
    ok++;
    chunks += st.result?.chunks ?? 0;
    console.log(`✅ ${d.name.slice(0, 34).padEnd(36)} ${st.result?.chunks ?? '?'} 块`);
  } else {
    fail++;
    console.log(`❌ ${d.name.slice(0, 34)} ${st?.status}: ${st?.message || '超时'}`);
  }
}
console.log(`\n完成：成功 ${ok}，失败 ${fail}，共 ${chunks} 块，耗时 ${((Date.now() - t0) / 1000).toFixed(0)}s`);
