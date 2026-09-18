/** 给单篇文档重建索引并等完成（验证语境行管线）。
 *  用法：node tools/index-one.mjs <文档名关键词> */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';
const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
if (login.body?.code !== 0) { console.error('登录失败'); process.exit(1); }
const TOKEN = `Bearer ${login.body.data.accessToken}`;
const call = (m, p, b) => c.call(m, p, b, { token: TOKEN });
const kw = process.argv[2] || '';
const docs = (await call('GET', '/api/docs')).body.data.docs || [];
const d = docs.find(x => x.name.includes(kw));
if (!d) { console.error('没找到含「' + kw + '」的文档'); process.exit(1); }
console.log(`重建：${d.name}（${d.chunks ?? '?'} 块）`);
const t0 = Date.now();
const t = await call('POST', `/api/docs/${d.id}/index`);
const taskId = t.body?.data?.taskId;
if (!taskId) { console.error('提交失败：', JSON.stringify(t.body).slice(0, 200)); process.exit(1); }
for (let i = 0; i < 200; i++) {
  await new Promise(r => setTimeout(r, 3000));
  const st = await call('GET', `/api/docs/tasks/${taskId}`);
  const s = st.body?.data;
  if (!s) continue;
  if (s.status === 'done' || s.status === 'failed') {
    console.log(`  ${s.status}  用时 ${((Date.now() - t0) / 1000).toFixed(0)}s  块 ${s.chunks ?? '?'}  ${s.message || ''}`);
    process.exit(s.status === 'done' ? 0 : 1);
  }
  if (i % 5 === 0) console.log(`  …${s.status || ''} ${s.message || ''}`);
}
console.error('超时'); process.exit(1);
