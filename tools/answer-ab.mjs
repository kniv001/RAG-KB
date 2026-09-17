/**
 * 资料段数的边际收益 A/B —— 出题、提问、存档（评分在 answer-ab-score.py）。
 *
 * 为什么必须问真问题而不是只看覆盖率：「多塞几段资料」在**上下文覆盖率**上必然更高
 * （多装几段当然多覆盖几个词），那是同义反复。真问题是**答案有没有变好**。
 *
 * 方法：每题开一个新会话（不带 convId，排除历史干扰），走完整 agent 链路，
 * 存下答案与检索统计。评分用机械判据 —— 答案里出现了多少「目标文档的独有词」，
 * 不引模型判分（README 里写明模型输出类断言失败率约三成）。
 *
 *   node tools/answer-ab.mjs A     # 跑一轮，存成 data/_answers-A.json
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const label = process.argv[2] || 'A';

const QUESTIONS = [
  'HTTP/2 的多路复用是怎么实现的？',
  'Redis 的 RDB 和 AOF 有什么区别？',
  '布隆过滤器为什么会有误判，误判率怎么算？',
  'HNSW 的 efSearch 参数控制什么？',
  'Pod 调度里的亲和性是怎么配置的？',
  'int8 量化和 4bit 量化的区别是什么？',
  'Docker 的 bridge 网络是怎么连通的？',
  'G1 垃圾回收器的 Region 是怎么划分的？',
  'Raft 的选举过程是怎样的？',
  '令牌桶和漏桶算法有什么区别？',
];

const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const out = [];
for (const q of QUESTIONS) {
  const t0 = Date.now();
  const st = await c.openStream('POST', '/api/chat/stream',
    { question: q, strategy: 'agent', model: 'local/qwen3:4b' }, { token: TOKEN });
  let text = '', sources = 0, retrieves = [], assesses = [];
  await readSse(st.response, (name, raw) => {
    let d; try { d = JSON.parse(raw); } catch { d = raw; }
    const dec = st.decrypt(d);
    if (name === 'answer') text += dec.t || '';
    if (name === 'retrieve') retrieves.push(dec.hits ?? 0);
    if (name === 'assess') assesses.push(dec.enough);
    if (name === 'done') sources = (dec.sources || []).length;
  });
  const ms = Date.now() - t0;
  out.push({ q, text, retrieves, assesses, sources, ms });
  console.log(`${String(ms).padStart(6)}ms  检索${JSON.stringify(retrieves)}  `
    + `评估${JSON.stringify(assesses)}  来源${sources}  ${q.slice(0, 22)}`);
}
fs.writeFileSync(`D:/vs/rag-kb-java/data/_answers-${label}.json`,
  JSON.stringify(out, null, 1), 'utf8');
console.log(`\n已存 data/_answers-${label}.json（${out.length} 题）`);
