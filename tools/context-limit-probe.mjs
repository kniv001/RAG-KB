/**
 * 上下文长度 与 速度。
 *
 * 关键设计：**每种长度测两遍，一遍冷、一遍热**。
 *
 * 上一版没这么做，结果数据是错的：填充文本每次都一样，于是从第二次起
 * Ollama 复用了前缀 KV，测出来的「前算耗时」根本不是冷启动 ——
 * 表现在数据上就是「6198 token 花 247ms」比「5581 token 花 437ms」还快，
 * 长了反而更快，物理上不可能。
 *
 * 冷 / 热 两种数各有各的用处：
 *   冷 = 前缀与之前任何请求都不同，全部要重算 —— 最坏情况
 *   热 = 前缀与上一次完全相同 —— 这正是本项目主题概览的情形
 *        （它是固定前缀，实测复用后 819ms → 44ms）
 *
 * 读的是 /api/chat 的计时字段，不靠墙钟反推：
 *   prompt_eval_count / prompt_eval_duration   前算
 *   eval_count / eval_duration                 生成
 */
const OLLAMA = process.env.OLLAMA || 'http://127.0.0.1:11434';
const MODEL = process.env.KB_MODEL || 'qwen3:4b';
const NUM_CTX = Number(process.env.KB_NUM_CTX || 10240);

const FILLER = '这段文字用于填充上下文，本身没有实际含义，只是为了让提示词达到指定长度。'
  + '检索增强生成系统需要把检索到的资料拼进提示词，资料越多提示词越长，'
  + '而模型处理提示词的耗时随长度增长，这个增长是不是线性的值得单独测一次。';

async function ask(userText, numPredict = 32) {
  const t0 = Date.now();
  const r = await fetch(`${OLLAMA}/api/chat`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: MODEL,
      messages: [{ role: 'user', content: userText }],
      stream: false,
      think: false,
      format: { type: 'object', properties: { ok: { type: 'boolean' } }, required: ['ok'] },
      options: { num_ctx: NUM_CTX, temperature: 0, num_predict: numPredict },
    }),
  });
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  const preMs = (j.prompt_eval_duration || 0) / 1e6;
  const genMs = (j.eval_duration || 0) / 1e6;
  return {
    promptTokens: j.prompt_eval_count || 0,
    preMs,
    genTokens: j.eval_count || 0,
    genMs,
    wall: Date.now() - t0,
    tokPerSec: genMs > 0 ? (j.eval_count || 0) / (genMs / 1000) : 0,
  };
}

/** 前缀加一个唯一标记 —— 这样整段都要重算，测的是冷启动 */
function coldText(chars, nonce) {
  let s = `【标记 ${nonce}】`;
  while (s.length < chars) s += FILLER;
  return s.slice(0, chars);
}

/** 前缀完全相同 —— 复用它，测的是实际使用情形 */
function warmText(chars) {
  let s = '';
  while (s.length < chars) s += FILLER;
  return s.slice(0, chars) + '\n请只输出 {"ok":true}';
}

console.log(`模型 ${MODEL} · num_ctx ${NUM_CTX} · q8 KV + flash attention\n`);
process.stdout.write('预热中…');
await ask('预热', 8);
console.log(' 完成\n');

const SIZES = [500, 2000, 4000, 6000, 8000, 10000, 12000, 16000, 20000];

console.log('=== 冷启动：前缀唯一，全部重算（最坏情况）===');
console.log('送入字数   实际tokens   前算耗时   每千token   生成tok/s   首token前要等');
console.log('-'.repeat(80));
const cold = [];
for (const chars of SIZES) {
  try {
    const r = await ask(coldText(chars, crypto.randomUUID().slice(0, 8)));
    cold.push({ chars, ...r });
    console.log(
      `${String(chars).padStart(8)}  ${String(r.promptTokens).padStart(10)}  ` +
        `${String(Math.round(r.preMs) + 'ms').padStart(9)}  ` +
        `${String((r.preMs / Math.max(1, r.promptTokens / 1000)).toFixed(1) + 'ms').padStart(10)}  ` +
        `${String(r.tokPerSec.toFixed(1)).padStart(10)}  ` +
        `${String(Math.round(r.preMs) + 'ms').padStart(12)}`
    );
  } catch (e) {
    console.log(`${String(chars).padStart(8)}  失败：${e.message.slice(0, 46)}`);
  }
}

console.log('\n=== 热前缀：前缀与上一次相同（实际使用情形）===');
console.log('送入字数   实际tokens   前算耗时   对比冷启动');
console.log('-'.repeat(60));
// 连续两次同样的前缀：第一次是冷的，第二次才是热的
for (const chars of [4000, 8000, 12000]) {
  const t = warmText(chars);
  const a = await ask(t);
  const b = await ask(t);
  const ratio = a.preMs > 0 ? b.preMs / a.preMs : 1;
  console.log(
    `${String(chars).padStart(8)}  ${String(b.promptTokens).padStart(10)}  ` +
      `${String(Math.round(b.preMs) + 'ms').padStart(9)}  ` +
      `${a.preMs.toFixed(0)}ms → ${b.preMs.toFixed(0)}ms（省 ${Math.round((1 - ratio) * 100)}%）`
  );
}

console.log('\n=== 判读 ===');
if (cold.length >= 3) {
  const small = cold.find((r) => r.promptTokens > 100) || cold[0];
  const big = cold[cold.length - 1];
  // 边际成本：用首尾两点的增量算，而不是各自的平均值 ——
  // 平均值里混着固定开销，会把边际成本算低
  const marginal = (big.preMs - small.preMs) / Math.max(1, big.promptTokens - small.promptTokens);
  console.log(`  冷启动边际成本：约 ${(marginal * 1000).toFixed(0)}ms / 千 token`);
  console.log(`  最大一次（${big.promptTokens} token）前算 ${Math.round(big.preMs)}ms —— 这就是首 token 前的等待`);
  const drop = cold[0].tokPerSec / big.tokPerSec;
  console.log(`  解码速度：短上下文 ${cold[0].tokPerSec.toFixed(1)} tok/s → 长上下文 ${big.tokPerSec.toFixed(1)} tok/s（慢 ${drop.toFixed(2)}×）`);
}
