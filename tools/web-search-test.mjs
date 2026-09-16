/**
 * 联网搜索与入库的验证。
 *
 * 分三步，每步都能独立看出问题出在哪：
 *   ① 搜索 —— 能不能拿到结果（解析 Bing 的 HTML，对方改版就会坏）
 *   ② 抓取 —— 正文提取干不干净（去导航/脚本之后还有多少有效内容）
 *   ③ 入库 —— 落盘、建索引、出处能不能一路带到检索结果里
 *
 * 断言刻意不依赖「搜出来的具体是什么」—— 搜索结果会变，网络会抖，
 * 那种断言过两天就红，然后被当成噪音忽略掉。
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

let pass = 0, fail = 0;
const ok = (n, cond, e = '') => {
  if (cond) { pass++; console.log(`  ✅ ${n}${e ? ' — ' + e : ''}`); }
  else { fail++; console.log(`  ❌ ${n}${e ? ' — ' + e : ''}`); }
};

const call = (m, p, b) => c.call(m, p, b, { token: TOKEN });

console.log('\n=== 0. 开关状态 ===');
const st = await call('GET', '/api/web/status');
ok('接口可用', st.status === 200 && st.body?.code === 0);
ok('联网已开启', st.body?.data?.enabled === true,
   st.body?.data?.enabled ? st.body.data.searchEndpoint : '未开启（用 KB_WEB_ENABLED=true 启动）');
if (!st.body?.data?.enabled) {
  console.log('\n联网没开，后续跳过。启动时加 KB_WEB_ENABLED=true');
  process.exit(1);
}

console.log('\n=== 1. 搜索 ===');
const q = '三层缓存架构';
const s = await call('POST', '/api/web/search', { query: q, count: 5 });
ok('搜索返回成功', s.status === 200 && s.body?.code === 0, s.body?.message || '');
const hits = s.body?.data?.results || [];
ok('拿到了结果', hits.length > 0, `${hits.length} 条`);
if (hits.length) {
  ok('  每条都有标题', hits.every((h) => h.title && h.title.length > 0));
  ok('  每条都有 http(s) 网址', hits.every((h) => /^https?:\/\//.test(h.url)));
  ok('  网址不重复', new Set(hits.map((h) => h.url)).size === hits.length);
  console.log('  前三条：');
  hits.slice(0, 3).forEach((h) => console.log(`    · ${h.title.slice(0, 40)}  ${h.url.slice(0, 56)}`));
}

console.log('\n=== 2. SSRF 防护 ===');
for (const bad of ['http://127.0.0.1:6379/', 'http://localhost:8080/', 'http://10.0.0.1/',
                   'file:///C:/Windows/win.ini', 'http://192.168.1.1/']) {
  const r = await call('POST', '/api/web/ingest', { urls: [bad] });
  const res = r.body?.data?.results?.[0];
  const blocked = !!res?.error;
  ok(`拒绝 ${bad}`, blocked, blocked ? res.error.slice(0, 46) : '❗没被拦住');
}

console.log('\n=== 3. 抓取并入库 ===');
// 挑一条真实结果入库，验证整条链路
const target = hits.find((h) => /^https?:\/\//.test(h.url));
if (!target) {
  console.log('  没有可用网址，跳过');
} else {
  console.log(`  目标：${target.url}`);
  const ing = await call('POST', '/api/web/ingest', { urls: [target.url] });
  ok('入库接口返回成功', ing.status === 200 && ing.body?.code === 0, ing.body?.message || '');
  const one = ing.body?.data?.results?.[0];
  ok('抓取成功', one && !one.error, one?.error || `${one?.chars} 字`);
  ok('  正文长度合理（>200 字）', (one?.chars ?? 0) > 200, `${one?.chars} 字`);
  ok('  返回了 docId 与索引任务', !!one?.docId && !!one?.taskId, `${one?.docId} / ${one?.taskId}`);

  if (one?.taskId) {
    let task = null;
    for (let i = 0; i < 90; i++) {
      await new Promise((r) => setTimeout(r, 800));
      const t = await call('GET', `/api/docs/tasks/${one.taskId}`);
      task = t.body?.data;
      if (task && task.status !== 'running') break;
    }
    ok('索引完成', task?.status === 'done', `${task?.status} ${task?.message || ''}`);

    // 出处要能带出来 —— 这是「联网之后回答仍可追溯」的关键
    const docs = await call('GET', '/api/docs');
    const row = (docs.body?.data?.docs || []).find((d) => d.id === one.docId);
    ok('  文档已入库', !!row, row?.name);
  }
}

console.log(`\n${'─'.repeat(52)}`);
console.log(`通过 ${pass} 项，失败 ${fail} 项`);
process.exit(fail ? 1 : 0);
