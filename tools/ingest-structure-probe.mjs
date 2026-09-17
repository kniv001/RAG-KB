/**
 * 验「入库时保留标题层级」：抓几篇新页面，看落盘的 .md 有没有结构。
 *
 * 对照的是改造前那批：41 篇合计只有 64 个标题行，且绝大多数是抓取时加的那行标题。
 *
 *   node tools/ingest-structure-probe.mjs [主题] [篇数]
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const topic = process.argv[2] || 'Redis 持久化 RDB AOF 原理';
const want = Number(process.argv[3] || 2);

const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;
const call = (m, p, b) => c.call(m, p, b, { token: TOKEN });

const s = await call('POST', '/api/web/search', { query: topic, count: want + 3, site: 'cnblogs.com' });
const hits = (s.body?.data?.results || []).filter((h) => /^https?:\/\//.test(h.url)).slice(0, want);
console.log(`搜索到 ${hits.length} 条，开始入库…\n`);

const r = await call('POST', '/api/web/ingest', { urls: hits.map((h) => h.url) });
const ids = [];
for (const it of r.body?.data?.results || []) {
  if (it.docId && !it.error) { ids.push(it.docId); console.log(`✅ ${it.docId}  ${it.chars} 字  ${String(it.url).slice(0, 56)}`); }
  else { console.log(`❌ ${String(it.url).slice(0, 56)}  ${it.error || ''}`); }
}

await new Promise((res) => setTimeout(res, 3000));
console.log('\n落盘文件的结构：');
for (const id of ids) {
  const f = `D:/vs/rag-kb/data/uploads/${id}.md`;
  if (!fs.existsSync(f)) { console.log(`  ${id}: 找不到文件`); continue; }
  const t = fs.readFileSync(f, 'utf8');
  const heads = t.split('\n').filter((l) => /^#{1,6}\s/.test(l));
  const paras = t.split(/\n\s*\n/).filter((x) => x.trim());
  console.log(`\n  ${id}：${t.length} 字，标题 ${heads.length} 个，段落 ${paras.length} 个`);
  heads.slice(0, 8).forEach((h) => console.log(`     ${h.slice(0, 52)}`));
}
fs.writeFileSync('D:/vs/rag-kb-java/data/_structure-probe.json', JSON.stringify(ids), 'utf8');
