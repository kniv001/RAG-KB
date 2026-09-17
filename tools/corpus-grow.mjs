/**
 * 扩语料：对「知识库没有的主题」逐个搜索 → 抓取 → 入库。
 *
 * 目的不是收集资料，是**造出一个可判定规模效应的语料**：
 * 现有语料 294 段里只有 39 段是不同内容（86% 是同一篇文章的副本），
 * 在这种语料上量「选块算法有没有优势」量不出东西 —— 候选之间本来就一样。
 *
 * 三条自我约束：
 *   · 每个主题只取前 N 条结果，且**同一 URL 只入库一次**（库里的重复就是没这条约束造成的）
 *   · 主题刻意选库里完全没有的方向，避免与现有内容重叠
 *   · 打印每个新增文档的 id，事后要清理有一份完整清单
 *
 * 用法：node tools/corpus-grow.mjs [每主题条数]
 */
import fs from 'node:fs';
import { makeClient } from './kb-client.mjs';

const BASE = process.env.KB_BASE || 'http://127.0.0.1:8080';
const PER_TOPIC = Number(process.argv[2] || 2);

const TOPICS = [
  'TCP 拥塞控制 慢启动 拥塞窗口',
  'HTTP/2 多路复用 头部压缩',
  'TLS 1.3 握手过程 密钥交换',
  'Redis 持久化 RDB AOF 区别',
  'Kafka 分区 副本 消费者组',
  'Raft 一致性算法 日志复制',
  'Kubernetes Pod 调度器 亲和性',
  'Docker 容器网络 namespace cgroup',
  'JVM 垃圾回收 G1 分代',
  'Python GIL 全局解释器锁 多线程',
  'Rust 所有权 借用检查器',
  '布隆过滤器 原理 误判率',
  '一致性哈希 虚拟节点',
  '限流算法 令牌桶 漏桶',
  'HNSW 向量索引 近似最近邻',
  'Transformer 自注意力 机制',
  'LoRA 低秩微调 原理',
  '模型量化 int8 4bit 原理',
  'DNS 递归解析 过程',
  'CDN 缓存 回源 命中率',
];

const cred = JSON.parse(fs.readFileSync('D:/vs/rag-kb/data/auth.json.migrated', 'utf8'));
const c = makeClient(BASE);
const login = await c.call('POST', '/api/auth/login', { username: cred.user, password: cred.password });
if (login.body?.code !== 0) { console.error('登录失败', login.status, login.body?.message); process.exit(1); }
const TOKEN = `Bearer ${login.body.data.accessToken}`;
const call = (m, p, b) => c.call(m, p, b, { token: TOKEN });

const st = await call('GET', '/api/web/status');
if (!st.body?.data?.enabled) { console.error('联网未开启，退出'); process.exit(1); }
console.log(`联网已开启（后端 ${st.body.data.backends?.join('→')}），每主题取 ${PER_TOPIC} 条\n`);

const seen = new Set();
const added = [];
let failed = 0;

for (const topic of TOPICS) {
  const s = await call('POST', '/api/web/search', { query: topic, count: PER_TOPIC + 2 });
  const hits = (s.body?.data?.results || [])
    .filter((h) => /^https?:\/\//.test(h.url) && !seen.has(h.url))
    .slice(0, PER_TOPIC);
  if (!hits.length) { console.log(`— ${topic}：没搜到可用结果`); continue; }
  hits.forEach((h) => seen.add(h.url));

  const r = await call('POST', '/api/web/ingest', { urls: hits.map((h) => h.url) });
  const list = r.body?.data?.results || r.body?.data?.ingested || [];
  for (const it of list) {
    if (it.ok && it.docId) {
      added.push({ topic, docId: it.docId, url: it.url, chars: it.chars });
      console.log(`✅ ${topic} → ${it.docId}  ${it.chars} 字  ${String(it.url).slice(0, 60)}`);
    } else {
      failed++;
      console.log(`❌ ${topic} → ${String(it.url).slice(0, 60)}   ${it.error || '未入库'}`);
    }
  }
}

console.log(`\n入库成功 ${added.length} 篇，失败 ${failed} 篇`);
fs.writeFileSync('D:/vs/rag-kb-java/data/_grown-docs.json',
  JSON.stringify(added, null, 2), 'utf8');
console.log('清单已写入 data/_grown-docs.json（事后清理用）');
