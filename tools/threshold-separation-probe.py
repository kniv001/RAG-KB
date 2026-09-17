# -*- coding: utf-8 -*-
"""
门槛可分性探针：库内问题 vs 库外问题，向量距离分布能不能分开？

这是「用分数门槛替代 LLM 评估（assess）」这个方案的承重假设 ——
如果两类问题的距离分布重叠，门槛无论取多少都会误判，
那个方案就不成立，得另找判据。

用法：python tools/threshold-separation-probe.py
"""
import json
import os
import subprocess
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434/api/embed"
MODEL = "bge-m3"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS_FILE = r"D:\vs\rag-kb\data\pgapp.txt"

# 库内：知识库确实有内容的问题（B-tree 页结构 / 三层缓存 / 模版 PDF）
IN_KB = [
    "PostgreSQL 的 B-tree 索引页结构是怎样的？",
    "B-tree 索引有几种页？",
    "页内项怎么排列？",
    "索引页大小是多少？",
    "fillfactor 是什么？",
    "索引的根页可以同时是叶页吗？",
    "bt_page_items 是干什么用的？",
    "三层缓存架构是什么？",
    "技术文档模版里有哪些章节？",
    "索引页里的 fillfactor 对 B-tree 有什么影响？",
]
# 库外：知识库完全没有的主题
OUT_KB = [
    "请介绍一下量子纠缠在量子计算里的作用。",
    "请介绍一下「紫电青霜七号协议」及其淘汰策略。",
    "法国的首都是哪里？",
    "Python 的 GIL 是什么？",
    "红烧肉怎么做才好吃？",
]


def embed(texts):
    body = json.dumps({"model": MODEL, "input": texts}).encode("utf-8")
    req = urllib.request.Request(OLLAMA, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["embeddings"]


def literal(vec):
    return "'[" + ",".join(f"{x:.7f}" for x in vec) + "]'"


def main():
    # Windows 控制台默认 GBK，中文问题与表格会直接抛 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    questions = [("IN ", q) for q in IN_KB] + [("OUT", q) for q in OUT_KB]
    vecs = []
    for i in range(0, len(questions), 8):
        vecs.extend(embed([q for _, q in questions[i:i + 8]]))

    selects = []
    for idx, (tag, _) in enumerate(questions):
        v = literal(vecs[idx])
        selects.append(
            f"SELECT '{tag}{idx:02d}' AS q, "
            f"round((SELECT min(embedding <=> {v}) FROM chunks)::numeric, 3) AS min_d, "
            f"(SELECT count(*) FROM chunks WHERE embedding <=> {v} <= 0.60) AS n60, "
            f"(SELECT count(DISTINCT md5(content)) FROM chunks WHERE embedding <=> {v} <= 0.60) AS u60, "
            f"(SELECT count(*) FROM chunks WHERE embedding <=> {v} <= 0.40) AS n40, "
            f"(SELECT count(*) FROM chunks WHERE embedding <=> {v} <= 0.25) AS n25"
        )
    sql = " UNION ALL ".join(selects) + " ORDER BY q;"

    # 必须走文件：一条向量 1024 个浮点，15 条拼进命令行会超 Windows 的 32K 上限
    # （报 WinError 206 文件或扩展名太长，跟 SQL 本身无关）
    sql_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_threshold_probe.sql")
    with open(sql_file, "w", encoding="utf-8") as f:
        f.write(sql)

    env = dict(os.environ)
    env["PGPASSWORD"] = open(PGPASS_FILE, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    proc = subprocess.run(
        [PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-t", "-A", "-F", "\t", "-f", sql_file],
        capture_output=True, env=env,
    )
    out = proc.stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        print("psql failed:", proc.stderr.decode("utf-8", "backslashreplace")[:2000])
        sys.exit(1)

    print(f"{'q':<8}{'min_d':>7}{'#<=.60':>8}{'uniq':>6}{'#<=.40':>8}{'#<=.25':>8}   问题")
    rows = {}
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        tag = parts[0][:3].strip()
        rows[parts[0]] = parts[1:]
        idx = int(parts[0][3:])
        print(f"{parts[0]:<8}{parts[1]:>7}{parts[2]:>8}{parts[3]:>6}{parts[4]:>8}{parts[5]:>8}   {questions[idx][1]}")

    ins = [float(v[0]) for k, v in rows.items() if k.startswith("IN")]
    outs = [float(v[0]) for k, v in rows.items() if k.startswith("OUT")]
    print()
    print(f"IN  库内 {len(ins)} 题：min_d  {min(ins):.3f} ~ {max(ins):.3f}   均值 {sum(ins)/len(ins):.3f}")
    print(f"OUT 库外 {len(outs)} 题：min_d  {min(outs):.3f} ~ {max(outs):.3f}   均值 {sum(outs)/len(outs):.3f}")
    gap_lo, gap_hi = max(ins), min(outs)
    if gap_lo < gap_hi:
        print(f"→ 存在分离带：({gap_lo:.3f}, {gap_hi:.3f})，门槛可取中间值")
    else:
        print(f"→ **重叠**：库内最高 {gap_lo:.3f} ≥ 库外最低 {gap_hi:.3f}，单靠 min_d 无法分开")


if __name__ == "__main__":
    main()
