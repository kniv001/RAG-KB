/**
 * 去重：同一篇内容被联网入库多次留下的副本。
 *
 * 由来：联网检索早先没有按 URL 去重，同一个网址搜两次就是两篇。测试脚本每跑一次
 * 加一篇，于是 B 站那篇 B-tree 存了 8 份、CSDN 那篇三层缓存存了 9 份 —— 它们占了
 * 库里三分之一的段数，并且是主题树最大两簇和「未归类」簇的成因。
 *
 * 保留策略：**同组里块数最多的那份**（块数少说明当时正文提取得不全），块数相同取最早。
 * 删除走应用的 DELETE 接口：它会连磁盘上的 .md 一起删，chunks 由外键级联。
 *
 *   node tools/dedup-docs.mjs --dry     # 只看计划
 *   node tools/dedup-docs.mjs           # 执行
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const DRY = process.argv.includes('--dry');
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;
const call = (m, p, b) => c.call(m, p, b, { token: TOKEN });

const docs = (await call('GET', '/api/docs')).body.data.docs || [];
console.log(`库中文档 ${docs.length} 篇，共 ${docs.reduce((s, d) => s + (d.chunkCount || 0), 0)} 块\n`);

// 按「同源」分组：联网文档用 source_url，上传文档用文件名
const groups = new Map();
for (const d of docs) {
  const key = d.sourceUrl ? `url:${d.sourceUrl}` : `name:${d.name}`;
  if (!groups.has(key)) groups.set(key, []);
  groups.get(key).push(d);
}

const doomed = [], kept = [];
for (const [key, list] of groups) {
  if (list.length < 2) continue;
  // 块数最多优先，其次最早的
  list.sort((a, b) => (b.chunkCount || 0) - (a.chunkCount || 0)
    || String(a.uploadedAt).localeCompare(String(b.uploadedAt)));
  kept.push({ key, doc: list[0], total: list.length });
  doomed.push(...list.slice(1));
}

console.log('保留：');
for (const k of kept) {
  console.log(`  ${k.doc.chunkCount} 块  ${k.doc.name.slice(0, 38)}  （同源共 ${k.total} 份）`);
}
console.log(`\n拟删除 ${doomed.length} 篇：`);
for (const d of doomed) {
  console.log(`  ${String(d.chunkCount).padStart(3)} 块  ${d.name.slice(0, 38)}  ${d.id}`);
}
console.log(`\n删掉的段数约 ${doomed.reduce((s, d) => s + (d.chunkCount || 0), 0)}，`
  + `保留的段数约 ${kept.reduce((s, k) => s + (k.doc.chunkCount || 0), 0)}`);

if (DRY) {
  console.log('\n（--dry，未执行）');
} else {
  let ok = 0, fail = 0;
  for (const d of doomed) {
    const r = await call('DELETE', `/api/docs/${d.id}`);
    if (r.body?.code === 0) ok++; else { fail++; console.log(`  ❌ ${d.id}: ${r.body?.message}`); }
  }
  console.log(`\n已删除 ${ok} 篇，失败 ${fail} 篇`);
  fs.writeFileSync('D:/vs/rag-kb-java/data/_dedup-removed.json',
    JSON.stringify(doomed, null, 2), 'utf8');
  console.log('删除清单已写入 data/_dedup-removed.json');
}
