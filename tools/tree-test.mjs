/**
 * 主题树验证。
 *
 * 分三步：
 *   ① 先灌若干**主题明显不同**的资料（不然聚类没有意义，簇之间没有可分的结构）
 *   ② 建树，看分簇与概括的质量
 *   ③ 验概览真的进了提示词、且回答会用上它
 *
 * 第 ③ 步是重点：树建得再漂亮，如果只是躺在库里没进提示词，那它一点用没有。
 * 所以这里问一个知识库必然没有的问题，看回答会不会点出「库里覆盖了哪些方向」——
 * 这正是加这一层的目的。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;
const call = (m, p, b) => c.call(m, p, b, { token: TOKEN });

let pass = 0, fail = 0;
const ok = (n, cond, e = '') => {
  if (cond) { pass++; console.log(`  ✅ ${n}${e ? ' — ' + e : ''}`); }
  else { fail++; console.log(`  ❌ ${n}${e ? ' — ' + e : ''}`); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 主题刻意分得开：向量聚类是否有效，取决于语料本身有没有可分结构。
// 全灌同一主题的话，簇之间边界模糊，测不出东西。
const TOPICS = process.argv.slice(2).length ? process.argv.slice(2) : [
  'redis 缓存穿透 雪崩 解决方案',
  'PostgreSQL 索引原理 B树',
  '番茄炒蛋 做法 步骤',
];

console.log('=== 1. 灌入不同主题的资料 ===');
for (const q of TOPICS) {
  const s = await call('POST', '/api/web/search', { query: q, count: 2 });
  const hits = (s.body?.data?.results || []).slice(0, 2);
  if (!hits.length) { console.log(`  「${q}」没搜到，跳过`); continue; }
  const ing = await call('POST', '/api/web/ingest', { urls: hits.map((h) => h.url) });
  const good = (ing.body?.data?.results || []).filter((r) => !r.error);
  console.log(`  「${q}」→ 入库 ${good.length} 篇`);
  for (const g of good) {
    for (let i = 0; i < 60; i++) {
      await sleep(700);
      const t = await call('GET', `/api/docs/tasks/${g.taskId}`);
      if (t.body?.data?.status !== 'running') break;
    }
  }
}

const status0 = await call('GET', '/api/tree/status');
const chunks = status0.body?.data?.currentChunkCount ?? 0;
console.log(`\n  当前已索引块数：${chunks}`);
if (chunks < 6) {
  console.log('  块太少（<6），聚类没有意义，跳过建树测试');
  process.exit(0);
}

console.log('\n=== 1.5 检索质量：完全无关的问题不该召回资料 ===');
{
  // 这条是为一个真实缺陷补的。原先关键词通道的准入是「命中数 > 0」，
  // 而检索词里有大量中文二元组，常用词（作用、计算）几乎出现在任何中文段落里 ——
  // 问量子纠缠，知识库里全是 PostgreSQL 文档，却召回了 11 段。
  // 更糟的是那些块只在关键词通道出现、distance 为 null，而融合时的距离守卫
  // 写的是「有距离才比」，于是它们一路畅通，模型看到「有资料」就不再声明
  // 知识库没有，直接拿通用知识作答 —— 那正是这套系统唯一的信任边界。
  const q = '量子纠缠在量子计算中的作用是什么？';
  const r = await call('POST', '/api/ask', { question: q, strategy: 'classic' });
  const n = (r.body?.data?.sources || []).length;
  ok('无关问题召回的段落数应当很少', n < 3, `召回了 ${n} 段`);
}

console.log('\n=== 2. 建树 ===');
const b = await call('POST', '/api/tree/build');
ok('建树成功', b.status === 200 && b.body?.code === 0, b.body?.message || '');
const r = b.body?.data || {};
console.log(`  ${r.chunks} 块 → ${r.clusters} 个主题，耗时 ${r.elapsedMs}ms`);
ok('簇数在合理范围（2~12）', r.clusters >= 2 && r.clusters <= 12, `${r.clusters} 个`);
ok('耗时在可接受范围（<100s，不超过 CF 源站超时）', r.elapsedMs < 100000, `${r.elapsedMs}ms`);

console.log('\n=== 3. 分簇与概括的质量 ===');
const st = await call('GET', '/api/tree/status');
const topics = st.body?.data?.topics || [];
ok('状态里带出了主题列表', topics.length === r.clusters);
console.log('  主题：');
for (const t of topics) {
  console.log(`    · ${t.label}（${t.size} 块 / ${t.docs} 篇）${t.summary.slice(0, 46)}`);
}
ok('每个主题都有名字', topics.every((t) => t.label && t.label.length > 0));
ok('每个主题都有概括', topics.every((t) => t.summary && t.summary.length > 5));
ok('主题名不是敷衍的通用词',
   topics.every((t) => !/^(技术资料|资料|文档|未命名)$/.test(t.label)),
   topics.map((t) => t.label).join('、'));
ok('块数合计等于总块数',
   topics.reduce((n, t) => n + t.size, 0) === r.chunks,
   `${topics.reduce((n, t) => n + t.size, 0)} vs ${r.chunks}`);

console.log('\n=== 4. 概览真的进了提示词（这一层唯一的用处）===');
// 问一个知识库必然没有的话题；回答里若点出库里覆盖的方向，说明概览生效了
const q = '请介绍一下量子纠缠在量子计算里的作用。';
const stream = await c.openStream('POST', '/api/chat/stream',
  { question: q, strategy: 'agent', model: 'local/qwen3:4b' }, { token: TOKEN });
let text = '';
await readSse(stream.response, (name, raw) => {
  let d; try { d = JSON.parse(raw); } catch { d = raw; }
  const dec = stream.decrypt(d);
  if (name === 'answer') text += dec.t || '';
});
console.log(`  回答：${text.slice(0, 260).replace(/\n/g, ' ')}`);
ok('声明了知识库没有这方面资料', /(知识库|资料)[^。\n]{0,12}(没有|未|无)/.test(text));
// 这是加主题树的直接目的：从「什么都没有」变成「没有 X，但有 Y」
const mentioned = topics.filter((t) => t.label && text.includes(t.label));
ok('回答点出了库里确实覆盖的方向', mentioned.length > 0,
   mentioned.length ? mentioned.map((t) => t.label).join('、') : '一个主题名都没提到');

console.log(`\n${'─'.repeat(52)}`);
console.log(`通过 ${pass} 项，失败 ${fail} 项`);
process.exit(fail ? 1 : 0);
