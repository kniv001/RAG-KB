/**
 * 向指定会话问一句，打印完整事件序列。排查用。
 *
 *   node tools/ask-once.mjs "问题" [convId] [strategy]
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const [question, convId, strategy = 'agent'] = process.argv.slice(2);
if (!question) {
  console.error('用法: node tools/ask-once.mjs "问题" [convId] [strategy]');
  process.exit(1);
}

const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const body = { question, strategy, model: 'local/qwen3:4b' };
if (convId) body.convId = convId;

const t0 = Date.now();
const st = await c.openStream('POST', '/api/chat/stream', body, { token: TOKEN });

let text = '';
const counts = {};
await readSse(st.response, (name, raw) => {
  let d; try { d = JSON.parse(raw); } catch { d = raw; }
  const dec = st.decrypt(d);
  counts[name] = (counts[name] || 0) + 1;
  if (name === 'answer') text += dec.t || '';
  if (name === 'meta') console.log(`convId=${dec.convId}  historyTurns=${dec.historyTurns}`);
  if (name === 'plan') console.log(`plan   ${(dec.queries || []).join(' | ')}`);
  if (name === 'assess') console.log(`assess enough=${dec.enough}  ${String(dec.reason || '').slice(0, 70)}`);
});

console.log(`\n事件统计 ${JSON.stringify(counts)}  耗时 ${Date.now() - t0}ms`);
console.log(`\n回答：\n${text.trim()}`);
