/**
 * 探测各搜索后端对中文查询的实际表现。
 *
 * 起因：Bing 收到了完整的查询（码点确认为「三层缓存架构」六个字），
 * 却返回了「三」这个汉字的结果页 —— 所以问题不在编码，在 Bing 那边。
 * 这份脚本用同一个查询横向比几个后端，看谁真的能用。
 *
 * 用 curl 子进程而不是 Node 的 fetch：这台机器上 IPv4/IPv6 的可达性差异很大
 * （校园网按 SNI 选择性阻断），必须能显式指定走哪一边。
 */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
const q = process.argv[2] || '三层缓存架构';
const enc = encodeURIComponent(q);
const outDir = 'data/probe';
fs.mkdirSync(outDir, { recursive: true });

const BACKENDS = [
  ['bing',        `https://www.bing.com/search?q=${enc}&count=10`],
  ['bing-mkt',    `https://www.bing.com/search?q=${enc}&count=10&mkt=zh-CN`],
  ['bing-setmkt', `https://www.bing.com/search?q=${enc}&count=10&setmkt=zh-CN&setlang=zh-CN`],
  ['cn-bing',     `https://cn.bing.com/search?q=${enc}`],
  ['sogou',       `https://www.sogou.com/web?query=${enc}`],
  ['so360',       `https://www.so.com/s?q=${enc}`],
  ['baidu',       `https://www.baidu.com/s?wd=${enc}`],
  ['mojeek',      `https://www.mojeek.com/search?q=${enc}`],
];

function curl4(url, file) {
  try {
    execFileSync('curl', ['-s', '-4', '-L', '--max-time', '20', '-A', UA,
      '-H', 'Accept-Language: zh-CN,zh;q=0.9', url, '-o', file], { timeout: 30000 });
  } catch { /* 超时或失败都当空处理 */ }
}

/** 通用的标题提取：h2>a（Bing）或 h3（多数中文引擎） */
function titles(html, limit = 3) {
  const out = [];
  for (const re of [/<h2[^>]*>\s*<a[^>]*>([\s\S]*?)<\/a>/g, /<h3[^>]*>([\s\S]*?)<\/h3>/g]) {
    for (const m of html.matchAll(re)) {
      const t = m[1].replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
      if (t && !out.includes(t)) out.push(t);
      if (out.length >= limit) return out;
    }
  }
  return out;
}

console.log(`查询：「${q}」\n`);
console.log('后端            字节     结果标题');
console.log('-'.repeat(96));

for (const [name, url] of BACKENDS) {
  const file = `${outDir}/${name}.html`;
  curl4(url, file);
  let html = '';
  let size = 0;
  try {
    html = fs.readFileSync(file, 'utf8');
    size = Buffer.byteLength(html);
  } catch { /* 没下载到 */ }
  const ts = titles(html);
  const shown = ts.length ? ts.map((t) => t.slice(0, 28)).join(' | ') : '（没解析出标题）';
  console.log(`${name.padEnd(14)} ${String(size).padStart(8)}  ${shown}`);
}

// 顺便判断一下：结果里到底有没有把查询词拆散
console.log('\n是否把「三层」拆成了单个「三」：');
for (const [name] of BACKENDS) {
  try {
    const h = fs.readFileSync(`${outDir}/${name}.html`, 'utf8');
    const ts = titles(h, 10).join(' ');
    const bad = /^三（|阿拉伯数字之一|汉语汉字/.test(ts) || /三（汉语汉字）/.test(ts);
    console.log(`  ${name.padEnd(14)} ${bad ? '❌ 是（结果跑偏到单字「三」）' : '✅ 否'}`);
  } catch { console.log(`  ${name.padEnd(14)} —`); }
}
