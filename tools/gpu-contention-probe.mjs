/**
 * 后台任务会不会拖慢用户请求？
 *
 * 机制：Ollama 只有一个推理槽（OLLAMA_NUM_PARALLEL=1），请求是 FIFO 排队的。
 * 所以后台任务（滚动摘要、历史索引）在跑的时候，用户的问题只能等。
 *
 * 但「会不会」和「有多严重」是两回事 —— 如果后台那条只要一两秒，影响可能
 * 小到不值得为它加一套调度；如果要十几秒，那就必须做优先级。
 * 所以这里直接量。
 *
 * 做法：
 *   ① 单独跑一条「用户请求」，记下耗时作为基线
 *   ② 先发一条「后台任务」，0.4 秒后再发「用户请求」，看后者被拖慢多少
 *   ③ 把后台任务的长度从短到长扫一遍，看拖慢量怎么变
 */
const OLLAMA = process.env.OLLAMA || 'http://127.0.0.1:11434';
const MODEL = process.env.KB_MODEL || 'qwen3:4b';
const NUM_CTX = Number(process.env.KB_NUM_CTX || 10240);

async function chat(userText, opts = {}) {
  const t0 = Date.now();
  const r = await fetch(`${OLLAMA}/api/chat`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: MODEL,
      messages: [{ role: 'user', content: userText }],
      stream: false,
      think: opts.think ?? false,
      ...(opts.json ? { format: { type: 'object', properties: { ok: { type: 'boolean' } }, required: ['ok'] } } : {}),
      options: { num_ctx: NUM_CTX, temperature: 0, num_predict: opts.numPredict ?? 32 },
    }),
  });
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return {
    wall: Date.now() - t0,
    gen: j.eval_count || 0,
    genMs: (j.eval_duration || 0) / 1e6,
    preMs: (j.prompt_eval_duration || 0) / 1e6,
  };
}

const FILLER = '填充内容，用于模拟后台任务要处理的一大段文本。检索增强生成系统需要把资料拼进提示词。';
const big = (n) => { let s = ''; while (s.length < n) s += FILLER; return s.slice(0, n); };

console.log(`模型 ${MODEL} · num_ctx ${NUM_CTX}\n`);
process.stdout.write('预热…');
await chat('预热');
console.log(' 完成\n');

// ① 基线：用户请求单独跑
const base = [];
for (let i = 0; i < 3; i++) base.push(await chat('一句话回答：缓存是什么？', { json: true }));
const baseAvg = base.reduce((a, b) => a + b.wall, 0) / base.length;
console.log(`用户请求基线（单独跑 3 次）：平均 ${Math.round(baseAvg)}ms`);
console.log(`  明细：${base.map((b) => Math.round(b.wall) + 'ms').join(' / ')}\n`);

console.log('=== 后台任务同时跑时，用户请求要等多久 ===');
console.log('后台任务长度      后台耗时    用户请求耗时   被拖慢     用户首token前等待');
console.log('-'.repeat(82));

for (const bgLen of [500, 3000, 8000, 20000]) {
  // 后台任务：一大段提示词 + 较长生成，模拟摘要/改写这类活
  const bg = chat(big(bgLen) + '\n请用一句话概括上面这段话。', { think: false, numPredict: 120 })
    .catch((e) => ({ wall: -1, err: e.message }));

  // 等后台任务确实进到模型里，再发用户请求
  await new Promise((r) => setTimeout(r, 400));
  const user = await chat('一句话回答：缓存是什么？', { json: true });
  const bgDone = await bg;

  const slow = user.wall - baseAvg;
  console.log(
    `${String(bgLen).padStart(12)} 字  ${String(Math.round(bgDone.wall) + 'ms').padStart(9)}  ` +
      `${String(Math.round(user.wall) + 'ms').padStart(12)}  ` +
      `${String((slow > 0 ? '+' : '') + Math.round(slow) + 'ms').padStart(9)}  ` +
      `${String(Math.round(user.preMs) + 'ms').padStart(14)}`
  );
}

console.log('\n判读：');
console.log('  「用户请求耗时」明显大于基线 → 后台任务确实在抢槽，用户要排队等它');
console.log('  「用户首token前等待」（prefill）不受影响 → 排队是在等槽，不是算得慢');
