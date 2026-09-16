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
  const question = `三层缓存分别省掉什么开销？请简要回答。（核对 ${Date.now().toString(36)}）`;
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
  ok('四个页签', drawer.tabs === 4);
  ok('缓存页有内容', drawer.body > 20, `${drawer.body} 字`);

  for (const [tab, expect] of [['docs', '文档'], ['model', '提供方'], ['system', '系统']]) {
    await cdp.eval(`document.querySelector('#setTabs button[data-tab="${tab}"]').click()`);
    await sleep(900);
    const t = await cdp.eval(`document.querySelector('#setBody')?.textContent || ''`);
    ok(`「${tab}」页可渲染`, t.includes(expect), t.slice(0, 46).replace(/\s+/g, ' '));
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
  // 两类预期内的 401：
  //   /api/auth/refresh —— 首次打开没有 refresh Cookie，boot() 试一次必得 401
  //   /api/auth/login   —— 步骤 2 故意输错密码那一次
  const expected = /\/api\/auth\/(refresh|login)/;
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
