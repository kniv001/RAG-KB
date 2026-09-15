/**
 * 知识库助手前端。无框架，一个文件。
 *
 * 三件事必须自己实现，因为没有任何库在做：
 *   ① 端到端加密 —— 每个请求新生成 AES 密钥，用服务端 RSA 公钥包起来；
 *      请求体是密文，响应体也是密文，SSE 则逐事件加密。
 *   ② 双令牌 —— access 走内存（放 localStorage 会给 XSS 留后门），
 *      refresh 是 httpOnly Cookie，由浏览器自动带、JS 读不到。
 *   ③ SSE —— EventSource 不支持 POST 与自定义头，只能用 fetch + 流式读取。
 */

// ═══════════════ 小工具 ═══════════════

const $ = (s, r = document) => r.querySelector(s);

function h(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return n;
}

const B64 = {
  enc(buf) {
    const b = new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < b.length; i += 0x8000) s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    return btoa(s);
  },
  dec(s) {
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  },
};

let toastTimer = null;
function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (bad ? ' bad' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 5200 : 2600);
}

const fmtBytes = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const fmtTime = (s) => (s ? new Date(s).toLocaleString('zh-CN', { hour12: false }) : '—');

// ═══════════════ 加密层 ═══════════════

const enc = new TextEncoder();
const dec = new TextDecoder();

let publicKey = null;

async function loadPublicKey(force = false) {
  if (publicKey && !force) return publicKey;
  const r = await fetch('/api/crypto/public-key');
  const j = await r.json();
  if (!j?.data?.publicKey) throw new Error('取公钥失败');
  publicKey = await crypto.subtle.importKey(
    'spki', B64.dec(j.data.publicKey),
    { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt']);
  return publicKey;
}

/** 每个请求一把新密钥：一把密钥用整场会话，一次泄露就等于全程可解 */
async function newSession() {
  const key = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt', 'decrypt']);
  const raw = await crypto.subtle.exportKey('raw', key);
  const wrapped = await crypto.subtle.encrypt(
    { name: 'RSA-OAEP' }, await loadPublicKey(), raw);
  return { key, wrapped: B64.enc(wrapped) };
}

async function seal(key, obj) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, enc.encode(JSON.stringify(obj)));
  return { iv: B64.enc(iv), d: B64.enc(ct) };
}

async function open(key, env) {
  // WebCrypto 的密文尾部就是认证标签，与 Node 侧手动拼接的格式一致
  const pt = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: B64.dec(env.iv) }, key, B64.dec(env.d));
  return JSON.parse(dec.decode(pt));
}

// ═══════════════ 令牌与请求 ═══════════════

// access 只放内存：放 localStorage 等于给 XSS 留一扇门。
// 代价是刷新页面要重新登录 —— 用 refresh Cookie 换成新的，用户无感。
let accessToken = null;
let onAuthLost = () => {};

async function refreshToken() {
  const { key, wrapped } = await newSession();
  const meta = await seal(key, { ts: Date.now(), nonce: rnd(), token: null });
  const r = await fetch('/api/auth/refresh', {
    method: 'POST',
    headers: { 'X-Enc-Key': wrapped, 'X-Enc-Meta': B64.enc(enc.encode(JSON.stringify(meta))) },
    credentials: 'same-origin',
  });
  const j = await parseResponse(r, key);
  if (!r.ok) throw new Error(j?.message || '刷新失败');
  accessToken = j.data.accessToken;
  return accessToken;
}

const rnd = () => B64.enc(crypto.getRandomValues(new Uint8Array(12))).replace(/[+/=]/g, '');

async function parseResponse(res, key) {
  const text = await res.text();
  let raw;
  try { raw = JSON.parse(text); } catch { return { message: text.slice(0, 200) }; }
  if (res.headers.get('X-Encrypted') === '1' && raw?.d) {
    try { return await open(key, raw); } catch { return { message: '响应解密失败' }; }
  }
  return raw;
}

/**
 * 加密调用。
 *
 * 收到 401 且体里 expired=true 时自动刷新一次再重试 —— 令牌 30 分钟过期，
 * 不自动续的话用户每半小时被打断一次。只重试一次，避免刷新也失效时死循环。
 */
