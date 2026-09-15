/**
 * 历史索引验证：多轮对话后，超出最近窗口的旧轮次还能不能被召回。
 *
 * 思路：先问一个带明确代号的问题（代号只存在于对话历史里，不在知识库中），
 * 中间灌足够多的无关轮次把第 1 轮挤出最近窗口（HISTORY_LIMIT=16 条 = 8 轮），
 * 最后问「我们最开始聊的那个」——看模型还能不能指出内容。
 *
 * 若第 1 轮被丢弃，模型只能答「不知道」；若历史索引生效，它能说出主题。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const FILLER = [
  '三层缓存里哪一层最贵？',
  '解析缓存是干什么用的？',
  '异步索引为什么要立即返回 202？',
  'jsonb 列为什么要显式给 TypeHandler？',
  'pgvector 的向量列怎么写入？',
  '缓存故障时为什么降级为未命中？',
  '为什么要显式传 num_ctx？',
  'RRF 为什么对分数尺度不敏感？',
  '切分参数为什么要和 Python 版保持一致？',
  '双令牌里 refresh token 为什么放 Redis？',
];

async function ask(question, convId) {
  const t0 = Date.now();
  const body = { question, strategy: 'agent', model: 'local/qwen3:4b' };
  if (convId) body.convId = convId;
  const st = await c.openStream('POST', '/api/chat/stream', body, { token: TOKEN });

  let text = '', id = convId, thinking = 0, answer = 0;
  await readSse(st.response, (name, raw) => {
    let d; try { d = JSON.parse(raw); } catch { d = raw; }
    const dec = st.decrypt(d);
    if (name === 'meta' && dec.convId) id = dec.convId;
    if (name === 'answer') { text += dec.t || ''; answer++; }
    if (name === 'thinking') thinking++;
  });
  return { convId: id, text: text.trim(), ms: Date.now() - t0, thinking, answer };
}

// 开一个名字很独特的话题，便于在最后验证
const OPENER = '请记住一个项目代号：本次测试用的内部代号叫「苍鹭计划」。它和三层缓存无关，只是我要你记住的一个标记。';
const PROBE = '我们这次对话最开始说的那个项目代号是什么？';

console.log('=== 第 1 轮：埋入代号 ===');
let r = await ask(OPENER);
const convId = r.convId;
console.log(`convId=${convId}\n回答：${r.text.slice(0, 120)}`);

console.log(`\n=== 灌 ${FILLER.length} 轮无关问题，把第 1 轮挤出最近窗口 ===`);
console.log('（HISTORY_LIMIT = 16 条 = 8 轮；灌完共 ' + (1 + FILLER.length) + ' 轮）');
for (let i = 0; i < FILLER.length; i++) {
  r = await ask(FILLER[i], convId);
  process.stdout.write(`  ${String(i + 2).padStart(2)}. ${FILLER[i].slice(0, 22)}… ${r.ms}ms\n`);
}

console.log('\n=== 最后一轮：问最开始的代号 ===');
r = await ask(PROBE, convId);
console.log(`回答（${r.ms}ms，thinking ${r.thinking} 事件 / answer ${r.answer} 事件）：`);
console.log(r.text.slice(0, 500));

const hit = /苍鹭/.test(r.text);
console.log(`\n${hit ? '✅ 召回了第 1 轮 —— 历史索引生效' : '❌ 没有召回 —— 第 1 轮已丢失或检索失败'}`);
console.log(`\n会话 id  ${convId}（可到库里查 messages.embedding 是否已写满）`);
