/**
 * 清空**答案缓存** —— 给 A/B 用。
 *
 * 为什么必须清：答案缓存的键含**提示词哈希 + 开关指纹**，所以"改了提示词就自动不命中"。
 * 但**两次跑同一个臂**（比如先冒烟再正式跑）键完全相同 ⇒ 第二遍全命中，
 * 于是那次跑出来的耗时是**回放**，不是真的。
 *
 * 实测踩过（2026-09-21 那次 A/B）：臂 A 21 题里 **14 题命中缓存**，
 * 报出来的"耗时中位 2.6s"完全没有意义（真身生成不可能那么快），
 * 而**判分是好的**（命中的是同一份提示词的答案）——
 * 所以它是**只会污染速度、不污染质量**的那种坑，更容易被忽略。
 *
 * 顺带一个判据：**缓存命中时不推思考**，所以「思考为 0 字」曾被当成命中的标志 ——
 * **不成立**：基线本身就有 10/21 题思考为 0（模型对简单题不想）。
 * 要证命中，只能看日志里的「回答缓存命中」。
 *
 * 用法：node tools/clear-answers.mjs
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login',
  { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;

const r = await c.call('DELETE', '/api/cache?which=answers', null, { token: TOKEN });
console.log('清空答案缓存 →', JSON.stringify(r.body?.data ?? r.body));
