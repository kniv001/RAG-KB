/**
 * 入库形状体检：**一段带标题+表格+代码的 HTML，走真实入库路径之后变成什么样的块**。
 *
 * 为什么现在做：语料里上传的 HTML 是 **0 篇**（44 篇里 43 篇 .md、1 篇 .pdf），
 * 所以上游清单那十一条「没有靶子」。与其等真内容，不如**自己造一个靶子** ——
 * 看现有解析器会把什么送进索引，这直接决定「要不要预代码处理」。
 *
 * 注意：**所有请求都要走加密信封**（`缺少 X-Enc-Key 头` 就是没走），所以得用
 * `kb-client` 的 `call`，上传则照抄 `encrypted-upload-test.mjs` 的封装。
 *
 * 用法：node tools/ingest-shape-probe.mjs
 */
import fs from 'node:fs';
import crypto from 'node:crypto';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;
const pub = await c.fetchPublicKey();

function newSession() {
  const key = crypto.randomBytes(32);
  const wrapped = crypto.publicEncrypt(
    { key: crypto.createPublicKey({ key: Buffer.from(pub.publicKey, 'base64'), format: 'der', type: 'spki' }),
      padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256' }, key);
  return { key, wrapped: wrapped.toString('base64') };
}
function seal(key, obj) {
  const iv = crypto.randomBytes(12);
  const ci = crypto.createCipheriv('aes-256-gcm', key, iv);
  const ct = Buffer.concat([ci.update(JSON.stringify(obj), 'utf8'), ci.final(), ci.getAuthTag()]);
  return { iv: iv.toString('base64'), d: ct.toString('base64') };
}
function open(key, env) {
  const buf = Buffer.from(env.d, 'base64');
  const d = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(env.iv, 'base64'));
  d.setAuthTag(buf.subarray(buf.length - 16));
  return JSON.parse(Buffer.concat([d.update(buf.subarray(0, buf.length - 16)), d.final()]).toString('utf8'));
}
async function upload(content, filename) {
  const { key, wrapped } = newSession();
  const iv = crypto.randomBytes(12);
  const ci = crypto.createCipheriv('aes-256-gcm', key, iv);
  const ct = Buffer.concat([ci.update(Buffer.from(content, 'utf8')), ci.final(), ci.getAuthTag()]);
  const meta = seal(key, {
    ts: Date.now(), nonce: crypto.randomBytes(9).toString('base64url'),
    token: TOKEN, iv: iv.toString('base64'), name: filename,
    size: Buffer.byteLength(content, 'utf8'),
  });
  const res = await fetch(`${BASE}/api/docs/upload-encrypted`, {
    method: 'POST',
    headers: { 'X-Enc-Key': wrapped, 'X-Enc-Meta': Buffer.from(JSON.stringify(meta)).toString('base64'),
               'Content-Type': 'application/octet-stream' },
    body: ct,
  });
  const parsed = JSON.parse(await res.text());
  return res.headers.get('X-Encrypted') === '1' && parsed?.d ? open(key, parsed) : parsed;
}

const html = fs.readFileSync('tools/_probe-page.html', 'utf8');
const up = await upload(html, '_探针页-限流方案选型速查.html');
console.log('上传:', JSON.stringify(up).slice(0, 200), '\n');

const docs = (await c.call('GET', '/api/docs', undefined, { token: TOKEN })).body.data.docs || [];
const doc = docs.filter(d => d.name.includes('_探针页')).sort((a, b) => (b.id > a.id ? 1 : -1))[0];
if (!doc) { console.log('没找到刚上传的文档'); process.exit(1); }
console.log(`docId=${doc.id}  状态=${doc.indexState}  块数=${doc.chunkCount}\n`);

if (doc.indexState !== 'indexed') {
  const t = await c.call('POST', `/api/docs/${doc.id}/index`, undefined, { token: TOKEN });
  console.log('触发建索引:', JSON.stringify(t.body).slice(0, 120));
  for (let i = 0; i < 90; i++) {
    await new Promise(r => setTimeout(r, 2000));
    const d2 = (await c.call('GET', '/api/docs', undefined, { token: TOKEN })).body.data.docs
      .find(d => d.id === doc.id);
    if (d2?.indexState === 'indexed') break;
  }
}
const d3 = (await c.call('GET', '/api/docs', undefined, { token: TOKEN })).body.data.docs
  .find(d => d.id === doc.id);
console.log(`索引状态: ${d3?.indexState}　块数 ${d3?.chunkCount}\n`);

const ch = await c.call('GET', `/api/docs/${doc.id}/chunks`, undefined, { token: TOKEN });
const list = ch.body.data?.chunks || ch.body.data || [];
console.log(`—— 入库后的 ${list.length} 个块 ——\n`);
list.forEach((x, i) => {
  const text = String(x.content || x.text || '').replace(/\s+/g, ' ').trim();
  console.log(`[${i}] ${text.length} 字：${text.slice(0, 160)}`);
});
console.log('\n看三件事：① 表格有没有被拍平/串行 ② 导航与页脚有没有进来 ③ 代码块有没有被切碎');
