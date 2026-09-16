/**
 * 清理测试数据。
 *
 * 原则：**只删能证明是测试造出来的，其余一律保留并报告。**
 * 库里混着用户自己的文件与会话，误删不可恢复 —— 所以宁可漏删。
 *
 * 默认是 dry-run，只列出打算删什么；加 --apply 才真的执行。
 *
 *   node tools/cleanup-test-data.mjs            # 看清单
 *   node tools/cleanup-test-data.mjs --apply    # 真删
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const APPLY = process.argv.includes('--apply');
const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

// 这些文件名是历次测试脚本自己生成的，模式明确
const DOC_TEST = [
  /^enc-test-\d+\.md$/,          // tools/encrypted-upload-test.mjs
  /^e2e-[a-z0-9]+\.md$/,         // tools/frontend-e2e-cdp.mjs
  /^java-engineering-test\.md$/, // 早期工程功能测试
  /^test_doc\.md$/,              // 最初的连通性测试
];

// 会话标题是首轮提问，只认测试脚本里独有的措辞。
//
// 刻意不匹配「你是谁」「校园网为什么拒绝 IPv6 入站」这类 —— 它们虽然也出现在
// 我的脚本或文档里，但那是引用，问题本身是用户问的。宁可漏删，误删不可恢复。
const CONV_TEST = [
  /^请记住一个(项目)?代号/,       // tools/history-index-test.mjs / summary-test.mjs
  // 注意不要写右括号：标题在库里被截断到 30 字，右括号经常被切掉，
  // 带上它就会一个都匹配不到（第一版就是这么漏的）
  /（核对 [a-z0-9]+/,             // frontend-e2e-cdp.mjs 为避开缓存加的随机后缀
];

const hit = (patterns, s) => patterns.some((p) => p.test(s));

console.log(APPLY ? '模式：执行删除\n' : '模式：预览（加 --apply 才真删）\n');

// ── 文档 ──
const docs = (await c.call('GET', '/api/docs', undefined, { token: TOKEN })).body.data.docs || [];
const delDocs = docs.filter((d) => hit(DOC_TEST, d.name));
const keepDocs = docs.filter((d) => !hit(DOC_TEST, d.name));

console.log(`文档：共 ${docs.length}，删 ${delDocs.length}，留 ${keepDocs.length}`);
for (const d of delDocs) console.log(`  ✕ ${d.name}  ${d.bytes} B  ${d.chunkCount} 块`);
for (const d of keepDocs) console.log(`  ✓ 保留 ${d.name}  ${d.bytes} B  ← 匹配不上测试模式`);

// ── 会话 ──
const convs = (await c.call('GET', '/api/chat/conversations?limit=200', undefined, { token: TOKEN }))
  .body.data.conversations || [];
const delConvs = convs.filter((x) => hit(CONV_TEST, x.title || ''));
const keepConvs = convs.filter((x) => !hit(CONV_TEST, x.title || ''));

console.log(`\n会话：共 ${convs.length}，删 ${delConvs.length}，留 ${keepConvs.length}`);
for (const x of keepConvs.slice(0, 12)) {
  console.log(`  ✓ 保留 ${x.id}  ${(x.title || '').slice(0, 30)}`);
}
if (keepConvs.length > 12) console.log(`  … 另有 ${keepConvs.length - 12} 个`);

if (!APPLY) {
  console.log('\n以上是预览。确认无误后加 --apply 执行。');
  process.exit(0);
}

console.log('\n执行删除…');
let okDoc = 0, okConv = 0;
for (const d of delDocs) {
  const r = await c.call('DELETE', `/api/docs/${d.id}`, undefined, { token: TOKEN });
  if (r.body?.code === 0) { okDoc++; console.log(`  ✕ ${d.name}`); }
  else console.log(`  ! ${d.name} 失败：${r.body?.message}`);
}
for (const x of delConvs) {
  const r = await c.call('DELETE', `/api/chat/conversations/${x.id}`, undefined, { token: TOKEN });
  if (r.body?.code === 0) okConv++;
  else console.log(`  ! ${x.id} 失败：${r.body?.message}`);
}
console.log(`\n完成：删除 ${okDoc} 篇文档、${okConv} 个会话；保留 ${keepDocs.length} 篇文档、${keepConvs.length} 个会话。`);
