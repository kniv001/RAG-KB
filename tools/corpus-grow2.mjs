/**
 * 扩语料·第二轮：对第一轮没覆盖到的主题，用**来源限定**逐个站点试。
 *
 * 第一轮暴露的问题：搜索后端几乎只回 CSDN，而 CSDN 现在对本机抓取一律 521
 * （昨晚还能抓，今天不行 —— 反爬是动态的）。所以改用 site: 限定到实测可达的站点，
 * 一个站点打不动就换下一个。
 *
 * 用法：node tools/corpus-grow2.mjs
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const SITES = ['cnblogs.com', 'juejin.cn', 'segmentfault.com', 'zhihu.com', 'baike.so.com'];
const TOPICS = [
  'HTTP/2 多路复用 头部压缩',
  'Redis 持久化 RDB AOF',
  'Kubernetes Pod 调度 亲和性',
  'Docker 容器网络 namespace',
  'JVM 垃圾回收 G1',
  'Python GIL 全局解释器锁',
  'Rust 所有权 借用检查',
  '布隆过滤器 原理 误判率',
  '一致性哈希 虚拟节点',
  '模型量化 int8 4bit',
  'DNS 递归解析 过程',
  'MySQL 事务隔离级别 MVCC',
  '向量数据库 检索 原理',
  '提示词工程 上下文 窗口',
  '微服务 熔断 降级',
];

const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
const TOKEN = `Bearer ${login.body.data.accessToken}`;
const call = (m, p, b) => c.call(m, p, b, { token: TOKEN });

const seen = new Set();
const added = [];
const missed = [];

for (const topic of TOPICS) {
  let done = false;
  for (const site of SITES) {
    if (done) break;
    const s = await call('POST', '/api/web/search', { query: topic, count: 3, site });
    const hits = (s.body?.data?.results || [])
      .filter((h) => /^https?:\/\//.test(h.url) && !seen.has(h.url))
      .slice(0, 2);
    if (!hits.length) continue;
    hits.forEach((h) => seen.add(h.url));

    const r = await call('POST', '/api/web/ingest', { urls: hits.map((h) => h.url) });
    // Ingested 是 Java record，ok() 是方法不是字段 —— 不会出现在 JSON 里。
    // 成功判据只能是 docId 有值且 error 为空。
    for (const it of r.body?.data?.results || []) {
      if (it.docId && !it.error) {
        added.push({ topic, site, docId: it.docId, url: it.url, chars: it.chars });
        console.log(`✅ [${site}] ${topic} → ${it.docId} ${it.chars}字`);
        done = true;
      } else {
        console.log(`   ✗ [${site}] ${String(it.url).slice(0, 52)}  ${(it.error || '').slice(0, 60)}`);
      }
    }
  }
  if (!done) { missed.push(topic); console.log(`— ${topic}：所有站点都没成`); }
}

console.log(`\n本轮新增 ${added.length} 篇，未覆盖 ${missed.length} 个主题`);
if (missed.length) console.log('未覆盖：' + missed.join('、'));
const all = JSON.parse(fs.readFileSync('D:/vs/rag-kb-java/data/_grown-docs.json', 'utf8')).concat(added);
fs.writeFileSync('D:/vs/rag-kb-java/data/_grown-docs.json', JSON.stringify(all, null, 2), 'utf8');
console.log(`累计清单 ${all.length} 篇 → data/_grown-docs.json`);
