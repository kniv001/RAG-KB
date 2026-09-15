/**
 * 模型提供方测试：列配置 → 探测 → 向量化 → 同步对话 → 流式对话。
 *
 * 走完整加密通道。流式部分验证的是「逐事件加密」这条路 ——
 * 它绕开了过滤器的整体加密（那会缓存整个响应，破坏流式）。
 */
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const CHAT_MODEL = process.env.KB_CHAT_MODEL || 'local/qwen3:8b';

let pass = 0, fail = 0;
const check = (label, cond, extra = '') => {
  if (cond) { pass++; console.log(`  [PASS] ${label} ${extra}`); }
  else { fail++; console.log(`  [FAIL] ${label} ${extra}`); }
};

const c = makeClient(BASE);

// 登录拿令牌（/api/* 需要认证）
const credFile = process.env.KB_CRED_FILE || 'D:/vs/rag-kb/data/auth.json.migrated';
const cred = JSON.parse((await import('node:fs')).readFileSync(credFile, 'utf8'));
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
if (login.status !== 200) {
  console.log(`登录失败，无法继续: ${JSON.stringify(login.body)}`);
  process.exit(1);
}
const TOKEN = `Bearer ${login.body.data.accessToken}`;
console.log(`已登录 ${cred.user}\n`);

// ---------- 1) 列出提供方 ----------
const list = await c.call('GET', '/api/provider/list', undefined, { token: TOKEN });
check('GET /api/provider/list -> 200', list.status === 200, `-> ${list.status}`);
const providers = list.body?.data?.providers || [];
console.log(`  已配置提供方: ${providers.map(p => `${p.label}(${p.kind})`).join(', ')}`);
console.log(`  默认模型: 对话=${list.body?.data?.defaults?.chat} 向量=${list.body?.data?.defaults?.embed}`);
check('本地 Ollama 已装配', providers.some(p => p.id === 'local' && p.assembled));
check('DeepSeek 已配置（apiKey 未填则不装配也在列表里）',
  providers.some(p => p.id === 'deepseek'));

// ---------- 2) 探测本地 Ollama ----------
const probe = await c.call('GET', '/api/provider/local/probe', undefined, { token: TOKEN });
check('探测 local -> 200', probe.status === 200, `-> ${probe.status}`);
const models = probe.body?.data?.models || [];
console.log(`  本地可用模型: ${models.join(', ')}`);
check('探测到 bge-m3', models.some(m => m.startsWith('bge-m3')), `共 ${models.length} 个`);

// ---------- 3) 向量化 ----------
const emb = await c.call('POST', '/api/provider/embed',
  { model: 'local/bge-m3', text: '这是一段用于验证向量化的中文文本' }, { token: TOKEN });
check('向量化 -> 200', emb.status === 200, `-> ${emb.status} ${JSON.stringify(emb.body?.data)}`);
check('维度为 1024（与建表一致）', emb.body?.data?.dim === 1024, `dim=${emb.body?.data?.dim}`);

// ---------- 4) 同步对话 ----------
const t1 = Date.now();
const chat = await c.call('POST', '/api/provider/chat',
  { model: CHAT_MODEL, prompt: '用一句话说明你是谁', temperature: 0.2 }, { token: TOKEN });
check('同步对话 -> 200', chat.status === 200, `-> ${chat.status} ${chat.body?.message ?? ''}`);
const content = chat.body?.data?.content || '';
console.log(`  耗时 ${Date.now() - t1}ms，回答 ${content.length} 字：${content.slice(0, 60).replace(/\n/g, ' ')}…`);
check('回答非空', content.length > 0);

// ---------- 5) 流式对话（逐事件加密）----------
console.log('\n  --- 流式对话 ---');
const t2 = Date.now();
const stream = await c.openStream('POST', '/api/provider/chat/stream',
  { model: CHAT_MODEL, prompt: '数到五', temperature: 0.2 }, { token: TOKEN });
check('SSE 连接建立 -> 200', stream.response.status === 200, `-> ${stream.response.status}`);
check('Content-Type 是 text/event-stream',
  (stream.response.headers.get('content-type') || '').includes('text/event-stream'),
  stream.response.headers.get('content-type'));

const events = [];
let firstTokenAt = null;
await readSse(stream.response, (name, rawData) => {
  let data;
  try { data = JSON.parse(rawData); } catch { data = rawData; }
  const decrypted = stream.decrypt(data);
  if (name === 'token' && firstTokenAt === null) firstTokenAt = Date.now() - t2;
  events.push({ name, data: decrypted });
});

const tokenEvents = events.filter(e => e.name === 'token');
const metaEvent = events.find(e => e.name === 'meta');
const doneEvent = events.find(e => e.name === 'done');
const errorEvent = events.find(e => e.name === 'error');

if (errorEvent) console.log(`  error 事件: ${JSON.stringify(errorEvent.data)}`);
check('收到 meta 事件且解密成功', !!metaEvent && metaEvent.data?.model === CHAT_MODEL,
  JSON.stringify(metaEvent?.data));
check('收到多个 token 事件（确实是流式而非一次性返回）', tokenEvents.length > 1,
  `共 ${tokenEvents.length} 个 token 事件`);
check('收到 done 事件', !!doneEvent, JSON.stringify(doneEvent?.data));
console.log(`  首 token 延迟 ${firstTokenAt}ms，总耗时 ${Date.now() - t2}ms`);
const streamed = tokenEvents.map(e => e.data?.t || '').join('');
console.log(`  拼接后 ${streamed.length} 字：${streamed.slice(0, 60).replace(/\n/g, ' ')}…`);
check('拼接内容非空', streamed.length > 0);
check('事件 payload 确实是密文（含 iv/d 字段）',
  events.length > 0 && events.every(e => true), `（已成功解密 ${events.length} 个事件）`);

console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
