/**
 * 拆解 plan / assess 阶段的耗时构成，并测三个提速杠杆。
 *
 * 背景：agent 模式里 plan 只要吐一个 JSON，却慢得离谱。怀疑是 qwen3 的
 * 「思考模式」默认开启 —— 它在产出 JSON 前先写了几百 token 的推理。
 *
 * 直接打 Ollama 的 /api/chat，绕开整个 Java 应用，测的是同一份提示词。
 *
 * 四个变体：
 *   A 现状        —— 不传 think、不传 format（当前 Java 代码就是这样）
 *   B /no_think   —— Qwen3 的软开关，写在提示词里
 *   C think:false —— 硬关思考
 *   D C + format  —— 硬关思考 + JSON Schema 语法约束
 *   E B + format  —— 软开关 + 语法约束
 *
 * 看点有两处：耗时降到多少，以及 content 还是不是合法 JSON。
 */

const OLLAMA = process.env.OLLAMA || 'http://127.0.0.1:11434';
const MODELS = (process.argv[2] || 'qwen3:4b').split(',');

// 与 AgenticRagService.PLAN_PROMPT 逐字一致
const PLAN_PROMPT = `你是知识库检索规划器。把用户的问题拆成 1~3 个用于检索的查询。

硬性要求：
1. 每个查询必须自包含，不能出现「它」「这个」「上面提到的」这类指代 —— 检索时没有对话上下文。
2. 查询用词要贴近资料里可能出现的说法，不要改写成抽象概念。
3. 问题简单就给 1 个查询，不要为了凑数硬拆。
4. 只输出 JSON，不要解释、不要加代码块标记。

输出格式：{"queries":["查询一","查询二"]}`;

// 与 AgenticRagService.ASSESS_PROMPT 逐字一致
const ASSESS_PROMPT = `你是资料充分性评估器。判断给出的资料能否支撑回答用户的问题。

判断标准：
- 只要资料里有能用来回答的**具体内容**（事实、现象、步骤、数据、结论、甚至间接线索），就判「够」。
- 只有当资料完全没涉及问题主题，或通篇只是泛泛提及而无任何可用内容时，才判「不够」。

明确禁止的判「不够」理由：
- 「资料没有给出官方出处 / 权威文件名称」
- 「资料没有明确写明原因 / 政策依据」
- 「资料不够全面 / 不够详细」
这些都不是「不够」。用户要的是能回答问题，不是要一份官方文件。

只输出 JSON，不要解释：
{"enough":true,"reason":"一句话理由","missing":"若不够，说明还缺什么"}`;

const PLAN_SCHEMA = {
  type: 'object',
  properties: { queries: { type: 'array', items: { type: 'string' } } },
  required: ['queries'],
};

const ASSESS_SCHEMA = {
  type: 'object',
  properties: {
    enough: { type: 'boolean' },
    reason: { type: 'string' },
    missing: { type: 'string' },
  },
  required: ['enough'],
};

// 用一个真实形态的问题：稍复杂，需要拆查询
const QUESTION = '三层缓存分别省掉了什么开销？它们的键是怎么设计的，为什么改了输入就自动不命中？';

const ASSESS_USER = `用户问题：${QUESTION}

已有资料：
[1] rag-kb 设计笔记 第 3 块：向量缓存以 sha256(文本+模型) 为键，命中则跳过最贵的向量计算；解析缓存以 sha256(文件内容) 为键，跳过大文档解析；回答缓存以 sha256(问题+上下文哈希+历史哈希+provider+model+温度) 为键。
[2] rag-kb 设计笔记 第 4 块：把所有影响结果的参数都塞进键里，于是「改了输入就自动不命中」，永远不存在返回陈旧结果的窗口，清空缓存也就只是释放空间。
[3] rag-kb 运维记录 第 1 块：缓存故障一律降级为未命中而不是抛错，缓存不该让主流程挂掉。`;

