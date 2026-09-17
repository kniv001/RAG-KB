# -*- coding: utf-8 -*-
"""
给 answer-ab.mjs 的两轮答案打分并对照。

判据（全机械，无模型在环）：
  · **独有词命中**：答案里出现了多少个「只在该主题文档里出现」的标识符/数字。
    这些词就是「资料里的具体事实」，答案里有它 = 真的用上了那篇文档。
  · **兜底率**：答案里出现「知识库中没有」这类措辞的比例（越低越好）。
  · 答案长度：命中率高但只是因为写得多，这条能把它暴露出来。

用法：python tools/answer-ab-score.py A B
"""
import io
import json
import os
import re
import subprocess
import sys

PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
HERE = os.path.dirname(os.path.abspath(__file__))
SQL_FILE = os.path.join(HERE, "_score.sql")
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-\.]{2,}|\d{2,}")
STOP = {"the", "and", "for", "with", "http", "https", "com", "www", "org", "cnblogs",
        "article", "post", "code", "class", "public", "static", "void", "img", "src",
        "div", "span", "href", "this", "that", "float", "int", "region", "size"}
FALLBACK = re.compile(r"知识库中没有|没有相关|未收录")

# 题 → 目标文档名的 LIKE 模式
TARGETS = [
    (["%HTTP 2%", "%HTTP2%", "%http系列%"]),
    (["%Redis持久化%", "%Redis两种持久化%"]),
    (["%布隆过滤器%"]),
    (["%HNSW%", "%向量数据库%"]),
    (["%Kubernetes Pod调度%"]),
    (["%模型量化%", "%INT8量化%"]),
    (["%docker容器网络%"]),
    (["%G1%"]),
    (["%raft%"]),
    (["%限流%"]),
]


def psql(sql):
    with io.open(SQL_FILE, "w", encoding="utf-8") as f:
        f.write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    p = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                        "-t", "-A", "-F", "\t", "-f", SQL_FILE], capture_output=True, env=env)
    return p.stdout.decode("utf-8", "replace")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    labels = sys.argv[1:] or ["A", "B"]
    runs = {L: json.load(io.open(os.path.join(HERE, "..", "data", f"_answers-{L}.json"),
                                encoding="utf-8")) for L in labels}

    # 每篇文档的独有词（只出现在 ≤3 篇文档里的标识符/数字）
    rows = psql("SELECT doc_id, content FROM chunks;")
    df, docs = {}, {}
    for line in rows.splitlines():
        p = line.split("\t")
        if len(p) < 2:
            continue
        docs.setdefault(p[0], []).append(p[1])
    for did, texts in docs.items():
        for t in {x.lower() for x in TOKEN.findall(" ".join(texts))} - STOP:
            df.setdefault(t, set()).add(did)

    topic_terms = []
    for pats in TARGETS:
        ids = set()
        for pat in pats:
            ids |= {x.strip() for x in psql(
                f"SELECT id FROM documents WHERE name LIKE '{pat}';").splitlines() if x.strip()}
        topic_terms.append({w for w, ds in df.items() if ds & ids and len(ds) <= 3})

    print(f"{'问题':<30}" + "".join(f"{L+' 命中':>9}{L+' 字数':>8}" for L in labels))
    tot = {L: [0, 0, 0, 0] for L in labels}   # 命中词、总词、兜底数、字数
    n = min(len(runs[labels[0]]), len(topic_terms))
    for i in range(n):
        line = f"{runs[labels[0]][i]['q'][:28]:<30}"
        for L in labels:
            a = runs[L][i]["text"].lower()
            terms = topic_terms[i]
            hit = sum(1 for w in terms if w in a)
            tot[L][0] += hit
            tot[L][1] += len(terms)
            tot[L][2] += 1 if FALLBACK.search(runs[L][i]["text"]) else 0
            tot[L][3] += len(runs[L][i]["text"])
            line += f"{100*hit/max(len(terms),1):>8.0f}%{len(runs[L][i]['text']):>8}"
        print(line)
    print()
    for L in labels:
        h, t, fb, ch = tot[L]
        print(f"  {L}：独有词命中 {100*h/max(t,1):.1f}%　兜底（说「知识库中没有」）{fb}/{n} 题　"
              f"答案均长 {ch//n} 字")
    if len(labels) == 2:
        a, b = labels
        print(f"\n  差异：命中 {100*(tot[b][0]-tot[a][0])/max(tot[a][1],1):+.1f} 个点，"
              f"兜底 {tot[b][2]-tot[a][2]:+d} 题，均长 {tot[b][3]//n - tot[a][3]//n:+d} 字")


if __name__ == "__main__":
    main()
