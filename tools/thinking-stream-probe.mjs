/**
 * 验证思考流：thinking 事件有没有真的推出来，以及首个 thinking 帧比首个正文帧早多少。
 *
 * 期望形态：thinking 事件先到（0.2~2.5 秒），answer 事件后到（12~46 秒）。
 * 若 thinking 事件数为 0，说明 stream-thinking 没生效或提供方没接。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const qs = process.argv.slice(2);
const questions = qs.length ? qs : ['三层缓存分别省掉了什么开销？为什么改了输入就自动不命中？'];

for (const question of questions) {
  console.log('\n' + '='.repeat(74));
  console.log(`问题：${question}`);
  console.log('='.repeat(74));

  const t0 = Date.now();
  const st = await c.openStream('POST', '/api/ask/stream',
    { question, strategy: 'agent', model: 'local/qwen3:4b' }, { token: TOKEN });

  let firstThink = null, firstAnswer = null, thinkEvents = 0, answerEvents = 0;
  let thinkChars = 0, answerChars = 0;
  let answerText = '';

  await readSse(st.response, (name, raw) => {
    let d; try { d = JSON.parse(raw); } catch { d = raw; }
    const dec = st.decrypt(d);
    const at = Date.now() - t0;
    if (name === 'thinking') {
      thinkEvents++;
      thinkChars += (dec.t || '').length;
      if (firstThink === null) firstThink = at;
    } else if (name === 'answer') {
      answerEvents++;
      answerChars += (dec.t || '').length;
      answerText += dec.t || '';
      if (firstAnswer === null) firstAnswer = at;
    }
  });
  const total = Date.now() - t0;

  console.log(`\n首个 thinking 帧  ${firstThink === null ? '  —（没有 thinking 事件）' : firstThink + ' ms'}`);
  console.log(`首个 answer  帧  ${firstAnswer === null ? '  —' : firstAnswer + ' ms'}`);
  console.log(`总耗时           ${total} ms`);
  console.log(`事件数           thinking ${thinkEvents} 个 / ${thinkChars} 字    answer ${answerEvents} 个 / ${answerChars} 字`);
  console.log(`\n正文（用于确认质量没坏）：\n${answerText.trim().slice(0, 400) || '（空）'}`);

  const cached = thinkEvents === 0 && answerEvents === 1;
  if (cached) {
    console.log('\n⚠️  看起来是回答缓存命中（单个 answer 事件、总耗时 <1s）——');
    console.log('    命中时不调模型，本来就没有 thinking。换个没问过的问题再测。');
  } else if (thinkEvents === 0) {
    console.log('\n❌ 没有收到 thinking 事件 —— 检查 ragkb.rag.agent.stream-thinking 与该提供方是否实现');
  } else if (firstAnswer !== null && firstThink !== null) {
    const saved = firstAnswer - firstThink;
    console.log(`\n✅ 用户原本要黑屏等 ${firstAnswer}ms，现在 ${firstThink}ms 就有内容可看（提前 ${saved}ms）`);
    console.log(`   合并比 ${(thinkChars / thinkEvents).toFixed(0)} 字/事件 —— 若接近 1 说明没走合并，SSE 与加密开销会偏高`);
  }
}