async function api(method, path, body, opts = {}) {
  const { key, wrapped } = await newSession();
  const meta = await seal(key, { ts: Date.now(), nonce: rnd(), token: accessToken ? `Bearer ${accessToken}` : null });

  const headers = { 'X-Enc-Key': wrapped, 'X-Enc-Meta': B64.enc(enc.encode(JSON.stringify(meta))) };
  const init = { method, headers, credentials: 'same-origin' };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(await seal(key, body));
  }

  const res = await fetch(path, init);
  const data = await parseResponse(res, key);

  if (res.status === 401 && data?.expired && opts.retry !== false) {
    try {
      await refreshToken();
      return api(method, path, body, { retry: false });
    } catch {
      onAuthLost();
      throw new Error('登录已过期');
    }
  }
  if (res.status === 401) { onAuthLost(); throw new Error(data?.message || '未登录'); }
  if (!res.ok || (data && data.code !== undefined && data.code !== 0)) {
    throw new Error(data?.message || `HTTP ${res.status}`);
  }
  return data?.data;
}

/**
 * 打开 SSE 流。
 *
 * 流式响应不走「整体加密」—— 那需要先缓存完整响应，会把流式毁掉。
 * 服务端改为把 AES 密钥交接给 SSE 写入方，每个事件的 data 各自是密文信封。
 */
async function stream(path, body, onEvent, signal) {
  const { key, wrapped } = await newSession();
  const meta = await seal(key, { ts: Date.now(), nonce: rnd(), token: accessToken ? `Bearer ${accessToken}` : null });

  const res = await fetch(path, {
    method: 'POST',
    headers: {
      'X-Enc-Key': wrapped,
      'X-Enc-Meta': B64.enc(enc.encode(JSON.stringify(meta))),
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(await seal(key, body)),
    credentials: 'same-origin',
    signal,
  });

  if (!res.ok || !res.headers.get('content-type')?.includes('text/event-stream')) {
    const err = await parseResponse(res, key);
    if (res.status === 401) onAuthLost();
    throw new Error(err?.message || `HTTP ${res.status}`);
  }

  const reader = res.body.getReader();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let name = 'message';
      const lines = [];
      for (const line of block.split('\n')) {
        if (line.startsWith(':')) continue;            // 保活注释
        if (line.startsWith('event:')) name = line.slice(6).trim();
        else if (line.startsWith('data:')) lines.push(line.slice(5).trim());
      }
      if (!lines.length) continue;
      let payload;
      try {
        const env = JSON.parse(lines.join('\n'));
        payload = env && env.d ? await open(key, env) : env;
      } catch { continue; }                            // 单个事件解不开不该中断整段
      onEvent(name, payload);
    }
  }
}

// ═══════════════ Markdown（最小实现）═══════════════

