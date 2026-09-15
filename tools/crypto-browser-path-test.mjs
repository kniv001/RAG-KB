/**
 * 直接验证「浏览器那条加密路径」。
 *
 * app.js 的加密层用的是 WebCrypto（crypto.subtle）。Node 20+ 的 crypto.subtle
 * 就是同一套 API，所以把 app.js 里那段源码原样抽出来、在 Node 里跑一遍，
 * 打的是真实服务端 —— 这验证的是**将要在浏览器里执行的那段代码本身**，
 * 不是它的副本。
 *
 * 为什么值得单独测：加密一旦有一处不匹配（OAEP 的 hash、GCM 的标签位置、
 * 密钥的导出格式），表现是整站白屏，而在浏览器里排查这个问题极其痛苦。
 * 在这里失败，错误是具体的。
 */
import fs from 'node:fs';

const APP = 'rag-kb-web/src/main/resources/static/assets/app.js';
const src = fs.readFileSync(APP, 'utf8');

/** 按标记切出源码片段 —— 不做任何改写，跑的就是文件里的原文 */
function slice(startMarker, endMarker, name) {
  const a = src.indexOf(startMarker);
  const b = src.indexOf(endMarker);
  if (a < 0 || b < 0 || b <= a) throw new Error(`切不出「${name}」：找不到标记`);
  return src.slice(a, b);
}

const b64Block = slice('const B64 = {', 'let toastTimer', 'B64 工具');
const cryptoBlock = slice('// ═══════════════ 加密层', '// ═══════════════ 令牌与请求', '加密层');

const factory = new Function(
  `${b64Block}\n${cryptoBlock}\nreturn { B64, loadPublicKey, newSession, seal, open };`);
const { B64, loadPublicKey, newSession, seal, open } = factory();

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const origFetch = globalThis.fetch;
// app.js 里的路径是相对根路径的，这里补上主机
globalThis.fetch = (u, o) => origFetch(String(u).startsWith('http') ? u : BASE + u, o);

let pass = 0, fail = 0;
const ok = (n, c, e = '') => {
  if (c) { pass++; console.log(`  ✅ ${n}${e ? ' — ' + e : ''}`); }
  else { fail++; console.log(`  ❌ ${n}${e ? ' — ' + e : ''}`); }
};

console.log('\n=== 用的是 app.js 里的原文吗 ===');
ok('切到 B64 工具', b64Block.includes('b64') || b64Block.includes('enc(buf)'));
ok('切到加密层', cryptoBlock.includes('RSA-OAEP') && cryptoBlock.includes('AES-GCM'));
ok('  含 SHA-256 指定（与 Java 端 OAEPParameterSpec 必须一致）', cryptoBlock.includes("hash: 'SHA-256'"));
console.log(`  片段长度：B64 ${b64Block.length} 字符，加密层 ${cryptoBlock.length} 字符`);

console.log('\n=== 1. 取公钥并封装 AES 密钥 ===');
let session;
try {
  session = await newSession();
  ok('newSession 成功（RSA-OAEP 封装 32 字节 AES 密钥）', !!session.wrapped);
} catch (e) {
  fail++; console.log(`  ❌ newSession 失败：${e.message}`);
}

if (session) {
  console.log('\n=== 2. 用浏览器那套加密去登录真实服务端 ===');
  const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
  const meta = await seal(session.key, { ts: Date.now(), nonce: 'test-' + Date.now(), token: null });

  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: {
      'X-Enc-Key': session.wrapped,
      'X-Enc-Meta': B64.enc(new TextEncoder().encode(JSON.stringify(meta))),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(await seal(session.key, { username: cred.user, password: cred.password })),
  });

  ok('服务端接受了 WebCrypto 封装的密钥', res.status === 200, `HTTP ${res.status}`);
  ok('  响应标记为已加密', res.headers.get('X-Encrypted') === '1');

  const raw = JSON.parse(await res.text());
  let body = null;
  try { body = await open(session.key, raw); } catch (e) { console.log(`  解密异常：${e.message}`); }
  ok('用 WebCrypto 解开了服务端响应', !!body, body ? `用户 ${body.data?.user?.username}` : '');
  ok('  拿到 accessToken', !!body?.data?.accessToken);

  if (body?.data?.accessToken) {
    console.log('\n=== 3. 带令牌取一份真实数据 ===');
    const s2 = await newSession();
    const m2 = await seal(s2.key, { ts: Date.now(), nonce: 'n' + Date.now(), token: `Bearer ${body.data.accessToken}` });
    const r2 = await fetch('/api/cache/stats', {
      headers: {
        'X-Enc-Key': s2.wrapped,
        'X-Enc-Meta': B64.enc(new TextEncoder().encode(JSON.stringify(m2))),
      },
    });
    const b2 = await open(s2.key, JSON.parse(await r2.text()));
    ok('GET /api/cache/stats 走通', b2?.code === 0, Object.keys(b2?.data || {}).join('、'));

    console.log('\n=== 4. 二进制往返（中文 + emoji，验证 UTF-8 编解码）===');
    const tricky = { s: '三层缓存 · 向量缓存 ✓ 🎯', n: 12345, arr: [1, 2, 3], nil: null };
    const round = await open(session.key, await seal(session.key, tricky));
    ok('中文与 emoji 原样往返', round.s === tricky.s, round.s);
    ok('数字/数组/null 保持类型',
       round.n === 12345 && Array.isArray(round.arr) && round.nil === null);
  }
}

console.log(`\n${'─'.repeat(50)}`);
console.log(`通过 ${pass} 项，失败 ${fail} 项`);
if (fail === 0) {
  console.log('\n结论：app.js 的加密层与 Java 端协议完全对得上 ——');
  console.log('      浏览器里跑的是同一段代码，不存在「Node 能通、浏览器不通」的隐患。');
}
process.exit(fail ? 1 : 0);
