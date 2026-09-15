/**
 * 前缀复用探针：树状存储能不能省，全看这一件事。
 *
 * 树的上层节点（根 + 章节摘要）如果每轮都重新进提示词，那它比扁平检索还贵 ——
 * 因为扁平检索一次性付 6 块原文，树是「每下降一层再付一次」。
 *
 * 树唯一可能划算的情形是：这部分固定前缀的 KV 被复用，后续查询只为增量付费。
 * 那就必须验证 Ollama 到底复不复用。
 *
 * 判据是 prompt_eval_count（本次请求实际前算了多少 token）：
 *   第二次请求若 ≈ 第一次 —— 完全不复用，树的上层是纯开销
 *   第二次请求若 << 第一次 —— 复用了，树可行
 */
const OLLAMA = process.env.OLLAMA || 'http://127.0.0.1:11434';
const MODEL = process.argv[2] || 'qwen3:4b';

// 造一段固定前缀，模拟「树的上层节点」。目标 ~2000 token
//
// 开头这个随机标记是必须的：不加的话，前一次运行留下的 KV 还活着，
// 「第 1 次」测到的其实是缓存命中，冷启动根本测不出来（实测踩过：
// 同一段前缀隔了几分钟再发，第 1 次 prefill 就只有 47ms）。
const NONCE = Math.random().toString(36).slice(2, 10);
const UNIT = '第三章讨论了检索增强生成的召回策略。向量检索按余弦距离排序，关键词检索按词频打分，' +
  '混合检索用倒数排名融合把两路结果合并。倒数排名融合对分数尺度不敏感，' +
  '因为它只用排名不用分数，这一点在向量距离与词频分数不可比时尤其关键。';
const FIXED = `会话标记 ${NONCE}。以下是知识库的章节概览，供你判断该往哪个分支深入。\n\n` +
  Array.from({ length: 40 }, (_, i) => `[${i + 1}] ${UNIT}`).join('\n');

async function ask(user, label) {
  const t0 = Date.now();
  const r = await fetch(`${OLLAMA}/api/chat`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: MODEL,
      messages: [
        { role: 'system', content: '你是知识库导航助手。只输出 JSON。' },
        { role: 'user', content: user },
      ],
      stream: false,
      think: false,
      format: { type: 'object', properties: { ok: { type: 'boolean' } }, required: ['ok'] },
      options: { num_ctx: 8192, num_predict: 8 },
    }),
  });
  const j = await r.json();
  const ms = Date.now() - t0;
  // 注意：prompt_eval_count 是「提示词总长」，不是「实际重算了多少」——
  // 复用了前缀它照样报全长。真正能看出复用的是 prefill 耗时。
  const prefill = Math.round((j.prompt_eval_duration || 0) / 1e6);
  console.log(
    `${label.padEnd(12)} prompt_tokens=${String(j.prompt_eval_count).padStart(6)}  ` +
      `prefill=${String(prefill + 'ms').padStart(8)}  ` + `墙钟=${ms}ms`
  );
  return { count: j.prompt_eval_count, prefill };
}

console.log(`模型 ${MODEL}\n`);
console.log('前缀长度约 ' + FIXED.length + ' 字\n');

// 预热，把模型装载排除掉
await ask('预热', '预热');

// 第 1 次：完整前缀
const a = await ask(FIXED + '\n\n问题：向量检索怎么排序？', '第1次');

// 第 2 次：完全相同的前缀，只换尾巴
const b = await ask(FIXED + '\n\n问题：混合检索怎么融合？', '第2次');

// 第 3 次：前缀再砍掉一半，看前算量是否随之下降
const half = FIXED.slice(0, Math.floor(FIXED.length / 2));
const c = await ask(half + '\n\n问题：关键词检索怎么打分？', '第3次(半前缀)');

const ratio = b.prefill / a.prefill;
console.log('\n判读（看 prefill 耗时，不看 token 数）：');
console.log(
  `  第2次 prefill / 第1次 = ${b.prefill}ms / ${a.prefill}ms = ${ratio.toFixed(2)}  → ` +
    (ratio < 0.5 ? '✅ 前缀被复用，树的上层可以只付一次' : '❌ 没有复用，树的上层每轮全额付费')
);
console.log(`  单 token 前算成本约 ${(a.prefill / a.count).toFixed(3)} ms`);
console.log(
  `  若不复用，第2次应约 ${a.prefill}ms；实测 ${b.prefill}ms，` +
    `相当于省掉 ${Math.round((1 - ratio) * 100)}%`
);