function md(src) {
  const blocks = [];
  // 先摘出代码块，避免其中的内容被后续规则误伤
  let s = String(src ?? '').replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre><code>${h_esc(code.replace(/\n$/, ''))}</code></pre>`);
    return ` B${blocks.length - 1} `;
  });

  s = h_esc(s);
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/^###\s+(.+)$/gm, '<h3>$1</h3>');
  s = s.replace(/^##\s+(.+)$/gm, '<h2>$1</h2>');
  s = s.replace(/^#\s+(.+)$/gm, '<h1>$1</h1>');

  // 列表：连续以 - / * / 数字. 开头的行
  s = s.replace(/(?:^[ \t]*(?:[-*]|\d+\.)\s+.+\n?)+/gm, (chunk) => {
    const items = chunk.trimEnd().split('\n')
      .map((l) => `<li>${l.replace(/^[ \t]*(?:[-*]|\d+\.)\s+/, '')}</li>`).join('');
    return `<ul>${items}</ul>`;
  });

  s = s.split(/\n{2,}/).map((p) => {
    const t = p.trim();
    if (!t) return '';
    return /^<(h\d|ul|pre|blockquote)/.test(t) ? t : `<p>${t.replace(/\n/g, '<br>')}</p>`;
  }).join('');

  return s.replace(/ B(\d+) /g, (_, i) => blocks[+i]);
}

function h_esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ═══════════════ 状态 ═══════════════

const state = {
  convId: null,
  conversations: [],
  attachments: [],       // {localId, name, size, docId, status, message}
  busy: false,
  controller: null,
  trace: [],             // 本轮 agent 过程，设置里的「系统」页会读它
};

// ═══════════════ 登录 ═══════════════

async function boot() {
  $('#loginForm').addEventListener('submit', doLogin);
  $('#logout').addEventListener('click', doLogout);
  $('#newChat').addEventListener('click', () => openConversation(null));
  $('#openSettings').addEventListener('click', () => openSettings('cache'));
  $('#closeSettings').addEventListener('click', () => { $('#settings').hidden = true; });
  $('#toggleSide').addEventListener('click', toggleSide);

  for (const b of $('#setTabs').children) {
    b.addEventListener('click', () => {
      for (const x of $('#setTabs').children) x.classList.toggle('on', x === b);
      renderSettings(b.dataset.tab);
    });
  }

  $('#strategy').addEventListener('change', () => localStorage.setItem('kb.strategy', $('#strategy').value));

  wireComposer();
  wireAttachments();

  // 走加密调用而不是裸 fetch：它是 /api/ 下的接口，而加密过滤器对 /api/**
  // 一律要求密文 —— 裸 fetch 会被挡在 400「请求未加密」上（实测过）。
  // api() 在没有令牌时发 token:null，正合适。
  const cfg = await api('GET', '/api/auth/config').catch(() => null);
  $('#gateSub').textContent = cfg?.hasUser === false
    ? '库里还没有账号，请先用初始化脚本创建'
    : '请登录';

  // 刷新页面后 access 已丢，试着用 refresh Cookie 换回来，换到就直接进主界面
  try {
    await refreshToken();
    await enter();
  } catch {
    $('#username').focus();
  }
}

async function doLogin(e) {
  e.preventDefault();
  const btn = $('#loginBtn');
  const err = $('#loginErr');
  btn.disabled = true;
  err.hidden = true;
  try {
    const data = await api('POST', '/api/auth/login', {
      username: $('#username').value.trim(),
      password: $('#password').value,
    });
    accessToken = data.accessToken;
    $('#password').value = '';
    await enter();
  } catch (e2) {
    err.textContent = e2.message;
    err.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

async function doLogout() {
  try { await api('POST', '/api/auth/logout', {}); } catch { /* 服务端失败也要清本地 */ }
  accessToken = null;
  onAuthLost();
}

onAuthLost = () => {
  accessToken = null;
  $('#app').hidden = true;
  $('#gate').hidden = false;
  $('#gateSub').textContent = '登录已失效，请重新登录';
  $('#username').focus();
};

async function enter() {
  $('#gate').hidden = true;
  $('#app').hidden = false;
  $('#strategy').value = localStorage.getItem('kb.strategy') || 'agent';
  try {
    const me = await api('GET', '/api/auth/me');
    $('#whoami').textContent = me.username;
  } catch { /* 拿不到用户名不影响使用 */ }
  await loadConversations();
  openConversation(null);
}

// ═══════════════ 会话 ═══════════════

async function loadConversations() {
  try {
    const d = await api('GET', '/api/chat/conversations?limit=100');
    state.conversations = d.conversations || [];
    renderConversations();
  } catch (e) { toast(e.message, true); }
}

function renderConversations() {
  const box = $('#convList');
  box.replaceChildren();
  if (!state.conversations.length) {
    box.append(h('p', { class: 'muted', text: '还没有对话' }));
    return;
  }
  for (const c of state.conversations) {
    const row = h('div', { class: 'conv' + (c.id === state.convId ? ' on' : ''), onClick: () => openConversation(c.id) },
      h('span', { class: 't', text: c.title || '未命名' }),
      h('span', {
        class: 'x', text: '✕', title: '删除',
        onClick: (e) => { e.stopPropagation(); removeConversation(c.id); },
      }));
    box.append(row);
  }
}

async function removeConversation(id) {
  if (!confirm('删除这个对话？')) return;
  try {
    await api('DELETE', `/api/chat/conversations/${id}`);
    if (state.convId === id) openConversation(null);
    await loadConversations();
  } catch (e) { toast(e.message, true); }
}

async function openConversation(id) {
  if (state.busy) { toast('正在回答，稍等'); return; }
  state.convId = id;
  state.trace = [];
  $('#messages').replaceChildren();
  $('#convTitle').textContent = '新对话';
  closeSide();

  if (!id) {
    renderEmpty();
    renderConversations();
    return;
  }
  try {
    const d = await api('GET', `/api/chat/conversations/${id}`);
    $('#convTitle').textContent = d.conversation?.title || '对话';
    const box = $('#messages');
    box.replaceChildren();
    for (const m of d.messages || []) box.append(messageNode(m.role, m.content, m.sources));
    scrollDown();
  } catch (e) { toast(e.message, true); }
  renderConversations();
}

function renderEmpty() {
  $('#messages').append(h('div', { class: 'empty' },
    h('b', { text: '知识库助手' }),
    h('div', { text: '直接提问即可。要加入资料，点左下角 📎 选择文件。' })));
}

// ═══════════════ 消息渲染 ═══════════════

function messageNode(role, content, sources, opts = {}) {
  const isUser = role === 'user';
  const wrap = h('div', { class: 'msg ' + (isUser ? 'user' : 'assistant') });
  const body = h('div', { class: 'body' });

  body.append(h('div', { class: 'who', text: isUser ? '我' : '助手' }));
  if (opts.trace) body.append(opts.trace);
  if (opts.think) body.append(opts.think);

  const text = h('div', { class: 'text' });
  text.innerHTML = md(content || '');
  body.append(text);
  if (opts.cursor) text.classList.add('cursor');

  const src = h('div', { class: 'src' });
  body.append(src);
  if (sources?.length) {
    for (const s of sources) {
      src.append(h('span', { text: `${s.docName || '?'} #${s.seq ?? '?'}`, title: `距离 ${s.distance ?? '—'}` }));
    }
  }
  wrap.append(body);
  wrap._text = text;
  wrap._src = src;
  return wrap;
}

