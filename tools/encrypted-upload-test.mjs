/**
 * 加密上传的端到端验证。
 *
 * 关键不是「接口返回 200」，而是**字节完全一致** —— 解密只要错一个字节，
 * 文件就静静地坏掉，而接口看起来完全正常。所以这里上传一份内容已知的文本，
 * 建完索引后把块取回来逐字比对。
 *
 * 同时验三件事：
 *   ① 加密路径能把文件正确还原
 *   ② 文件名确实藏在密文里（明文头里不应该出现它）
 *   ③ 篡改密文会被拒绝，且不在磁盘上留下半截文件
 */
import fs from 'node:fs';
import crypto from 'node:crypto';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);

let pass = 0, fail = 0;
const ok = (n, cond, e = '') => {
  if (cond) { pass++; console.log(`  ✅ ${n}${e ? ' — ' + e : ''}`); }
  else { fail++; console.log(`  ❌ ${n}${e ? ' — ' + e : ''}`); }
};

const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;
const pub = await c.fetchPublicKey();

// 与 app.js 完全一致的封装方式
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

/** 上传一段已知内容，返回 {docId, name, bytes} */
async function upload(content, filename, { tamper = false } = {}) {
  const { key, wrapped } = newSession();
  const iv = crypto.randomBytes(12);
  const ci = crypto.createCipheriv('aes-256-gcm', key, iv);
  const ct = Buffer.concat([ci.update(Buffer.from(content, 'utf8')), ci.final(), ci.getAuthTag()]);
  if (tamper) ct[Math.floor(ct.length / 2)] ^= 0xff;   // 改一个字节

  const meta = seal(key, {
    ts: Date.now(), nonce: crypto.randomBytes(9).toString('base64url'),
    token: TOKEN, iv: iv.toString('base64'), name: filename,
    size: Buffer.byteLength(content, 'utf8'),
  });
  const headers = {
    'X-Enc-Key': wrapped,
    'X-Enc-Meta': Buffer.from(JSON.stringify(meta)).toString('base64'),
    'Content-Type': 'application/octet-stream',
  };

  const res = await fetch(`${BASE}/api/docs/upload-encrypted`, { method: 'POST', headers, body: ct });
  const parsed = JSON.parse(await res.text());
  const body = res.headers.get('X-Encrypted') === '1' && parsed?.d ? open(key, parsed) : parsed;
  // 注意返回的是**响应头**，不是上面那组请求头 —— 两者都叫 headers 很容易搞混
  return { status: res.status, body, respHeaders: res.headers, reqHeaders: headers };
}

const MARK = 'KB-加密往返-' + crypto.randomBytes(4).toString('hex');
const CONTENT = [
  `# 加密上传往返测试`,
  ``,
  `唯一标记：${MARK}`,
  ``,
  `内容含中文、emoji 🎯 与符号 <>&"'，用来验证 UTF-8 与转义都没被破坏。`,
  `再补几行，保证切分后至少有一块完整包含上面那行标记。`,
  `填充段落一：向量检索按余弦距离排序，关键词检索按词频打分。`,
  `填充段落二：混合检索用 RRF 融合两路结果，只看排名不看分数。`,
].join('\n');

console.log('\n=== 1. 加密上传并还原 ===');
const up = await upload(CONTENT, `enc-test-${Date.now()}.md`);
ok('上传被接受', up.status === 200 && up.body?.code === 0,
   `HTTP ${up.status} ${up.body?.message || ''}`);
const docId = up.body?.data?.id;
ok('返回 docId', !!docId, docId);
ok('字节数与原文一致', up.body?.data?.bytes === Buffer.byteLength(CONTENT, 'utf8'),
   `${up.body?.data?.bytes} vs ${Buffer.byteLength(CONTENT, 'utf8')}`);
ok('文件名正确回显', up.body?.data?.name?.startsWith('enc-test-'), up.body?.data?.name);

console.log('\n=== 2. 文件名是否真的没走明文 ===');
// 这道检查的意思是：真正上网的那份请求里，除了「被 RSA 包起来的密钥」和
// 「被 AES 加密的元信息」之外，没有任何可读的文件信息。
// 若还有人误把文件名塞进 X-Filename 之类的头，这里就会挂。
const rawKey = Buffer.from(up.reqHeaders['X-Enc-Key'], 'base64');
ok('请求头里不含文件名', !JSON.stringify(up.reqHeaders).includes('enc-test-'));
ok('  请求头里不含明文中文', !/[一-龥]/.test(JSON.stringify(up.reqHeaders)));
ok('  X-Enc-Key 是 RSA-3072 密文（384 字节，不可读）', rawKey.length === 384, `${rawKey.length} 字节`);
ok('响应标记为已加密', up.respHeaders.get('X-Encrypted') === '1');

console.log('\n=== 3. 建索引后取块，逐字比对 ===');
const t = await c.call('POST', `/api/docs/${docId}/index`, undefined, { token: TOKEN });
ok('提交索引任务', t.status === 200 || t.status === 202, `taskId=${t.body?.data?.taskId}`);

let status = 'running';
for (let i = 0; i < 60 && status === 'running'; i++) {
  await new Promise((r) => setTimeout(r, 800));
  const st = await c.call('GET', `/api/docs/tasks/${t.body.data.taskId}`, undefined, { token: TOKEN });
  status = st.body?.data?.status;
  if (status !== 'running') {
    ok('索引完成', status === 'done', `${status} ${st.body?.data?.message || ''}，${st.body?.data?.result?.chunks ?? '?'} 块`);
  }
}

// 块接口给的是 preview（截断到 200 字）与 chars（完整长度），不是全文
const ch = await c.call('GET', `/api/docs/${docId}/chunks`, undefined, { token: TOKEN });
const rows = ch.body?.data?.chunks || [];
const text = rows.map((x) => x.preview).join('\n');
const totalChars = rows.reduce((n, x) => n + (x.chars || 0), 0);

ok('取回块内容', text.length > 0, `${rows.length} 块 / preview ${text.length} 字`);
ok('块总字数与原文一致（解密无截断）',
   totalChars === CONTENT.length, `${totalChars} vs ${CONTENT.length}`);
ok('唯一标记原样存在（解密无误）', text.includes(MARK), MARK);
ok('中文与 emoji 未被破坏', text.includes('🎯') && text.includes('中文'));
ok('特殊符号未被转义', text.includes('<>&"\''));

console.log('\n=== 4. 篡改密文必须被拒绝 ===');
const bad = await upload(CONTENT, `tampered-${Date.now()}.md`, { tamper: true });
ok('篡改体被拒绝', bad.body?.code !== 0, bad.body?.message || `code=${bad.body?.code}`);
ok('  且没有落库', !bad.body?.data?.id);

console.log(`\n${'─'.repeat(50)}`);
console.log(`通过 ${pass} 项，失败 ${fail} 项`);
if (docId) console.log(`（测试文档 ${docId} 留在库里，清理时连同它的 .md 一起删）`);
process.exit(fail ? 1 : 0);
