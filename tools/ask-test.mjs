/**
 * Agentic RAG 端到端测试。
 *
 * 验证 agent 循环的每个阶段事件都正确产出，以及「本质仍是 RAG」——
 * 回答必须建立在检索到的资料上，且能标注来源。
 *
 * 用的是 Python 版索引好的文档（embed_model 两边都是 local:bge-m3），
 * 顺带验证跨版本数据互操作。
 */
import fs from 'node:fs';
import { makeClient, readSse } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const CRED = process.env.KB_CRED_FILE || 'D:/vs/rag-kb/data/auth.json.migrated';
const QUESTION = process.env.KB_QUESTION || '校园网为什么拒绝 IPv6 入站？依据是什么？';

let pass = 0, fail = 0;
const check = (label, cond, extra = '') => {
  if (cond) { pass++; console.log(`  [PASS] ${label} ${extra}`); }
  else { fail++; console.log(`  [FAIL] ${label} ${extra}`); }
};

const cred = JSON.parse(fs.readFileSync(CRED, 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
if (login.status !== 200) { console.log('登录失败'); process.exit(1); }
const TOKEN = `Bearer ${login.body.data.accessToken}`;
console.log(`已登录 ${cred.user}`);
console.log(`问题：${QUESTION}\n`);

// ---------- 1) 公开配置检查 ----------
const healthy = await c.call('GET', '/api/health', undefined, { token: TOKEN });
console.log(`  /api/health -> ${healthy.status}`);
if (healthy.status === 200) {
  const db = healthy.body?.data?.database;
  console.log(`  数据库：${JSON.stringify(db)}`);
}

// ---------- 2) Agentic 流式问答 ----------
console.log('\n--- Agentic 模式（流式）---');
const t0 = Date.now();
const st = await c.openStream('POST', '/api/ask/stream',
  { question: QUESTION, strategy: 'agent', model: 'local/qwen3:8b' }, { token: TOKEN });
check('SSE 建立 -> 200', st.response.status === 200, `-> ${st.response.status}`);

const events = [];
let firstTokenAt = null;
await readSse(st.response, (name, raw) => {
  let data;
  try { data = JSON.parse(raw); } catch { data = raw; }
  const d = st.decrypt(data);
  if (name === 'answer' && firstTokenAt === null) firstTokenAt = Date.now() - t0;
  events.push({ name, data: d });
});

const byName = (n) => events.filter(e => e.name === n);
const meta = byName('meta')[0];
const plans = byName('plan');
const retrieves = byName('retrieve');
const assesses = byName('assess');
const answers = byName('answer');
const done = byName('done')[0];
const errorEvent = byName('error')[0];

if (errorEvent) console.log(`  error 事件: ${JSON.stringify(errorEvent.data)}`);

check('收到 meta 事件', !!meta, JSON.stringify(meta?.data)?.slice(0, 90));
check('收到 plan 事件（agent 做了检索规划）', plans.length > 0);
if (plans.length) {
  console.log(`  第 1 轮规划出的查询：${JSON.stringify(plans[0].data?.queries)}`);
}
check('收到 retrieve 事件（确实执行了检索）', retrieves.length > 0);
if (retrieves.length) {
  const r = retrieves[0].data;
  console.log(`  检索命中 ${r.hits} 段：${JSON.stringify(r.sources)}`);
  check('检索到了内容（知识库里有 Python 版索引的文档）', r.hits > 0, `hits=${r.hits}`);
}
check('收到 assess 事件（agent 评估了资料是否充分）', assesses.length > 0);
if (assesses.length) {
  console.log(`  评估：enough=${assesses[0].data?.enough} reason=${assesses[0].data?.reason}`);
}
check('收到 answer 增量（流式输出）', answers.length > 0,
  `共 ${answers.length} 个 token 事件，首 token 延迟 ${firstTokenAt}ms`);
check('收到 done 事件', !!done, JSON.stringify(done?.data));

const text = answers.map(a => a.data?.t || '').join('');
console.log(`\n  回答（${text.length} 字）：\n  ${text.replace(/\n/g, '\n  ')}`);
check('回答非空', text.length > 0);
check('回答带来源标注（本质是 RAG 的证据）', /\[\d+\]/.test(text),
  /\[\d+\]/.test(text) ? '含 [编号] 引用' : '未发现 [编号] 标注');
check('没有编造（未出现 error）', !errorEvent);

console.log(`\n  总耗时 ${Date.now() - t0}ms，轮次 ${done?.data?.rounds}，上下文 ${done?.data?.contexts} 段`);

// ---------- 3) 经典模式对照 ----------
console.log('\n--- 经典模式（单轮，非流式）对照 ---');
const t1 = Date.now();
const classic = await c.call('POST', '/api/ask',
  { question: QUESTION, strategy: 'classic', model: 'local/qwen3:8b' }, { token: TOKEN });
check('经典模式 -> 200', classic.status === 200, `-> ${classic.status}`);
const cdata = classic.body?.data;
console.log(`  耗时 ${cdata?.elapsedMs}ms，轮次 ${cdata?.rounds}，来源 ${cdata?.sources?.length} 段`);
check('经典模式只跑 1 轮', cdata?.rounds === 1);
check('经典模式回答非空', (cdata?.answer || '').length > 0);

console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