function scrollDown() {
  const m = $('#messages');
  m.scrollTop = m.scrollHeight;
}

// ═══════════════ 发送 ═══════════════

function wireComposer() {
  const input = $('#input');
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 180) + 'px';
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $('#composer').requestSubmit();
    }
  });
  $('#composer').addEventListener('submit', (e) => { e.preventDefault(); send(); });
}

async function send() {
  if (state.busy) return;
  const input = $('#input');
  const question = input.value.trim();
  if (!question) return;
  if (state.attachments.some((a) => a.status === 'uploading' || a.status === 'indexing')) {
    toast('还有附件在处理中，稍等一下', true);
    return;
  }

  input.value = '';
  input.style.height = 'auto';
  state.busy = true;
  $('#sendBtn').disabled = true;

  const box = $('#messages');
  if (box.querySelector('.empty')) box.replaceChildren();
  box.append(messageNode('user', question));

  // 本轮的过程与思考各自独立折叠，与正文严格分开：它们是过程不是结论
  const traceOl = h('ol');
  const trace = h('details', { class: 'trace' },
    h('summary', { text: '处理过程' }), traceOl);
  const thinkBody = h('div', { class: 't' });
  const think = h('details', { class: 'think' },
    h('summary', { text: '思考中…' }), thinkBody);
  think.hidden = true;

  const node = messageNode('assistant', '', null, { trace, think, cursor: true });
  box.append(node);
  scrollDown();

  const t0 = Date.now();
  let answer = '';
  let thinkText = '';
  let sources = [];
  let lastPaint = 0;

  const stamp = () => `+${((Date.now() - t0) / 1000).toFixed(1)}s`;
  const addTrace = (label, detail) => {
    state.trace.push({ at: Date.now() - t0, label, detail });
    traceOl.append(h('li', {}, `${label} `, h('span', { class: 'ms', text: `(${stamp()})` }),
      detail ? ` — ${detail}` : ''));
    trace.open = false;
  };

  try {
    await stream('/api/chat/stream', {
      convId: state.convId,
      question,
      strategy: $('#strategy').value,
    }, (name, d) => {
      switch (name) {
        case 'meta':
          if (d.convId && d.convId !== state.convId) {
            state.convId = d.convId;
            $('#convTitle').textContent = question.slice(0, 20);
          }
          break;
        case 'plan':
          addTrace(`规划（第 ${d.round} 轮）`, (d.queries || []).join(' · '));
          break;
        case 'retrieve':
          addTrace(`检索「${d.query}」`, `命中 ${d.hits} 段`);
          break;
        case 'assess':
          addTrace(`评估`, d.enough ? '资料足够' : `不足，继续找：${d.missing || ''}`);
          break;
        case 'thinking':
          think.hidden = false;
          thinkText += d.t || '';
          thinkBody.textContent = thinkText;
          think.open = false;
          break;
        case 'answer':
          answer += d.t || '';
          // 逐 token 重渲染 markdown 是 O(n²)，节流到 ~60ms 一次，
          // 观感上仍是逐字出现，但不会把主线程占满
          if (Date.now() - lastPaint > 60) {
            lastPaint = Date.now();
            node._text.innerHTML = md(answer);
            scrollDown();
          }
          break;
        case 'done':
          sources = d.sources || [];
          break;
        case 'error':
          addTrace('出错', d.message || '');
          break;
      }
    }, state.controller?.signal);

    node._text.innerHTML = md(answer || '（没有收到内容）');
    node._text.classList.remove('cursor');
    think.querySelector('summary').textContent = `思考过程（${thinkText.length} 字）`;
    if (!traceOl.childElementCount) trace.hidden = true;

    const srcBox = node._src;
    srcBox.replaceChildren();
    for (const s of sources || []) {
      srcBox.append(h('span', {
        text: `${s.docName} #${s.seq}`,
        title: `${s.preview || ''}\n距离 ${s.distance ?? '—'}`,
      }));
    }
  } catch (e) {
    node._text.classList.remove('cursor');
    node._text.innerHTML = md(`**出错了**：${e.message}`);
    toast(e.message, true);
  } finally {
    state.busy = false;
    $('#sendBtn').disabled = false;
    scrollDown();
    loadConversations();
  }
}

