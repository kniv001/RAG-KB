/**
 * 回答三段式验证。
 *
 * 知识库覆盖的问题 → 严格按资料答并标引用
 * 知识库没覆盖的   → 先声明没有，再给通用知识，且**明确标注那不是来自资料**
 * 与知识库无关的   → 直接答，不套知识库约束
 *
 * 关键是第二条：改之前它连模型都不调，直接返回硬编码的「资料中没有相关内容。」，
 * 用户永远只拿到那 9 个字。所以判据不能只看「有没有说没有」，
 * 还要看**有没有真给出内容**、以及**有没有标注来源边界**。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

let pass = 0, fail = 0;
const ok = (n, cond, extra = '') => {
  if (cond) { pass++; console.log(`  ✅ ${n}${extra ? ' — ' + extra : ''}`); }
  else { fail++; console.log(`  ❌ ${n}${extra ? ' — ' + extra : ''}`); }
};

/**
 * 先清掉回答缓存再测。
 *
 * 不这么做就得在问题里塞「（核对 xxxx）」之类的随机后缀来避开缓存 ——
 * 而模型会尽职地指出「问题里那句核对 xxx 知识库里没有」，
 * 被下面的 NO_KB 判据误伤（第一版就是这么误报的）。
 * 清缓存比污染问题干净。
 */
await c.call('DELETE', '/api/cache?which=answers', undefined, { token: TOKEN });
console.log('（已清空回答缓存）');

async function ask(question) {
  const st = await c.openStream('POST', '/api/chat/stream',
    { question, strategy: 'agent', model: 'local/qwen3:4b' }, { token: TOKEN });
  let text = '', thinking = 0, sources = 0, t0 = Date.now(), firstAt = null;
  await readSse(st.response, (name, raw) => {
    let d; try { d = JSON.parse(raw); } catch { d = raw; }
    const dec = st.decrypt(d);
    if (name === 'thinking') thinking++;
    if (name === 'answer') { if (!firstAt) firstAt = Date.now() - t0; text += dec.t || ''; }
    if (name === 'done') sources = dec.sources?.length ?? 0;
  });
  return { text: text.trim(), thinking, sources, ms: Date.now() - t0, firstAt };
}

const NO_KB = /(知识库|资料)[^。\n]{0,12}(没有|未|无|不含|缺少)|没有(检索|找到)[^。\n]{0,8}(资料|内容)/;
const MARKED = /(通用知识|一般知识|常识|并非来自|不是来自|未引用|非.*知识库|不属于.*知识库|基于.*(自身|我).*知识)/;

console.log('\n=== 一、知识库能覆盖的问题 ===');
{
  const q = `技术文档模版里，性能目标那一节要求考虑哪些方面？`;
  const r = await ask(q);
  console.log(`  回答（${r.ms}ms，${r.sources} 个来源）：\n    ${r.text.slice(0, 200).replace(/\n/g, '\n    ')}`);
  ok('检索到了资料', r.sources > 0, `${r.sources} 个来源`);
  ok('没有说「知识库没有」', !NO_KB.test(r.text));
  ok('标注了来源编号', /\[\d+\]/.test(r.text));
  ok('正文提到了性能目标相关内容', /(CPU|内存|响应时间|性能)/.test(r.text));
}

console.log('\n=== 二、知识库没有覆盖的问题（本轮改动的重点）===');
{
  const q = `什么是三层缓存？它的淘汰策略一般怎么设计？`;
  const r = await ask(q);
  console.log(`  回答（${r.ms}ms）：\n    ${r.text.slice(0, 420).replace(/\n/g, '\n    ')}`);
  ok('先声明了知识库没有这方面的资料', NO_KB.test(r.text));
  // 这条是核心：不能只有「没有」两个字，必须真给出内容
  ok('真的给出了内容（不再是那 9 个字）', r.text.length > 80, `${r.text.length} 字`);
  ok('标注了这部分是通用知识、不是来自资料', MARKED.test(r.text),
     MARKED.test(r.text) ? '' : '没找到来源边界标注');
}

console.log('\n=== 三、与知识库无关的问题 ===');
{
  const q = `你是谁？用一句话说。`;
  const r = await ask(q);
  console.log(`  回答（${r.ms}ms）：\n    ${r.text.slice(0, 200).replace(/\n/g, '\n    ')}`);
  ok('直接回答了，没有套知识库约束', !NO_KB.test(r.text) && r.text.length > 5);
}

console.log(`\n${'─'.repeat(52)}`);
console.log(`通过 ${pass} 项，失败 ${fail} 项`);
process.exit(fail ? 1 : 0);
