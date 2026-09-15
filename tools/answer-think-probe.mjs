/**
 * 回答路径的「思考」问题：首 token 要等 7~11 秒，而这期间模型在 thinking 通道里
 * 写推理 —— 当前 provider 只读 content，于是这段被整个丢掉，用户对着黑屏干等。
 *
 * 三种对策，逐一实测首 token 延迟与产出质量：
 *   A 现状       —— 思考照开，thinking 丢掉（黑屏 7~11 秒）
 *   B 关思考     —— think:false，快，但推理可能漏进正文
 *   C 流式思考   —— 保留思考，把 thinking 也推给前端（首帧立刻可见）
 */
const OLLAMA = process.env.OLLAMA || 'http://127.0.0.1:11434';
const MODEL = process.argv[2] || 'qwen3:4b';

const ANSWER_SYSTEM = `你是严谨的个人知识库助手。

规则：
1. 只依据【参考资料】回答，不得编造资料中没有的内容。
2. 参考资料无法回答时，直接说明「资料中没有相关内容」，不要猜测。
3. 用中文回答，简洁准确；涉及要点时用条目列出。
4. 引用了某段资料的地方，用 [编号] 标注来源。`;

const CONTEXT = `【参考资料】
[1] 来源：rag-kb 设计笔记（第 3 块）
向量缓存以 sha256(文本 + 模型) 为键，命中则跳过最贵的向量计算。解析缓存以 sha256(文件内容) 为键，跳过大文档解析。回答缓存以 sha256(问题 + 上下文哈希 + 历史哈希 + provider + model + 温度) 为键。

[2] 来源：rag-kb 设计笔记（第 4 块）
把所有影响结果的参数都塞进键里，于是「改了输入就自动不命中」，永远不存在返回陈旧结果的窗口，清空缓存也就只是释放空间，不影响正确性。

[3] 来源：rag-kb 运维记录（第 1 块）
缓存故障一律降级为未命中而不是抛错 —— 缓存不该让主流程挂掉。

【问题】
`;

const QUESTIONS = [
  {
    tag: '要点归纳',
    q: '三层缓存分别省掉了什么开销？它们的键是怎么设计的，为什么改了输入就自动不命中？',
  },
  {
    tag: '跨段推理',
    q: '如果往回答缓存的键里漏掉了温度参数，会出现什么问题？请结合资料说明。',
  },
];

/** 流式调用，分别记录首个 thinking 帧、首个 content 帧的到达时刻 */
async function run(system, user, { think } = {}) {
  const body = {
    model: MODEL,
    messages: [
      { role: 'system', content: system },
      { role: 'user', content: user },
    ],
    stream: true,
    options: { temperature: 0.2, num_ctx: 8192 },
  };
  if (think !== undefined) body.think = think;

  const t0 = Date.now();
  const r = await fetch(`${OLLAMA}/api/chat`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });

  let firstThink = null;
  let firstContent = null;
  let thinking = '';
  let content = '';
  let lineBuf = '';
  const dec = new TextDecoder();

  for await (const chunk of r.body) {
    lineBuf += dec.decode(chunk, { stream: true });
    let nl;
    while ((nl = lineBuf.indexOf('\n')) >= 0) {
      const line = lineBuf.slice(0, nl).trim();
      lineBuf = lineBuf.slice(nl + 1);
      if (!line) continue;
      let j;
      try {
        j = JSON.parse(line);
      } catch {
        continue;
      }
      const th = j.message?.thinking || '';
      const ct = j.message?.content || '';
      if (th) {
        if (firstThink === null) firstThink = Date.now() - t0;
        thinking += th;
      }
      if (ct) {
        if (firstContent === null) firstContent = Date.now() - t0;
        content += ct;
      }
    }
  }
  return { total: Date.now() - t0, firstThink, firstContent, thinking, content };
}

// D 变体加的这段：模型关掉思考后仍会在正文里自述分析过程，
// 而「复述问题」这个动作本身就会把问题再生成一遍 —— 那正是它在正文里干的事。
const NO_ANALYSIS = `

补充要求：直接给出答案。不要复述问题，不要写出你的分析、推理或判断过程，
不要以「首先」「用户的问题」「我需要」这类词开头。第一句话就是答案本身。`;

const VARIANTS = [
  { name: 'A 现状', think: undefined, note: 'thinking 不展示（当前行为）' },
  { name: 'B 关思考', think: false, note: 'think:false' },
  { name: 'D 关思考+禁分析', think: false, note: 'think:false + 明令禁止写分析过程', extra: NO_ANALYSIS },
];

for (const q of QUESTIONS) {
  console.log('\n' + '='.repeat(78));
  console.log(`【${q.tag}】${q.q}`);
  console.log('='.repeat(78));

  for (const v of VARIANTS) {
    const sys = v.extra ? ANSWER_SYSTEM + v.extra : ANSWER_SYSTEM;
    const r = await run(sys, CONTEXT + q.q, { think: v.think });
    const ft = r.firstContent === null ? '—' : r.firstContent + 'ms';
    const tt = r.firstThink === null ? '—' : r.firstThink + 'ms';
    console.log(`\n--- ${v.name}（${v.note}）---`);
    console.log(
      `首thinking ${String(tt).padStart(7)}   首content ${String(ft).padStart(7)}   ` +
        `总耗时 ${r.total}ms   思考 ${r.thinking.length} 字   正文 ${r.content.length} 字`
    );
    console.log(`正文开头：${JSON.stringify(r.content.slice(0, 150))}`);
    // 关思考时，检查推理有没有漏进正文
    if (v.think === false) {
      const leak = /^(首先|好的|让我|我需要|用户的问题|我们来)/.test(r.content.trim());
      console.log(`推理泄漏进正文：${leak ? '❌ 有' : '✅ 无'}`);
    }
  }
}

console.log('\n\n说明：「首content」是用户真正看到第一个字的时刻。');
console.log('若「首thinking」远小于「首content」，说明这段等待本可以显示成「正在思考」。');