// ═══════════════ 附件 ═══════════════

function wireAttachments() {
  $('#attachBtn').addEventListener('click', () => $('#fileInput').click());
  $('#fileInput').addEventListener('change', (e) => {
    for (const f of e.target.files) addAttachment(f);
    e.target.value = '';
  });
  // 拖拽进来也当作附件
  const main = $('#main');
  main.addEventListener('dragover', (e) => { e.preventDefault(); });
  main.addEventListener('drop', (e) => {
    e.preventDefault();
    for (const f of e.dataTransfer.files) addAttachment(f);
  });
}

function addAttachment(file) {
  const a = { localId: rnd(), name: file.name, size: file.size, status: 'uploading', file };
  state.attachments.push(a);
  renderAttachments();
  uploadAttachment(a);
}

async function uploadAttachment(a) {
  const fd = new FormData();
  fd.append('file', a.file);
  try {
    // multipart 走不了加密（需要分片方案），靠 HTTPS 与 Authorization 头保护。
    // 这是已知缺口，README 里记着。
    const res = await fetch('/api/docs/upload', {
      method: 'POST',
      headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
      body: fd,
      credentials: 'same-origin',
    });
    const j = await res.json();
    if (!res.ok || j.code !== 0) throw new Error(j.message || `HTTP ${res.status}`);
    a.docId = j.data.id;
    a.status = 'indexing';
    renderAttachments();
    await indexDoc(a);
  } catch (e) {
    a.status = 'error';
    a.message = e.message;
    renderAttachments();
    toast(`「${a.name}」上传失败：${e.message}`, true);
  }
}

async function indexDoc(a) {
  try {
    const t = await api('POST', `/api/docs/${a.docId}/index`);
    for (let i = 0; i < 200; i++) {
      await new Promise((r) => setTimeout(r, 900));
      const st = await api('GET', `/api/docs/tasks/${t.taskId}`);
      if (st.status === 'done') {
        a.status = 'done';
        a.chunks = st.result?.chunks ?? st.progress;
        renderAttachments();
        return;
      }
      if (st.status === 'error') throw new Error(st.message || '索引失败');
      a.message = `${st.progress ?? 0}/${st.total ?? '?'}`;
      renderAttachments();
    }
    throw new Error('索引超时');
  } catch (e) {
    a.status = 'error';
    a.message = e.message;
    renderAttachments();
    toast(`「${a.name}」建索引失败：${e.message}`, true);
  }
}

