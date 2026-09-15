/**
 * 加密 HTTP 客户端（测试/诊断用）。
 *
 * 实现的是服务端约定的那套协议：
 *   X-Enc-Key   base64( RSA-OAEP(aesKey) )      AES 密钥每请求新生成
 *   X-Enc-Meta  base64( {"iv","d"} )            明文 {ts, nonce, token}
 *   请求体      {"iv","d"}                       明文即原始请求 JSON
 *   响应体      {"iv","d"}                       复用同一密钥，服务端换新 IV
 *
 * 用 Node 的 crypto 模块，其 RSA-OAEP(SHA-256) 语义与浏览器 WebCrypto 完全一致 ——
 * 意味着这套客户端能跑通，浏览器端就一定能对上。
 */
import crypto from 'node:crypto';

export function makeClient(baseUrl) {
  let publicKey = null;
  let keyInfo = null;
  const cookies = new Map();

  async function fetchPublicKey(force = false) {
    if (publicKey && !force) return keyInfo;
    const r = await fetch(`${baseUrl}/api/crypto/public-key`);
    const j = await r.json();
    if (!j?.data?.publicKey) throw new Error(`取公钥失败: HTTP ${r.status}`);
    publicKey = crypto.createPublicKey({
      key: Buffer.from(j.data.publicKey, 'base64'), format: 'der', type: 'spki',
    });
    keyInfo = j.data;
    return keyInfo;
  }

  function encWith(aesKey, obj) {
    const iv = crypto.randomBytes(12);
    const c = crypto.createCipheriv('aes-256-gcm', aesKey, iv);
    const ct = Buffer.concat([c.update(JSON.stringify(obj), 'utf8'), c.final(), c.getAuthTag()]);
    return { iv: iv.toString('base64'), d: ct.toString('base64') };
  }

  function decWith(aesKey, env) {
    const buf = Buffer.from(env.d, 'base64');
    const tag = buf.subarray(buf.length - 16);
    const data = buf.subarray(0, buf.length - 16);
    const d = crypto.createDecipheriv('aes-256-gcm', aesKey, Buffer.from(env.iv, 'base64'));
    d.setAuthTag(tag);
    return Buffer.concat([d.update(data), d.final()]).toString('utf8');
  }

  function cookieHeader() {
    return [...cookies.entries()].map(([k, v]) => `${k}=${v}`).join('; ');
  }

  function absorbCookies(res) {
    const raw = res.headers.getSetCookie?.() ?? [];
    for (const line of raw) {
      const [pair] = line.split(';');
      const i = pair.indexOf('=');
      if (i <= 0) continue;
      const name = pair.slice(0, i).trim();
      const value = pair.slice(i + 1).trim();
      if (value === '') cookies.delete(name); else cookies.set(name, value);
    }
  }

  /**
   * @param token 放进加密元信息的令牌（Bearer xxx / Basic xxx），null 表示不带
   */
  async function call(method, path, payload, opts = {}) {
    await fetchPublicKey(opts.forceKeyRefresh);
    const aesKey = crypto.randomBytes(32);
    const wrapped = crypto.publicEncrypt(
      { key: publicKey, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256' }, aesKey);

    const meta = encWith(aesKey, {
      ts: opts.ts ?? Date.now(),
      nonce: opts.nonce ?? crypto.randomBytes(12).toString('base64url'),
      token: opts.token ?? null,
    });

    const headers = {
      'X-Enc-Key': wrapped.toString('base64'),
      'X-Enc-Meta': Buffer.from(JSON.stringify(meta)).toString('base64'),
    };
    const cookie = cookieHeader();
    if (cookie) headers['Cookie'] = cookie;

    const init = { method, headers, redirect: 'manual' };
    if (payload !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(encWith(aesKey, payload));
    }

    const r = await fetch(`${baseUrl}${path}`, init);
    absorbCookies(r);

    const text = await r.text();
    let parsed = null;
    try { parsed = JSON.parse(text); } catch { /* 非 JSON */ }

    const encrypted = r.headers.get('X-Encrypted') === '1';
    let body = parsed ?? text;
    let decrypted = false;
    if (encrypted && parsed?.d) {
      body = JSON.parse(decWith(aesKey, parsed));
      decrypted = true;
    }
    return { status: r.status, body, encrypted, decrypted, raw: text, headers: r.headers };
  }

  /**
   * 打开一条流式（SSE）连接。
   *
   * 与 call 的区别：响应不走「整体加密」，而是每个事件的 data 各自是一个加密信封
   * —— 因为过滤器的整体加密需要先缓存完整响应，那会破坏流式。
   * 返回 aesKey 与 decrypt，供调用方逐事件解密。
   */
  async function openStream(method, path, payload, opts = {}) {
    await fetchPublicKey();
    const aesKey = crypto.randomBytes(32);
    const wrapped = crypto.publicEncrypt(
      { key: publicKey, padding: crypto.constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256' }, aesKey);
    const meta = encWith(aesKey, {
      ts: Date.now(),
      nonce: crypto.randomBytes(12).toString('base64url'),
      token: opts.token ?? null,
    });
    const headers = {
      'X-Enc-Key': wrapped.toString('base64'),
      'X-Enc-Meta': Buffer.from(JSON.stringify(meta)).toString('base64'),
      Accept: 'text/event-stream',
    };
    const cookie = cookieHeader();
    if (cookie) headers['Cookie'] = cookie;

    const init = { method, headers };
    if (payload !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(encWith(aesKey, payload));
    }
    const response = await fetch(`${baseUrl}${path}`, init);
    absorbCookies(response);
    return {
      response,
      aesKey,
      /** 事件 data 是加密信封就解开，否则原样返回 */
      decrypt: (data) => (data && data.d ? JSON.parse(decWith(aesKey, data)) : data),
    };
  }

  return {
    call,
    openStream,
    fetchPublicKey,
    keyInfo: () => keyInfo,
    cookies,
    refreshCookie: () => cookies.get('kb_refresh'),
  };
}

/** 逐事件解析 SSE 流。Spring 的 SseEmitter 写出形如 "event:token\ndata:{...}\n\n"。 */
export async function readSse(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let name = 'message';
      const dataLines = [];
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) name = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length) onEvent(name, dataLines.join('\n'));
    }
  }
}
