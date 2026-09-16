/**
 * 提示词超过 num_ctx 时，被丢掉的是哪一段？
 *
 * 这不是学术问题。本项目的提示词顺序是：
 *   【知识库主题概览】→【更早的对话】→【对话背景】→【参考资料】→【问题】
 *
 *   丢掉开头 = 丢掉系统提示与主题概览 —— 问题还在，但回答会失去依据
 *   丢掉中间 = 资料没了 —— 模型会说「知识库中没有」，而其实是有的
 *   丢掉结尾 = 问题没了 —— 答非所问
 *
 * 三种失败模式的表现完全不同，而且都不会报错。所以必须实测。
 *
 * 做法：在提示词的开头、中间、结尾各埋一个唯一标记，超长送入，
 * 直接问模型「你看到了哪几个标记」——它能看到的就是还在上下文里的。
 */
const OLLAMA = process.env.OLLAMA || 'http://127.0.0.1:11434';
const MODEL = process.env.KB_MODEL || 'qwen3:4b';
const NUM_CTX = Number(process.env.KB_NUM_CTX || 10240);

const FILLER = '这段是填充内容，用于把提示词撑到超过上下文上限，本身没有含义。'
  + '检索增强生成会把资料拼进提示词，资料多到一定程度就会超出窗口，'
  + '而超出之后被丢弃的是哪一段，直接决定了回答会以什么方式出问题。';

async function ask(userText) {
  const r = await fetch(`${OLLAMA}/api/chat`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      model: MODEL,
      messages: [{ role: 'user', content: userText }],
      stream: false,
      think: false,
      options: { num_ctx: NUM_CTX, temperature: 0, num_predict: 200 },
    }),
  });
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return {
    promptTokens: j.prompt_eval_count || 0,
    answer: (j.message?.content || '').trim(),
  };
}

function build(totalChars) {
  const HEAD = '开头标记是 ALPHA-7。';
  const MID = '中间标记是 BRAVO-8。';
  const TAIL = '结尾标记是 CHARLIE-9。请回答：你在上面看到了哪几个标记？只列出来。';
  const body = totalChars - HEAD.length - MID.length - TAIL.length;
  let f = '';
  while (f.length < body / 2) f += FILLER;
  f = f.slice(0, Math.floor(body / 2));
  return HEAD + f + MID + f + TAIL;
}

console.log(`模型 ${MODEL} · num_ctx ${NUM_CTX}\n`);
process.stdout.write('预热…');
await ask('预热');
console.log(' 完成\n');

console.log('送入字数   实际tokens   模型看到了哪些标记');
console.log('-'.repeat(66));
for (const chars of [3000, 8000, 14000, 20000, 30000, 40000]) {
  let r;
  try {
    r = await ask(build(chars));
  } catch (e) {
    console.log(`${String(chars).padStart(8)}  失败：${e.message.slice(0, 46)}`);
    continue;
  }
  const seen = ['ALPHA-7', 'BRAVO-8', 'CHARLIE-9'].filter((m) => r.answer.includes(m));
  const label = seen.length === 3 ? '全部三个'
    : seen.length === 0 ? '⚠️ 一个都没看到（回答：' + r.answer.slice(0, 30) + '）'
      : seen.join('、');
  console.log(`${String(chars).padStart(8)}  ${String(r.promptTokens).padStart(10)}   ${label}`);
}

console.log('\n判读：');
console.log('  三个都在     → 没超上限');
console.log('  缺 ALPHA-7   → 丢开头（系统提示与主题概览没了，问题还在）');
console.log('  缺 BRAVO-8   → 丢中间（资料没了，模型会误判「知识库中没有」）');
console.log('  缺 CHARLIE-9 → 丢结尾（问题没了，答非所问）');