function renderAttachments() {
  const box = $('#attachments');
  box.replaceChildren();
  if (!state.attachments.length) { box.hidden = true; return; }
  box.hidden = false;
  for (const a of state.attachments) {
    const label = { uploading: '上传中', indexing: '建索引', done: '就绪', error: '失败' }[a.status];
    const extra = a.status === 'done' ? `${a.chunks ?? '?'} 块`
      : a.status === 'error' ? a.message
        : a.message || '';
    box.append(h('div', { class: 'att ' + a.status },
      h('span', { text: a.name, title: `${fmtBytes(a.size)}` }),
      h('span', { class: 'st', text: extra ? `${label} · ${extra}` : label }),
      h('span', {
        class: 'x', text: '✕', title: '移除',
        onClick: () => {
          state.attachments = state.attachments.filter((x) => x !== a);
          renderAttachments();
        },
      })));
  }
}

// ═══════════════ 设置 ═══════════════

function openSettings(tab) {
  $('#settings').hidden = false;
  for (const b of $('#setTabs').children) b.classList.toggle('on', b.dataset.tab === tab);
  renderSettings(tab);
}

async function renderSettings(tab) {
  const body = $('#setBody');
  body.replaceChildren(h('p', { class: 'muted', text: '加载中…' }));
  try {
    if (tab === 'cache') await renderCache(body);
    else if (tab === 'docs') await renderDocs(body);
    else if (tab === 'model') await renderModel(body);
    else renderSystem(body);
  } catch (e) {
    body.replaceChildren(h('p', { class: 'muted', text: `加载失败：${e.message}` }));
  }
}

async function renderCache(body) {
  const s = await api('GET', '/api/cache/stats');
  const nodes = [];
  const names = { embeddings: '向量缓存', parses: '解析缓存', answers: '回答缓存' };
  const tbl = h('table', { class: 'grid' },
    h('tr', {}, h('th', { text: '层' }), h('th', { text: '条目' }), h('th', { text: '命中' }), h('th', { text: '最近使用' })));
  for (const [kind, row] of Object.entries(s)) {
    tbl.append(h('tr', {},
      h('td', { text: names[kind] || kind }),
      h('td', { class: 'num', text: row.entries ?? '—' }),
      h('td', { class: 'num', text: row.hits ?? '—' }),
      h('td', { text: row.last_used ? fmtTime(row.last_used) : '—' })));
  }
  nodes.push(tbl);
  nodes.push(h('div', { class: 'row' },
    h('span', { class: 'grow muted', text: '缓存键里含所有影响结果的参数，改了输入就自动不命中 —— 清空只释放空间，不影响正确性。' }),
    h('button', { text: '清空全部', onClick: async () => {
      if (!confirm('清空全部缓存？')) return;
      const r = await api('DELETE', '/api/cache');
      toast(`已清空：${Object.entries(r).map(([k, v]) => `${k} ${v}`).join('，')}`);
      renderSettings('cache');
    } })));
  body.replaceChildren(h('h3', { text: '三层缓存' }), ...nodes);
}

async function renderDocs(body) {
  const d = await api('GET', '/api/docs');
  const stale = new Set(d.stale || []);
  const tbl = h('table', { class: 'grid' },
    h('tr', {}, h('th', { text: '文档' }), h('th', { text: '状态' }), h('th', { text: '块' }), h('th', {})));
  for (const doc of d.docs || []) {
    const ok = doc.status === 'indexed' && !stale.has(doc.id);
    tbl.append(h('tr', {},
      h('td', {}, h('div', { text: doc.name }), h('div', { class: 'muted', text: fmtBytes(doc.bytes) })),
      h('td', {}, h('span', {
        class: 'pill ' + (ok ? 'ok' : stale.has(doc.id) ? 'warn' : ''),
        text: stale.has(doc.id) ? '向量模型已变' : doc.status,
      })),
      h('td', { class: 'num', text: doc.chunkCount ?? 0 }),
      h('td', {}, h('button', { class: 'link', text: '删除', onClick: async () => {
        if (!confirm(`删除「${doc.name}」？`)) return;
        await api('DELETE', `/api/docs/${doc.id}`);
        toast('已删除');
        renderSettings('docs');
      } }))));
  }
  body.replaceChildren(
    h('h3', { text: `文档（${d.count || 0}）` }),
    d.count ? tbl : h('p', { class: 'muted', text: '还没有文档。回到对话页点 📎 添加。' }));
}

