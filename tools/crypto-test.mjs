/**
 * 端到端加密链路测试（模拟浏览器 WebCrypto 语义）。用完即删。
 *
 * 关键：Node 的 publicEncrypt({oaepHash:'sha256'}) 与浏览器 crypto.subtle RSA-OAEP(hash:SHA-256)
 * 一样，摘要与 MGF1 都用 SHA-256 —— 正好验证 Java 侧显式构造的 OAEPParameterSpec 是否对齐。
 */
import crypto from 'node:crypto';
import fs from 'node:fs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
// 凭据与其它工具用同一份，别再各自硬编码 —— 之前这里写的是早期开发的占位账号
// dev/dev-only-change-me，双令牌改造之后它登不上，于是两条用例一直红着。
const CRED = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const USER = process.env.KB_ADMIN_USER || CRED.user;
const PASS = process.env.KB_ADMIN_PASSWORD || CRED.password;

let pass = 0, fail = 0;
const check = (label, cond, extra = '') => {
  if (cond) { pass++; console.log(`  [PASS] ${label} ${extra}`); }
  else { fail++; console.log(`  [FAIL] ${label} ${extra}`); }
};

// ---------- 1) 取公钥 ----------
const pkResp = await fetch(`${BASE}/api/crypto/public-key`);
const pk = await pkResp.json();
console.log(`公钥接口 -> ${pkResp.status}`);
console.log(`  algorithm=${pk.data?.algorithm} keySize=${pk.data?.keySize} fingerprint=${pk.data?.fingerprint}`);
const pubKey = crypto.createPublicKey({
  key: Buffer.from(pk.data.publicKey, 'base64'), format: 'der', type: 'spki',
});
check('公钥可被 Node 正确解析', pubKey.asymmetricKeyType === 'rsa', `type=${pubKey.asymmetricKeyType}`);

// ---------- 原语 ----------
function encBody(aesKey, obj) {
  const iv = crypto.randomBytes(12);
  const c = crypto.createCipheriv('aes-256-gcm', aesKey, iv);
  const ct = Buffer.concat([c.update(JSON.stringify(obj), 'utf8'), c.final(), c.getAuthTag()]);
  return { iv: iv.toString('base64'), d: ct.toString('base64') };
}
function decBody(aesKey, env) {
  const buf = Buffer.from(env.d, 'base64');
  const tag = buf.subarray(buf.length - 16);
  const data = buf.subarray(0, buf.length - 16);
  const d = crypto.createDecipheriv('aes-256-gcm', aesKey, Buffer.from(env.iv, 'base64'));
  d.setAuthTag(tag);
  return Buffer.concat([d.update(data), d.final()]).toString('utf8');
}

