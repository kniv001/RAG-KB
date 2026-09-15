/**
 * 验证「think:false + format 语法约束」的产出质量，以及耗时是否稳定。
 *
 * 上面的 agent-plan-probe 只测出它快。快不等于好 —— 语法约束是「逼」模型
 * 直接开局输出 `{`，它可能就此跳过推理，随手糊一个平庸查询。
 * 这份脚本把每个问题的实际产出打印出来，逐个目视检查。
 *
 * 同时覆盖三类问题：简单事实型、需要拆解型、以及带指代的追问型。
 */

const OLLAMA = process.env.OLLAMA || 'http://127.0.0.1:11434';
const MODEL = process.argv[2] || 'qwen3:4b';
const REPEAT = Number(process.argv[3] || 3);
/** baseline = 现状（带思考、无语法约束）；fast = think:false + format */
const MODE = process.argv[4] || 'fast';
const FAST = MODE === 'fast';

const PLAN_PROMPT = `你是知识库检索规划器。把用户的问题拆成 1~3 个用于检索的查询。

硬性要求：
1. 每个查询必须自包含，不能出现「它」「这个」「上面提到的」这类指代 —— 检索时没有对话上下文。
2. 查询用词要贴近资料里可能出现的说法，不要改写成抽象概念。
3. 问题简单就给 1 个查询，不要为了凑数硬拆。
4. 只输出 JSON，不要解释、不要加代码块标记。

输出格式：{"queries":["查询一","查询二"]}`;

const PLAN_SCHEMA = {
  type: 'object',
  properties: { queries: { type: 'array', items: { type: 'string' } } },
  required: ['queries'],
};

const CASES = [
  {
    tag: '简单事实型',
    q: '三层缓存分别省掉了什么开销？',
    expect: '应该 1 条就够，不该硬拆',
  },
  {
    tag: '需拆解型',
    q: '三层缓存分别省掉了什么开销？它们的键是怎么设计的，为什么改了输入就自动不命中？',
    expect: '应拆成 2~3 条：键设计、自动失效机制',
  },
  {
    tag: '带指代追问',
    q: '那它的键为什么要带上模型名？',
    history: [
      { role: 'user', content: '介绍一下三层缓存' },
      { role: 'assistant', content: '向量缓存、解析缓存、回答缓存，分别对应向量计算、文档解析、模型推理三种开销。' },
    ],
    expect: '必须消解「它」→ 回答缓存；查询里不能残留指代',
  },
  {
    tag: '易跑偏型',
    q: '为什么不能用更大的模型？',
    expect: '查询要贴近资料说法（显存/KV 缓存），不能只写「大模型」',
  },
];

function buildUser(c) {
  let s = '';
  if (c.history?.length) {
    s += '最近的对话：\n';
    for (const m of c.history) s += `${m.role === 'assistant' ? '助手' : '用户'}：${m.content}\n`;
    s += '\n';
  }
  s += `用户问题：${c.q}`;
  return s;
}

async function planOnce(user) {
  const body = {
    model: MODEL,
    messages: [
      { role: 'system', content: PLAN_PROMPT },
      { role: 'user', content: user },
    ],
    stream: false,
    options: { temperature: 0.2, num_ctx: 8192 },
  };
  if (FAST) {
    body.think = false;
    body.format = PLAN_SCHEMA;
  }
  const t0 = Date.now();
  const r = await fetch(`${OLLAMA}/api/chat`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const j = await r.json();
  const ms = Date.now() - t0;
  const content = (j.message?.content || '').trim();
  let queries = null;
  try {
    queries = JSON.parse(content).queries;
  } catch {
    // 基线下模型爱包 ```json 围栏，正式代码有 JsonExtract 兜底，这里照做
    const m = content.match(/```(?:json)?\s*([\s\S]*?)```/);
    const t = m ? m[1] : content;
    const a = t.indexOf('{');
    const b = t.lastIndexOf('}');
    if (a >= 0 && b > a) {
      try {
        queries = JSON.parse(t.slice(a, b + 1)).queries;
      } catch {
        /* 交由调用方判失败 */
      }
    }
  }
  return { ms, queries, outTokens: j.eval_count, raw: content };
}

// 预热
await planOnce('预热');
console.log(`模型 ${MODEL}，模式 ${MODE}，每例重复 ${REPEAT} 次\n`);

for (const c of CASES) {
  console.log('='.repeat(74));
  console.log(`【${c.tag}】${c.q}`);
  if (c.history) console.log(`  （前一轮：${c.history[0].content}）`);
  console.log(`  期望：${c.expect}`);
  console.log('-'.repeat(74));

  const times = [];
  const seen = new Map();
  for (let i = 0; i < REPEAT; i++) {
    const r = await planOnce(buildUser(c));
    times.push(r.ms);
    const key = JSON.stringify(r.queries);
    seen.set(key, (seen.get(key) || 0) + 1);
  }

  for (const [key, n] of seen) {
    const qs = JSON.parse(key);
    console.log(`  ${n > 1 ? `(${n}/${REPEAT} 次相同) ` : ''}${qs ? qs.length + ' 条' : '解析失败'}`);
    if (qs) {
      for (const q of qs) console.log(`     · ${q}`);
    } else {
      console.log(`     ← 原始：${key.slice(0, 120)}`);
    }
  }
  const avg = times.reduce((a, b) => a + b, 0) / times.length;
  console.log(
    `  耗时：${times.map((t) => t + 'ms').join(' / ')}  →  平均 ${avg.toFixed(0)}ms，` +
      `最快 ${Math.min(...times)}ms 最慢 ${Math.max(...times)}ms`
  );
  console.log();
}