async function renderModel(body) {
  const d = await api('GET', '/api/provider/list');
  const nodes = [];
  for (const p of d.providers || []) {
    const row = h('div', { class: 'row' },
      h('div', { class: 'grow' },
        h('div', {}, h('strong', { text: p.label || p.id }),
          p.id === d.defaults?.chat?.split('/')[0] ? h('span', { class: 'pill ok', text: ' 默认' }) : ''),
        h('div', { class: 'muted', text: `${p.kind} · ${p.base}` })),
      h('button', { text: '探测', onClick: async (e) => {
        e.target.disabled = true;
        try {
          const r = await api('GET', `/api/provider/${p.id}/probe`);
          toast(r.ok ? `${p.id} 可用，${r.count} 个模型` : `${p.id} 不可用：${r.error}`, !r.ok);
          e.target.parentElement.append(h('div', { class: 'muted', text: (r.models || []).join('、') }));
        } catch (e2) { toast(e2.message, true); }
        e.target.disabled = false;
      } }));
    nodes.push(row);
  }
  nodes.push(h('h3', { text: '默认模型' }));
  nodes.push(h('dl', { class: 'kv' },
    h('dt', { text: '对话' }), h('dd', { text: d.defaults?.chat || '—' }),
    h('dt', { text: '向量' }), h('dd', { text: d.defaults?.embed || '—' })));
  body.replaceChildren(h('h3', { text: '提供方' }), ...nodes);
}

function renderSystem(body) {
  const nodes = [];
  nodes.push(h('div', { class: 'row' },
    h('button', { text: '健康检查', onClick: async (e) => {
      e.target.disabled = true;
      try {
        const r = await api('GET', '/api/health');
        const lines = Object.entries(r).map(([k, v]) =>
          `${k}: ${typeof v === 'object' ? (v.ok === false ? `不可用 — ${v.error}` : (v.ok ? '正常' : JSON.stringify(v))) : v}`);
        body.querySelector('#healthOut').textContent = lines.join('\n');
      } catch (e2) { body.querySelector('#healthOut').textContent = e2.message; }
      e.target.disabled = false;
    } })));
  nodes.push(h('pre', { class: 'muted', id: 'healthOut', text: '点上面的按钮查看依赖状态' }));

  nodes.push(h('h3', { text: '本轮 agent 过程' }));
  if (!state.trace.length) {
    nodes.push(h('p', { class: 'muted', text: '本次会话还没有问答记录。问一句再回来这里看。' }));
  } else {
    const ol = h('ol', { class: 'muted' });
    for (const t of state.trace) ol.append(h('li', { text: `+${(t.at / 1000).toFixed(1)}s ${t.label}${t.detail ? ' — ' + t.detail : ''}` }));
    nodes.push(ol);
  }

  const cust = JSON.parse(localStorage.getItem('kb.recentTrace') || 'null');
  if (cust) nodes.push(h('h3', { text: '上一轮（存档）' }), h('pre', { class: 'muted', text: cust }));

  body.replaceChildren(
    h('h3', { text: '系统' }), ...nodes,
    h('p', { class: 'muted', text: '提示：设置里的内容全部按需拉取，关闭抽屉即停止。' }));
}

// ═══════════════ 窄屏侧栏 ═══════════════

function toggleSide() {
  $('#sidebar').classList.toggle('open');
  $('#scrim').hidden = !$('#sidebar').classList.contains('open');
}
function closeSide() {
  $('#sidebar').classList.remove('open');
  $('#scrim').hidden = true;
}

$('#scrim')?.addEventListener('click', closeSide);

// 每轮结束后把过程存一份，供「上一轮」查看
setInterval(() => {
  if (state.trace.length) {
    localStorage.setItem('kb.recentTrace', JSON.stringify(
      state.trace.map((t) => `+${(t.at / 1000).toFixed(1)}s ${t.label}${t.detail ? ' — ' + t.detail : ''}`).join('\n')));
  }
}, 3000);

boot();