// ---------- 登录取令牌 ----------
// 双令牌之后认证走 Bearer，Basic 时代的老写法在这里已经登不上了。
// 登录本身也要走加密通道 —— 明文送密码等于这层加密白做。
let TOKEN = null;
{
  const aesKey = crypto.randomBytes(32);
  const wrapped = crypto.publicEncrypt(
    { key: pubKey, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256' }, aesKey);
  const meta = encBody(aesKey, { ts: Date.now(), nonce: crypto.randomBytes(12).toString('base64url'), token: null });
  const resp = await fetch(`${BASE}/api/auth/login`, {
    method: 'POST',
    headers: {
      'X-Enc-Key': wrapped.toString('base64'),
      'X-Enc-Meta': Buffer.from(JSON.stringify(meta)).toString('base64'),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(encBody(aesKey, { username: USER, password: PASS })),
  });
  const raw = JSON.parse(await resp.text());
  const body = resp.headers.get('X-Encrypted') === '1' && raw?.d ? JSON.parse(decBody(aesKey, raw)) : raw;
  TOKEN = body?.data?.accessToken ? `Bearer ${body.data.accessToken}` : null;
  console.log(`登录 -> ${resp.status}  ${TOKEN ? '已取得令牌' : '未取得令牌：' + (body?.message || '')}`);
}

async function call(method, path, payload, opts = {}) {
  const aesKey = crypto.randomBytes(32);
  const wrapped = crypto.publicEncrypt(
    { key: pubKey, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256' }, aesKey);

  const metaObj = {
    ts: opts.ts ?? Date.now(),
    nonce: opts.nonce ?? crypto.randomBytes(12).toString('base64url'),
    token: opts.noToken ? null : TOKEN,
  };
  const headers = {
    'X-Enc-Key': wrapped.toString('base64'),
    'X-Enc-Meta': Buffer.from(JSON.stringify(encBody(aesKey, metaObj))).toString('base64'),
  };
  const init = { method, headers };
  if (payload !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(encBody(aesKey, payload));
  }

  const r = await fetch(`${BASE}${path}`, init);
  const text = await r.text();
  let parsed = null;
  try { parsed = JSON.parse(text); } catch { /* 非 JSON */ }

  const encrypted = r.headers.get('X-Encrypted') === '1';
  let body = parsed ?? text;
  let decrypted = false;
  if (encrypted && parsed?.d) {
    body = JSON.parse(decBody(aesKey, parsed));
    decrypted = true;
  }
  return { status: r.status, body, encrypted, decrypted, raw: text };
}

// ---------- 2) 未加密请求应被拒 ----------
const plainResp = await fetch(`${BASE}/api/crypto/selftest`);
check('未加密请求被拒绝', plainResp.status === 400, `-> ${plainResp.status}`);

// ---------- 3) 加密 GET + Basic 认证 ----------
const r1 = await call('GET', '/api/crypto/selftest');
check('加密 GET 带认证 -> 200', r1.status === 200, `-> ${r1.status}`);
check('响应标记为加密且成功解出', r1.encrypted && r1.decrypted,
  `encrypted=${r1.encrypted} decrypted=${r1.decrypted}`);
check('解密后自检结果 aes/rsa 均为 true',
  r1.body?.data?.aes === true && r1.body?.data?.rsa === true,
  JSON.stringify(r1.body?.data));

// ---------- 4) 无令牌 -> 401（且响应同样被加密）----------
const r2 = await call('GET', '/api/crypto/selftest', undefined, { noToken: true });
check('无令牌 -> 401', r2.status === 401, `-> ${r2.status}`);
check('401 响应也被加密（不泄露内部结构）', r2.encrypted, `encrypted=${r2.encrypted}`);

// ---------- 5) 加密 POST 带请求体（验证体通道）----------
const payload = { hello: 'world', n: 42, zh: '中文也要能过' };
const r3 = await call('POST', '/api/ping', payload);
check('加密 POST -> 200', r3.status === 200, `-> ${r3.status} ${JSON.stringify(r3.body).slice(0, 100)}`);
check('请求体被正确解密（服务端回显逐字段一致）',
  r3.body?.data?.echo?.hello === 'world'
  && r3.body?.data?.echo?.n === 42
  && r3.body?.data?.echo?.zh === '中文也要能过',
  JSON.stringify(r3.body?.data?.echo));

// ---------- 6) 防重放 ----------
const fixedNonce = 'replay-test-nonce-001';
await call('GET', '/api/crypto/selftest', undefined, { nonce: fixedNonce });
const r4 = await call('GET', '/api/crypto/selftest', undefined, { nonce: fixedNonce });
check('重复 nonce 被拒绝', r4.status === 400 && /重复/.test(r4.body?.message || ''),
  `-> ${r4.status} ${r4.body?.message}`);

// ---------- 7) 时间戳过期 ----------
const r5 = await call('GET', '/api/crypto/selftest', undefined, { ts: Date.now() - 3600_000 });
check('过期时间戳被拒绝', r5.status === 400 && /过期/.test(r5.body?.message || ''),
  `-> ${r5.status} ${r5.body?.message}`);

// ---------- 8) 篡改密文应被 GCM 认证标签拦下 ----------
const tampered = await (async () => {
  const aesKey = crypto.randomBytes(32);
  const wrapped = crypto.publicEncrypt(
    { key: pubKey, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256' }, aesKey);
  const meta = encBody(aesKey, { ts: Date.now(), nonce: crypto.randomBytes(8).toString('hex'), token: null });
  const m = Buffer.from(meta.d, 'base64'); m[3] ^= 0xff;        // 篡改一个字节
  const r = await fetch(`${BASE}/api/crypto/selftest`, {
    headers: {
      'X-Enc-Key': wrapped.toString('base64'),
      'X-Enc-Meta': Buffer.from(JSON.stringify({ iv: meta.iv, d: m.toString('base64') })).toString('base64'),
    },
  });
  return { status: r.status, text: await r.text() };
})();
check('篡改密文被拒（GCM 完整性校验生效）', tampered.status === 400,
  `-> ${tampered.status} ${tampered.text.slice(0, 90)}`);

console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
