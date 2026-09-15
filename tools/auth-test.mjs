/**
 * 双令牌认证链路测试。走的是完整加密通道（/api/* 全部要求加密）。
 *
 * 关键验证点之一：Python 版写入的 bcrypt 哈希（$2b$ 前缀）能否被 Java 的
 * BCryptPasswordEncoder 正确校验 —— 这决定了现有账号能否平滑迁移。
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const CRED_FILE = process.env.KB_CRED_FILE || 'D:/vs/rag-kb/data/auth.json.migrated';
const cred = JSON.parse(fs.readFileSync(CRED_FILE, 'utf8'));

let pass = 0, fail = 0;
const check = (label, cond, extra = '') => {
  if (cond) { pass++; console.log(`  [PASS] ${label} ${extra}`); }
  else { fail++; console.log(`  [FAIL] ${label} ${extra}`); }
};

const c = makeClient(BASE);
console.log(`测试账号: ${cred.user}\n`);

// 1) 公开配置
const cfg = await c.call('GET', '/api/auth/config');
check('GET /api/auth/config -> 200', cfg.status === 200, `-> ${cfg.status}`);
check('已存在用户（users 表非空）', cfg.body?.data?.hasUser === true);
check('Redis 可用', cfg.body?.data?.redis === true,
  `access=${cfg.body?.data?.accessTtl}s refresh=${cfg.body?.data?.refreshTtl}s`);

// 2) 未加密请求应被拒
const plain = await fetch(`${BASE}/api/auth/config`);
check('未加密请求被拒（加密是硬要求）', plain.status === 400, `-> ${plain.status}`);

// 3) 未带令牌访问受保护接口
const noTok = await c.call('GET', '/api/auth/me');
check('无令牌访问 /api/auth/me -> 401', noTok.status === 401, `-> ${noTok.status}`);
check('401 响应体含 expired 标记（前端据此决定刷新还是跳登录）',
  noTok.body?.expired === false, JSON.stringify(noTok.body));

// 4) 错误密码
const bad = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: 'definitely-not-the-password' });
check('错误密码 -> 401', bad.status === 401, `-> ${bad.status} ${bad.body?.message ?? ''}`);

// 5) 正确密码登录（同时验证 bcrypt 跨语言兼容）
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
check('正确密码登录 -> 200', login.status === 200, `-> ${login.status} ${JSON.stringify(login.body).slice(0, 120)}`);
check('Python 写入的 $2b$ bcrypt 哈希被 Java 正确校验（跨语言兼容）', login.status === 200);
const access = login.body?.data?.accessToken;
check('拿到 access token', !!access, `${(access || '').slice(0, 28)}…`);
check('响应体不含 refreshToken（它只走 httpOnly Cookie）',
  login.body?.data?.refreshToken === undefined);
check('下发了 refresh Cookie', !!c.refreshCookie(), `kb_refresh=${(c.refreshCookie() || '').slice(0, 16)}…`);

// 6) 带令牌访问
const me = await c.call('GET', '/api/auth/me', undefined, { token: `Bearer ${access}` });
check('带令牌访问 /api/auth/me -> 200', me.status === 200,
  `-> ${me.status} user=${me.body?.data?.username} sessions=${me.body?.data?.sessions}`);

const health = await c.call('GET', '/api/health', undefined, { token: `Bearer ${access}` });
check('带令牌访问受保护业务接口 -> 200', health.status === 200, `-> ${health.status}`);

// 7) 伪造令牌
const forged = await c.call('GET', '/api/auth/me', undefined, { token: 'Bearer not.a.real.token' });
check('伪造令牌 -> 401', forged.status === 401, `-> ${forged.status}`);

// 8) 刷新（Cookie 自动携带）
const oldRefresh = c.refreshCookie();
const refreshed = await c.call('POST', '/api/auth/refresh');
check('用 Cookie 刷新 -> 200', refreshed.status === 200, `-> ${refreshed.status} ${refreshed.body?.message ?? ''}`);
const access2 = refreshed.body?.data?.accessToken;
check('刷新后拿到不同的 access token', !!access2 && access2 !== access);
check('refresh 已轮换（新旧不同）', c.refreshCookie() && c.refreshCookie() !== oldRefresh);

const me2 = await c.call('GET', '/api/auth/me', undefined, { token: `Bearer ${access2}` });
check('新令牌可用', me2.status === 200, `-> ${me2.status}`);

// 9) 改密码 —— 只测「原密码错误」这条安全路径，不真改用户密码
const badPw = await c.call('POST', '/api/auth/password',
  { oldPassword: 'wrong-old-password', newPassword: 'whatever-12345' },
  { token: `Bearer ${access2}` });
check('原密码错误 -> 400（且不会改动任何东西）', badPw.status === 400,
  `-> ${badPw.status} ${badPw.body?.message ?? ''}`);

// 10) 登出后刷新失效
const out = await c.call('POST', '/api/auth/logout');
check('登出 -> 200', out.status === 200, `-> ${out.status}`);
const afterOut = await c.call('POST', '/api/auth/refresh');
check('登出后刷新 -> 401（Redis 里的 refresh 已删）', afterOut.status === 401, `-> ${afterOut.status}`);
check('登出后 Cookie 被清空', !c.refreshCookie() || c.refreshCookie() === '');

console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
