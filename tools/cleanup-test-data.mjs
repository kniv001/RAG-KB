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
import path from 'node:path';
import { makeClient } from './kb-client.mjs';

/**
 * 从测试脚本里**自动提取**它们发出过的问题。
 *
 * 为什么不再手写模式表：那份表会随脚本演进而过期。实际上已经过期过一次 ——
 * 早期测试的问题带「（核对 xxxx）」随机后缀，模式表就照着那个写；
 * 后来改成「先清回答缓存、用自然问法」，后缀没了，模式表却还在匹配后缀，
 * 于是一整批新产生的测试会话全都漏掉了，混进了保留列表。
 *
 * 提取规则刻意收紧：只认引号里完整的一行、且看着像个问句的字符串。
 * 宁可漏，不可误删 —— 用户自己打的字不该被脚本吃掉。
 */
function questionsFromTools() {
  const out = new Set();
  const dir = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
  let files = [];
  try { files = fs.readdirSync(dir); } catch { return out; }
  for (const f of files) {
    if (!f.endsWith('.mjs') || f === 'cleanup-test-data.mjs' || f === 'kb-client.mjs') continue;
    const src = fs.readFileSync(path.join(dir, f), 'utf8');
    for (const m of src.matchAll(/['"`]([^'"`\n]{6,80})['"`]/g)) {
      const s = m[1].trim();
      // 滤掉模板字符串里被引号切断的碎片，例如 `${n > 1 ?` `-> ${bad.status} ${...`
      if (s.includes('${') || s.includes('\\n')) continue;
      // 只要**含**问号即可，不要求以问号结尾 ——
      // 「你是谁？用一句话说。」结尾是句号，用 $ 锚定会把它漏掉（踩过）
      if (!/[？?]/.test(s)) continue;
      out.add(s);
    }
  }
  return out;
}

const TOOL_QUESTIONS = questionsFromTools();

// 临时用 ask-once.mjs 问过的那些不在任何脚本源码里，脚本扫描扫不到，
// 所以那个工具会把问过的问题追加到这个文件，这里一并并入。
try {
  for (const line of fs.readFileSync('data/asked-questions.txt', 'utf8').split('\n')) {
    const s = line.trim();
    if (s.length >= 6) TOOL_QUESTIONS.add(s);
  }
} catch { /* 没有这个文件说明还没用过那个工具 */ }

/** 会话标题在库里被截断到 30 字，所以按前缀比 */
function cameFromTools(title) {
  if (!title || title.length < 8) return false;
  for (const q of TOOL_QUESTIONS) {
    if (q === title || (q.length > title.length && q.startsWith(title))) return true;
  }
  return false;
}

const APPLY = process.argv.includes('--apply');
/**
 * 连「分不清是谁产生的」会话也一起删。
 *
 * 默认不做，因为这些会话里有几个我无法证明来源 —— 比如
 * 「校园网为什么拒绝 IPv6 入站？依据是什么？」，它是 ask-test.mjs /
 * timing-probe.mjs 的默认测试问题，但那个话题本身也是用户提出来的，
 * 脚本产生的和用户自己问的，在库里长得一模一样。
 *
 * 这类判断不该由脚本替用户做，所以做成显式开关。
 */
const ALL_CONVS = process.argv.includes('--all-conversations');
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

if (process.argv.includes('--show-questions')) {
  console.log(`从测试脚本里提取到 ${TOOL_QUESTIONS.size} 条问题：`);
  for (const q of [...TOOL_QUESTIONS].sort()) console.log('  ' + q);
  process.exit(0);
}

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
const isTestConv = (x) => hit(CONV_TEST, x.title || '') || cameFromTools(x.title || '');
const delConvs = ALL_CONVS ? convs : convs.filter(isTestConv);
const keepConvs = ALL_CONVS ? [] : convs.filter((x) => !isTestConv(x));
if (ALL_CONVS) {
  console.log('\n⚠️  --all-conversations：所有会话都会被删除，包括无法判定来源的那些。');
  for (const x of convs) console.log(`      ${x.id}  ${(x.title || '').slice(0, 34)}`);
}

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