async function call(model, system, user, { think, format, numCtx = 8192, numPredict } = {}) {
  const body = {
    model,
    messages: [
      { role: 'system', content: system },
      { role: 'user', content: user },
    ],
    stream: false,
    options: { temperature: 0.2, num_ctx: numCtx },
  };
  if (numPredict > 0) body.options.num_predict = numPredict;
  if (think !== undefined) body.think = think;
  if (format !== undefined) body.format = format;

  const t0 = Date.now();
  const r = await fetch(`${OLLAMA}/api/chat`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const j = await r.json();
  const wall = Date.now() - t0;

  const thinking = j.message?.thinking || '';
  const content = (j.message?.content || '').trim();
  return {
    wall,
    thinkingLen: thinking.length,
    contentLen: content.length,
    content,
    promptTokens: j.prompt_eval_count || 0,
    outTokens: j.eval_count || 0,
    loadMs: Math.round((j.load_duration || 0) / 1e6),
    evalMs: Math.round((j.eval_duration || 0) / 1e6),
  };
}

/** 从可能夹带 markdown  fence 或前后废话的文本里抽出第一个 JSON 对象 */
function extractJson(s) {
  const fenced = s.match(/```(?:json)?\s*([\s\S]*?)```/);
  const t = fenced ? fenced[1] : s;
  const start = t.indexOf('{');
  const end = t.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  try {
    return JSON.parse(t.slice(start, end + 1));
  } catch {
    return null;
  }
}

function judgePlan(content) {
  const o = extractJson(content);
  if (!o) return { ok: false, why: '不是 JSON' };
  if (!Array.isArray(o.queries) || o.queries.length === 0) return { ok: false, why: '缺 queries' };
  return { ok: true, why: `${o.queries.length} 条查询` };
}

function judgeAssess(content) {
  const o = extractJson(content);
  if (!o) return { ok: false, why: '不是 JSON' };
  if (typeof o.enough !== 'boolean') return { ok: false, why: '缺 enough' };
  return { ok: true, why: `enough=${o.enough}` };
}

const VARIANTS = [
  { name: 'A 现状', think: undefined, format: undefined, soft: false },
  { name: 'B /no_think', think: undefined, format: undefined, soft: true },
  { name: 'C think:false', think: false, format: undefined, soft: false },
  { name: 'D 关思考+format', think: false, format: 'SCHEMA', soft: false },
  { name: 'E 软开关+format', think: undefined, format: 'SCHEMA', soft: true },
];

async function runStage(label, model, system, user, schema, judge) {
  console.log(`\n${'='.repeat(72)}`);
  console.log(`${label}   [${model}]`);
  console.log('='.repeat(72));
  console.log('变体             墙钟     思考tokens  输出tokens  产出        合法性');
  console.log('-'.repeat(72));

  for (const v of VARIANTS) {
    const sys = v.soft ? `${system}\n/no_think` : system;
    const fmt = v.format === 'SCHEMA' ? schema : v.format;
    try {
      const r = await call(model, sys, user, { think: v.think, format: fmt });
      const j = judge(r.content);
      console.log(
        `${v.name.padEnd(16)} ${String(r.wall + 'ms').padStart(7)} ` +
          `${String(r.thinkingLen).padStart(10)}  ${String(r.outTokens).padStart(10)}  ` +
          `${j.why.slice(0, 10).padEnd(10)}  ${j.ok ? '✅' : '❌'}`
      );
      if (!j.ok) {
        console.log(`      ↳ 原始输出片段: ${JSON.stringify(r.content.slice(0, 110))}`);
      }
    } catch (e) {
      console.log(`${v.name.padEnd(16)} 失败: ${e.message}`);
    }
  }
}

const STAMP = () => `生成于 ${new Date().toISOString().slice(0, 16).replace('T', ' ')}`;

for (const model of MODELS) {
  console.log(`\n\n########## 模型 ${model}   ${STAMP()} ##########`);
  // 先热一次，把模型装载耗时从对比里排除
  process.stdout.write('预热中…');
  await call(model, '你是助手。', '回复 ok', {});
  console.log(' 完成');

  await runStage('阶段一：plan（规划检索查询）', model, PLAN_PROMPT, `用户问题：${QUESTION}`, PLAN_SCHEMA, judgePlan);
  await runStage('阶段二：assess（评估资料是否充分）', model, ASSESS_PROMPT, ASSESS_USER, ASSESS_SCHEMA, judgeAssess);
}

console.log('\n\n说明：墙钟含模型装载（若发生切换）。思考 tokens 为 0 表示该变体没有触发思考。');
