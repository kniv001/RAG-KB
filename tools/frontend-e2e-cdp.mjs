/**
 * 前端端到端测试：真的开一个浏览器，真的登录，真的问一句，检查渲染。
 *
 * 为什么必须用真浏览器：加密层已经用 Node 的 WebCrypto 验过了（那段是同一套 API），
 * 但 DOM 渲染、事件绑定、SSE 逐事件解密的**主循环**只能在浏览器里跑才知道。
 * 而这三处的失败表现都是白屏或静默不更新 —— 光看代码看不出来。
 *
 * 用 CDP（Chrome DevTools Protocol）而不是截图：截图在这个环境下不可靠，
 * 而 CDP 能直接拿到 console 报错与异常栈，那才是排查白屏要的东西。
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const APP = process.env.KB_BASE || 'http://127.0.0.1:8080';
const PORT = Number(process.env.CDP_PORT || 9222);
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';

const profile = path.join(os.tmpdir(), 'kb-e2e-' + Date.now());
let pass = 0, fail = 0;
const ok = (n, c, e = '') => {
  if (c) { pass++; console.log(`  ✅ ${n}${e ? ' — ' + e : ''}`); }
  else { fail++; console.log(`  ❌ ${n}${e ? ' — ' + e : ''}`); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── 启动浏览器 ──
const browser = spawn(EDGE, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });

async function targets() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch { /* 还没起来 */ }
    await sleep(250);
  }
  throw new Error('浏览器没起来');
}

// ── 极简 CDP 客户端 ──
class CDP {
  constructor(url) { this.ws = new WebSocket(url); this.id = 0; this.pending = new Map(); this.handlers = []; }

  ready() {
    return new Promise((res, rej) => {
      this.ws.addEventListener('open', res);
      this.ws.addEventListener('error', rej);
    });
  }

  attach() {
    this.ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const { res, rej } = this.pending.get(m.id);
        this.pending.delete(m.id);
        m.error ? rej(new Error(m.error.message)) : res(m.result);
      } else if (m.method) {
        for (const h of this.handlers) h(m);
      }
    });
  }

  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  on(fn) { this.handlers.push(fn); }

  async eval(expression) {
    const r = await this.send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    }
    return r.result.value;
  }

  close() { try { this.ws.close(); } catch { /* 已关 */ } }
}

const logs = [];
const errors = [];

