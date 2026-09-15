/**
 * 前端契约测试。
 *
 * 浏览器里跑不了自动化，所以把前端依赖的每一处后端契约在这里验一遍 ——
 * 它们在浏览器里失败时的表现都是白屏或静默不显示，最难排查。
 *
 * 用 kb-client 走的是与浏览器 WebCrypto 完全一致的协议（RSA-OAEP SHA-256 + AES-GCM）。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);

let pass = 0, fail = 0;
const ok = (name, cond, extra = '') => {
  if (cond) { pass++; console.log(`  ✅ ${name}${extra ? ' — ' + extra : ''}`); }
  else { fail++; console.log(`  ❌ ${name}${extra ? ' — ' + extra : ''}`); }
};

console.log('\n=== 1. 匿名可取的部分（前端启动前就要能拿到）===');
{
  const r = await fetch(`${BASE}/index.html`);
  ok('GET /index.html 匿名可取', r.ok, `HTTP ${r.status}`);
  ok('  内容类型正确', r.headers.get('content-type')?.includes('text/html'));
  const html = await r.text();
  ok('  引用了 app.js 与 app.css', html.includes('/assets/app.js') && html.includes('/assets/app.css'));

  const k = await fetch(`${BASE}/api/crypto/public-key`).then((x) => x.json());
  ok('GET /api/crypto/public-key 匿名可取', !!k?.data?.publicKey);
  ok('  公钥是 base64 DER(SPKI)', typeof k.data.publicKey === 'string' && k.data.publicKey.length > 100);
}

console.log('\n=== 2. 登录与令牌 ===');
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
ok('加密登录成功', login.status === 200 && login.body?.code === 0, `HTTP ${login.status}`);
ok('  响应已解密', login.decrypted);
const TOKEN = `Bearer ${login.body.data.accessToken}`;
ok('  返回 accessToken', typeof login.body.data.accessToken === 'string');
ok('  返回用户信息', !!login.body.data.user?.username, login.body.data.user?.username);
ok('  refresh Cookie 已下发', !!c.refreshCookie(), 'kb_refresh');

console.log('\n=== 3. 鉴权与过期语义 ===');
{
  const r = await c.call('GET', '/api/auth/me', undefined, { token: TOKEN });
  ok('GET /api/auth/me 带令牌可用', r.status === 200 && r.body?.code === 0);

  const bad = await c.call('GET', '/api/auth/me', undefined, { token: 'Bearer 无效令牌' });
  ok('无效令牌返回 401', bad.status === 401, `HTTP ${bad.status}`);
  ok('  401 体里带 expired 字段（前端据此决定刷新还是跳登录）',
     typeof bad.body?.expired === 'boolean', String(bad.body?.expired));
}

console.log('\n=== 4. 会话列表 ===');
{
  const r = await c.call('GET', '/api/chat/conversations?limit=100', undefined, { token: TOKEN });
  ok('会话列表可取', r.status === 200 && r.body?.code === 0);
  ok('  结构含 conversations 数组', Array.isArray(r.body.data.conversations));
  const first = r.body.data.conversations[0];
  if (first) {
    ok('  每项含 id/title/updatedAt',
       'id' in first && 'title' in first && 'updatedAt' in first, first.title);
  } else {
    console.log('  ⚠️  还没有会话，跳过字段检查');
  }
}

console.log('\n=== 5. 流式事件（前端渲染全靠它）===');
{
  const st = await c.openStream('POST', '/api/chat/stream',
    { question: '三层缓存分别省掉什么开销？', strategy: 'agent' }, { token: TOKEN });

  ok('响应是 SSE', st.response.headers.get('content-type')?.includes('text/event-stream'),
     st.response.headers.get('content-type'));

  const seen = new Set();
  let meta = null, done = null, answer = '';
  await readSse(st.response, (name, raw) => {
    let d; try { d = JSON.parse(raw); } catch { d = raw; }
    const dec = st.decrypt(d);
    seen.add(name);
    if (name === 'meta') meta = dec;
    if (name === 'done') done = dec;
    if (name === 'answer') answer += dec.t || '';
  });

  ok('收到 meta 事件', !!meta);
  ok('  meta 带 convId（新建会话时前端靠它续接）', !!meta?.convId, meta?.convId);
  ok('收到 answer 事件', answer.length > 0, `${answer.length} 字`);
  ok('收到 done 事件', !!done, seen.has('done') ? '' : `实际事件：${[...seen].join(', ')}`);
  ok('  done 带 sources（回答下方的引用出处）', Array.isArray(done?.sources),
     `sources=${done?.sources?.length ?? 'undefined'}`);
  if (done?.sources?.length) {
    const s = done.sources[0];
    ok('  source 含 docName/seq/preview',
       'docName' in s && 'seq' in s && 'preview' in s, `${s.docName} #${s.seq}`);
  }
  ok('  done 带 rounds/queries（设置里的过程面板用）',
     typeof done?.rounds === 'number' && Array.isArray(done?.queries));
  console.log(`  事件类型：${[...seen].join(', ')}`);
}

console.log('\n=== 6. 设置面板依赖的接口 ===');
{
  const stats = await c.call('GET', '/api/cache/stats', undefined, { token: TOKEN });
  ok('GET /api/cache/stats', stats.status === 200 && stats.body?.code === 0,
     Object.keys(stats.body?.data || {}).join('、'));

  const docs = await c.call('GET', '/api/docs', undefined, { token: TOKEN });
  ok('GET /api/docs', docs.status === 200 && docs.body?.code === 0,
     `${docs.body?.data?.count} 篇，stale ${docs.body?.data?.stale?.length ?? 0} 篇`);

  const prov = await c.call('GET', '/api/provider/list', undefined, { token: TOKEN });
  ok('GET /api/provider/list', prov.status === 200 && prov.body?.code === 0);
  ok('  含 providers 与 defaults',
     Array.isArray(prov.body.data.providers) && !!prov.body.data.defaults,
     prov.body.data.defaults?.chat);

  const health = await c.call('GET', '/api/health', undefined, { token: TOKEN });
  ok('GET /api/health', health.status === 200 && health.body?.code === 0);
  ok('  database.ok 为真', health.body.data.database?.ok === true,
     health.body.data.database?.product);
}

console.log(`\n${'─'.repeat(50)}`);
console.log(`通过 ${pass} 项，失败 ${fail} 项`);
process.exit(fail ? 1 : 0);
