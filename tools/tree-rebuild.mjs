/**
 * 重建主题树。
 *
 * 为什么现在重建：树建于语料只有 297 段的时候（两篇 B-tree + 三层缓存），
 * 而库里现在是 812 段、覆盖 40 多个方向。那段概览每轮都进回答提示词，
 * 于是在「知识库没有 X」时被要求点出「库里覆盖了哪些方向」的那句话，
 * 指向的是两个月前的语料。
 *
 * 接口是同步的（聚类是纯几何运算，只有给每簇起名调模型），几十秒量级。
 *
 * 用法：node tools/tree-rebuild.mjs
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);

const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
if (login.body?.code !== 0) { console.error('登录失败', login.status, login.body?.message); process.exit(1); }
const TOKEN = `Bearer ${login.body.data.accessToken}`;
const call = (m, p, b) => c.call(m, p, b, { token: TOKEN });

const before = await call('GET', '/api/tree/status');
console.log('重建前：', JSON.stringify(before.body?.data));

const t0 = Date.now();
const r = await call('POST', '/api/tree/build');
console.log(`重建返回（耗时 ${((Date.now() - t0) / 1000).toFixed(1)}s）：`, JSON.stringify(r.body));

const after = await call('GET', '/api/tree/status');
console.log('重建后：', JSON.stringify(after.body?.data));