try {
  const page = await targets();
  const cdp = new CDP(page.webSocketDebuggerUrl);
  await cdp.ready();
  cdp.attach();

  cdp.on((m) => {
    if (m.method === 'Runtime.consoleAPICalled') {
      const text = (m.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
      logs.push(`[${m.params.type}] ${text}`);
      if (m.params.type === 'error') errors.push(text);
    }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails;
      errors.push(d.exception?.description || d.text);
    }
    if (m.method === 'Log.entryAdded') {
      const e = m.params.entry;
      logs.push(`[log:${e.level}] ${e.text}`);
      if (e.level === 'error') errors.push(`${e.text} @ ${e.url || ''}`);
    }
  });

  await cdp.send('Runtime.enable');
  await cdp.send('Log.enable');
  await cdp.send('Page.enable');

  console.log('\n=== 1. 打开页面 ===');
  await cdp.send('Page.navigate', { url: APP + '/' });
  await sleep(2500);

  const title = await cdp.eval('document.title');
  ok('页面标题正确', title === '知识库助手', title);

  const sub = await cdp.eval(`document.querySelector('#gateSub')?.textContent`);
  ok('JS 已执行（boot() 改写了副标题）', sub === '请登录', `副标题="${sub}"`);

  const assets = await cdp.eval(`({
    css: [...document.styleSheets].length,
    js: !!document.querySelector('script[type=module]')
  })`);
  ok('样式表已加载', assets.css > 0, `${assets.css} 个`);
  ok('模块脚本已挂载', assets.js);

  console.log('\n=== 2. 登录（走真实加密）===');
  const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));

  // 先故意输错一次：登录失败必须看得见报错，而不是静默什么都不发生
  await cdp.eval(`(() => {
    document.querySelector('#username').value = ${JSON.stringify(cred.user)};
    document.querySelector('#password').value = 'definitely-not-the-password';
    document.querySelector('#loginForm').requestSubmit();
    return true;
  })()`);
  await sleep(2500);
  const wrong = await cdp.eval(`(() => {
    const e = document.querySelector('#loginErr');
    const g = document.querySelector('#gate');
    return {
      errShown: !!e && !e.hidden && getComputedStyle(e).display !== 'none',
      errText: e?.textContent || '',
      stillOnGate: getComputedStyle(g).display !== 'none',
    };
  })()`);
  ok('密码错误时显示报错', wrong.errShown, wrong.errText.slice(0, 40));
  ok('  且仍停在登录页', wrong.stillOnGate);

  await cdp.eval(`(() => {
    document.querySelector('#username').value = ${JSON.stringify(cred.user)};
    document.querySelector('#password').value = ${JSON.stringify(cred.password)};
    document.querySelector('#loginForm').requestSubmit();
    return true;
  })()`);

  await sleep(3500);
  // 必须看计算样式，不能看 element.hidden —— 属性为 true 但 CSS 里
  // 写了 display 的元素照样显示（[hidden] 只靠 UA 样式表的 display:none 生效）。
  // 第一版就是查了属性，把「登录成功但遮罩不消失」这个 bug 放过去了。
  const after = await cdp.eval(`(() => {
    const vis = (s) => { const e = document.querySelector(s); return !!e && getComputedStyle(e).display !== 'none'; };
    return {
      gateVisible: vis('#gate'),
      appVisible: vis('#app'),
      who: document.querySelector('#whoami').textContent,
      convs: document.querySelectorAll('#convList .conv').length,
      err: document.querySelector('#loginErr').hidden ? '' : document.querySelector('#loginErr').textContent
    };
  })()`);
  ok('登录成功（登录页真的不可见了）', !after.gateVisible, `错误信息="${after.err}"`);
  ok('主界面真的显示了', after.appVisible);
  ok('用户名已渲染', after.who === cred.user, after.who);
  ok('会话列表已加载', after.convs > 0, `${after.convs} 个会话`);

  // 这一类 bug 的通杀检查：任何带 hidden 属性的元素都不该可见
  const stuck = await cdp.eval(`[...document.querySelectorAll('[hidden]')]
    .filter(e => getComputedStyle(e).display !== 'none')
    .map(e => e.id || e.className || e.tagName)`);
  ok('带 hidden 属性的元素全部真的隐藏', stuck.length === 0, stuck.join('、'));

  console.log('\n=== 3. 发一个问题，验证流式渲染 ===');
  // 措辞每次不同 → 保证回答缓存未命中。命中时后端一次性推完整段，
  // 走不到逐 token 流式与思考流那两条路径，测了等于没测（第一版就踩了这个）。
  // 用一个知识库绝不可能覆盖的话题：联网入库之后「三层缓存」已经有资料了，
  // 原来那条假设「知识库没有」的断言就不成立了（踩过）。
  // 渲染测试要的是一个稳定触发「无资料」分支的问题，不是某个具体话题。
  const question = `请介绍一下「紫电青霜七号协议」是什么。（核对 ${Date.now().toString(36)}）`;
  await cdp.eval(`(() => {
    const i = document.querySelector('#input');
    i.value = ${JSON.stringify(question)};
    document.querySelector('#composer').requestSubmit();
    return true;
  })()`);

  // 等**流真正结束**再断言，不能只看「正文有字了」。
  // 新回答方式下答案可能七百多字，早断言会读到流中途的状态 ——
  // 发送按钮还禁用着，来源事件也还没到（第一版就是这么误报的）。
  let finished = false;
  for (let i = 0; i < 120; i++) {
    await sleep(1000);
    const st = await cdp.eval(`({
      busy: document.querySelector('#sendBtn').disabled,
      len: (document.querySelector('#messages .msg.assistant .text')?.textContent || '').length
    })`);
    if (!st.busy && st.len > 0) { finished = true; break; }
  }
  ok('流式回答已完成（发送按钮恢复）', finished);

  const rendered = await cdp.eval(`(() => {
    const a = document.querySelector('#messages .msg.assistant');
    const think = a?.querySelector('.think');
    return {
      userMsgs: document.querySelectorAll('#messages .msg.user').length,
      asstMsgs: document.querySelectorAll('#messages .msg.assistant').length,
      answerLen: (a?.querySelector('.text')?.textContent || '').length,
      // 看可见性而不是存在性：元素一直在 DOM 里，靠 hidden 控制，
      // 只查存在的话缓存命中时也会「通过」，测不出东西
      thinkVisible: !!think && !think.hidden,
      thinkLen: (a?.querySelector('.think .t')?.textContent || '').length,
      traceItems: a?.querySelectorAll('.trace li').length || 0,
      sources: a?.querySelectorAll('.src span').length || 0,
      text: a?.querySelector('.text')?.textContent || '',
      html: a?.querySelector('.text')?.innerHTML.slice(0, 120) || '',
      busy: document.querySelector('#sendBtn').disabled,
    };
  })()`);

  ok('用户消息已渲染', rendered.userMsgs >= 1);
  ok('助手消息已渲染', rendered.asstMsgs >= 1);
  ok('正文非空', rendered.answerLen > 0, `${rendered.answerLen} 字`);
  ok('正文被渲染成 HTML（markdown 生效）', rendered.html.includes('<'), rendered.html.slice(0, 60).replace(/\n/g, ' '));
  ok('思考块可见且有内容', rendered.thinkVisible && rendered.thinkLen > 0, `${rendered.thinkLen} 字`);
  ok('agent 过程时间线有内容', rendered.traceItems > 0, `${rendered.traceItems} 条`);
  // 本用例问的是知识库没覆盖的话题，所以按新的三段式应当得到
  // 「先声明没有 + 通用知识回答 + 标注来源边界」，而不是一句拒答
  ok('没覆盖时给出通用知识回答并标注边界',
     /(知识库|资料)[^。\n]{0,12}(没有|未|无)/.test(rendered.text) && /(通用知识|未引用)/.test(rendered.text),
     rendered.text.slice(0, 46).replace(/\n/g, ' '));
  ok('流已结束（发送按钮可用）', rendered.busy === false);

  console.log('\n=== 4. 设置抽屉 ===');
  await cdp.eval(`document.querySelector('#openSettings').click()`);
  await sleep(1200);
  const drawer = await cdp.eval(`(() => {
    const d = document.querySelector('#settings');
    return {
      open: !!d && getComputedStyle(d).display !== 'none',
      body: (document.querySelector('#setBody')?.textContent || '').length,
      tabs: document.querySelectorAll('#setTabs button').length
    };
  })()`);
  ok('抽屉已打开', drawer.open);
  ok('五个页签', drawer.tabs === 5, `${drawer.tabs} 个`);
  ok('缓存页有内容', drawer.body > 20, `${drawer.body} 字`);

  for (const [tab, expect] of [['docs', '文档'], ['web', '联网'], ['model', '提供方'], ['system', '系统']]) {
    await cdp.eval(`document.querySelector('#setTabs button[data-tab="${tab}"]').click()`);
    await sleep(900);
    const t = await cdp.eval(`document.querySelector('#setBody')?.textContent || ''`);
    ok(`「${tab}」页可渲染`, t.includes(expect), t.slice(0, 46).replace(/\s+/g, ' '));
  }

  console.log('\n=== 4.5 渲染管线：markdown / 公式 / XSS ===');
  {
    const libs = await cdp.eval(`({
      marked: typeof window.marked,
      purify: typeof window.DOMPurify,
      katex: typeof window.katex,
      hook: typeof window.__kbRender,
    })`);
    ok('marked 已加载', libs.marked === 'object' || libs.marked === 'function', libs.marked);
    // DOMPurify 本身是个可调用的工厂函数，不是普通对象
    ok('DOMPurify 已加载', libs.purify === 'function' || libs.purify === 'object', libs.purify);
    ok('KaTeX 已加载', libs.katex === 'object', libs.katex);
    ok('渲染入口已暴露给测试', libs.hook === 'object');

    // 用**本应用自己的**渲染函数，而不是自己去调 marked/DOMPurify ——
    // 后者测的是库，万一应用漏了 sanitize 也照样绿
    const xss = await cdp.eval(`(() => {
      window.__pwned = false;
      const payload = '<img src=x onerror="window.__pwned=true">' +
                      '<script>window.__pwned=true<\\/script>' +
                      '<a href="javascript:window.__pwned=true">click</a>';
      const out = window.__kbRender.md(payload);
      const d = document.createElement('div');
      d.innerHTML = out;
      document.body.append(d);
      return { html: out, pwned: window.__pwned, hasOnerror: /onerror/i.test(out), hasJsHref: /javascript:/i.test(out) };
    })()`);
    await sleep(400);
    const pwned = await cdp.eval('window.__pwned');
    ok('XSS 载荷被清洗（onerror 被移除）', !xss.hasOnerror, xss.html.slice(0, 70));
    ok('XSS 载荷被清洗（javascript: 协议被移除）', !xss.hasJsHref);
    ok('脚本没有执行', pwned === false);

    const markdown = await cdp.eval(`(() => {
      const src = [
        '# 标题', '', '普通段落，含 **粗体** 与 \`行内代码\`。', '',
        '| 列A | 列B |', '| --- | --- |', '| 1 | 2 |', '',
        '- 项目一', '- 项目二', '',
        '> 引用', '',
        '\`\`\`js', 'const x = 1;', '\`\`\`',
      ].join('\\n');
      const d = document.createElement('div');
      d.innerHTML = window.__kbRender.md(src);
      return {
        h1: !!d.querySelector('h1'),
        strong: !!d.querySelector('strong'),
        code: !!d.querySelector('code'),
        table: !!d.querySelector('table'),
        ul: !!d.querySelector('ul'),
        quote: !!d.querySelector('blockquote'),
        pre: !!d.querySelector('pre'),
      };
    })()`);
    ok('标题 / 粗体 / 行内代码', markdown.h1 && markdown.strong && markdown.code);
    ok('表格（GFM）', markdown.table, '之前的最小实现不支持表格');
    ok('列表 / 引用 / 代码块', markdown.ul && markdown.quote && markdown.pre);

    const math = await cdp.eval(`(() => {
      const d = document.createElement('div');
      window.__kbRender.paint(d, '行内公式 $E = mc^2$ 与行间公式：\\n\\n$$\\\\sum_{i=1}^{n} i = \\\\frac{n(n+1)}{2}$$');
      return {
        katexNodes: d.querySelectorAll('.katex').length,
        display: d.querySelectorAll('.katex-display').length,
        // 渲染失败时 KaTeX 会留下 .katex-error
        errors: d.querySelectorAll('.katex-error').length,
        text: d.textContent.slice(0, 60),
      };
    })()`);
    ok('行内公式渲染成 KaTeX 节点', math.katexNodes >= 1, `${math.katexNodes} 个`);
    ok('行间公式渲染为 display 模式', math.display >= 1, `${math.display} 个`);
    ok('没有渲染错误', math.errors === 0);

    const noMathInCode = await cdp.eval(`(() => {
      const d = document.createElement('div');
      window.__kbRender.paint(d, '\`\`\`\\n价格是 $100 不是公式\\n\`\`\`');
      return d.querySelectorAll('.katex').length;
    })()`);
    ok('代码块里的 $ 不被当作公式', noMathInCode === 0);

    const badFormula = await cdp.eval(`(() => {
      const d = document.createElement('div');
      window.__kbRender.paint(d, '坏公式 $\\\\frac{1}{$ 之后还有正文');
      return d.textContent.length;
    })()`);
    ok('公式写错不会让整段挂掉', badFormula > 0, `正文仍有 ${badFormula} 字`);

    // ── 这一条是为一个真实 bug 补的 ──
    // 用户报「公式显示成原始字符」，根因是 markdown 的转义处理把 LaTeX 里的
    // \\（矩阵换行符）吃成了一个 \，下划线也会被当成强调符。
    // 所以必须验「带 \\ 与 _ 的公式」——只验 E=mc² 那种简单公式是测不出来的。
    const pmatrix = await cdp.eval(`(() => {
      const tex = '$$\\\\begin{pmatrix} a & b \\\\\\\\ c & d \\\\end{pmatrix} \\\\quad x_{11} = (-1)^{1+1} M_{11}$$';
      const d = document.createElement('div');
      document.body.append(d);          // innerText 需要真实布局，脱离文档的节点会退化成 textContent
      window.__kbRender.paint(d, tex);
      const text = d.innerText;
      d.remove();
      return {
        katex: d.querySelectorAll('.katex').length,
        errors: d.querySelectorAll('.katex-error').length,
        // 没渲染的话，LaTeX 源码会以可见文本出现。
        // 注意必须用 innerText 而不是 textContent —— KaTeX 会在 <annotation>
        // 里存一份源码供复制粘贴与无障碍用，那份在 CSS 里是隐藏的，
        // 但 textContent 照样读得到（第一版就是这么误报的）
        rawLeak: /begin\\{pmatrix\\}|\\\\quad|a_\\{11\\}/.test(text),
        text: text.replace(/\\s+/g, ' ').slice(0, 50),
      };
    })()`);
    ok('带 \\\\ 换行与 _ 下标的公式能渲染', pmatrix.katex >= 1,
       pmatrix.katex ? `${pmatrix.katex} 个节点` : '没渲染出来');
    ok('  且渲染无错误', pmatrix.errors === 0);
    ok('  且 LaTeX 源码没有以可见文本漏进正文', !pmatrix.rawLeak, pmatrix.text);
  }

  console.log('\n=== 4.6 真实回答里的公式（完整链路）===');
  {
    // 前面 4.5 是直接调 paint()，验的是渲染函数本身；
    // 这一步走完整链路：提问 → 流式接收 → 结束后渲染公式。
    // 缺了它，就测不出「流式途中 math=false、结束时才 math=true」这段接线有没有断。
    await cdp.eval(`(() => {
      const i = document.querySelector('#input');
      i.value = '余弦相似度的计算公式是什么？请给出公式。';
      document.querySelector('#composer').requestSubmit();
      return true;
    })()`);

    let done = false;
    for (let i = 0; i < 120; i++) {
      await sleep(1000);
      const st = await cdp.eval(`({
        busy: document.querySelector('#sendBtn').disabled,
        nodes: document.querySelectorAll('#messages .msg.assistant .katex').length
      })`);
      if (!st.busy) { done = true; break; }
    }
    ok('第二轮回答已完成', done);

    const real = await cdp.eval(`(() => {
      const msgs = [...document.querySelectorAll('#messages .msg.assistant')];
      const last = msgs[msgs.length - 1];
      const text = last?.querySelector('.text')?.textContent || '';
      return {
        katex: last?.querySelectorAll('.katex').length || 0,
        errors: last?.querySelectorAll('.katex-error').length || 0,
        hasDollar: /\\$/.test(text),
        src: text.slice(0, 80).replace(/\\n/g, ' '),
      };
    })()`);
    console.log(`  末条回答：${real.src}`);
    ok('真实回答里的公式被渲染成 KaTeX', real.katex > 0,
       real.katex > 0 ? `${real.katex} 个公式节点` : (real.hasDollar ? '回答里有 $ 但没渲染' : '回答里没有公式，换个问题'));
    ok('公式渲染无错误', real.errors === 0);
  }

  console.log('\n=== 4.7 点击事件（h() 里那些 onClick 绑的）===');
  {
    // 这一节是为一个 bug 补的：h() 里写成 addEventListener(k.slice(2))，
    // 'onClick'.slice(2) 得到 'Click'（大写 C），而事件名大小写敏感 ——
    // 于是凡是用 h(..., { onClick }) 绑的地方全是哑的。之前测的新建对话、
    // 设置、页签恰好都是显式写 addEventListener('click', ...)，所以一直没发现。
    const before = await cdp.eval(`({
      rows: document.querySelectorAll('#convList .conv').length,
      title: document.querySelector('#convTitle').textContent
    })`);
    ok('会话列表有条目', before.rows >= 1, `${before.rows} 个`);

    // 点另一条会话的标题（子元素）——顺带验证事件能冒泡到行上的监听器
    const target = await cdp.eval(`(() => {
      const rows = [...document.querySelectorAll('#convList .conv')];
      const t = rows.find((r) => !r.classList.contains('on')) || rows[0];
      const title = t.querySelector('.t').textContent;
      t.querySelector('.t').click();
      return title;
    })()`);
    await sleep(1600);
    const after = await cdp.eval(`({
      title: document.querySelector('#convTitle').textContent,
      msgs: document.querySelectorAll('#messages .msg').length,
      highlighted: !!document.querySelector('#convList .conv.on')
    })`);
    const same = after.title.includes(target.slice(0, 8)) || target.includes(after.title.slice(0, 8));
    ok('点击会话能切过去', same, `目标「${target.slice(0, 14)}」→ 现标题「${after.title.slice(0, 14)}」`);
    ok('  该会话的消息渲染出来了', after.msgs > 0, `${after.msgs} 条`);
    ok('  列表高亮跟着切', after.highlighted);

    // 设置里同样用 onClick 绑的按钮
    await cdp.eval(`document.querySelector('#openSettings').click()`);
    await sleep(600);
    await cdp.eval(`document.querySelector('#setTabs button[data-tab="system"]').click()`);
    await sleep(500);
    await cdp.eval(`[...document.querySelectorAll('#setBody button')].find(b => b.textContent.includes('健康检查'))?.click()`);
    await sleep(2500);
    const health = await cdp.eval(`document.querySelector('#healthOut')?.textContent || ''`);
    ok('设置里的「健康检查」按钮生效（同样走 onClick）',
       /database|service|version/i.test(health), health.slice(0, 50).replace(/\n/g, ' '));
    await cdp.eval(`document.querySelector('#closeSettings').click()`);
    await sleep(300);
  }

  console.log('\n=== 5. 附件：加密上传 + 自动建索引 ===');
  await cdp.eval(`document.querySelector('#closeSettings').click()`);
  await sleep(300);

  // 用 DataTransfer 造一个文件塞进 input，再触发 change —— 等价于用户选了文件
  const attName = `e2e-${Date.now().toString(36)}.md`;
  await cdp.eval(`(() => {
    const dt = new DataTransfer();
    dt.items.add(new File(
      ['# E2E 附件\\n\\n唯一标记 E2E-${attName}\\n\\n中文与 emoji 🎯 用于验证加密往返。'],
      ${JSON.stringify(attName)}, { type: 'text/markdown' }));
    const input = document.querySelector('#fileInput');
    input.files = dt.files;
    input.dispatchEvent(new Event('change'));
    return true;
  })()`);

  let att = null;
  for (let i = 0; i < 60; i++) {
    await sleep(1000);
    att = await cdp.eval(`(() => {
      const e = document.querySelector('#attachments .att');
      if (!e) return null;
      return { cls: e.className, text: e.textContent, visible: getComputedStyle(document.querySelector('#attachments')).display !== 'none' };
    })()`);
    if (att && /done|error/.test(att.cls)) break;
  }
  ok('附件区出现且可见', !!att && att.visible, att ? att.text.slice(0, 46) : '无附件元素');
  ok('附件已加密上传并建完索引', !!att && att.cls.includes('done'), att?.text.slice(0, 60));

  console.log('\n=== 6. 控制台 ===');
  // 预期内的请求噪音：
  //   /api/auth/refresh —— 首次打开没有 refresh Cookie，boot() 试一次必得 401
  //   /api/auth/login   —— 步骤 2 故意输错密码那一次
  //   /x                —— 步骤 4.5 的 XSS 载荷里有 <img src=x>，DOMPurify 正确地
  //                        只摘掉了危险的 onerror、保留了标签，浏览器便去请求 /x，
  //                        匿名访问它当然是 401。这是测试载荷的副作用，不是应用问题。
  const expected = /\/api\/auth\/(refresh|login)|\/x$/;
  const noisy = /favicon|DevTools|Autofill/i;
  const realErrors = errors.filter((e) => !noisy.test(e) && !expected.test(e));
  const expectedHits = errors.filter((e) => expected.test(e) && !noisy.test(e));
  if (expectedHits.length) console.log(`  （已忽略 ${expectedHits.length} 条：首次打开时的 refresh 401，属正常分支）`);
  ok('没有意料之外的 console 报错', realErrors.length === 0);
  if (realErrors.length) realErrors.slice(0, 6).forEach((e) => console.log(`     ⚠️  ${e.slice(0, 160)}`));

  cdp.close();
} catch (e) {
  fail++;
  console.log(`\n❌ 测试中断：${e.message}`);
} finally {
  browser.kill();
  await sleep(400);
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch { /* 文件占用，留系统清 */ }
}

console.log(`\n${'─'.repeat(52)}`);
console.log(`通过 ${pass} 项，失败 ${fail} 项`);
process.exit(fail ? 1 : 0);
