# -*- coding: utf-8 -*-
"""
靶子质量检查：**排到很后面的靶子，多半是标错了**。

没有第二标注者时，这是唯一能自查靶子的办法：
  对每道题，用**原始问句**做向量检索，看每个靶子排第几。
  · 排在前 60 —— 正常（说明它确实是这段内容的合理目标）
  · 排在 60 之外 —— **可疑**：要么靶子标错了块，要么题干的说法与那块内容差得太远

（用原始问句而不是规划器改写句：改写句是"为了检索优化过"的，会把分数抬好看，
 掩盖"人问法 ↔ 材料措辞"的真实距离。）

用法：python tools/target-quality-check.py [警戒线，默认 60]
"""
import io
import json
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
BS = chr(92)
_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def unescape(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == BS and i + 1 < len(s):
            out.append(_ESC.get(s[i + 1], BS + s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def psql_rows(sql):
    f = os.path.join(HERE, "_tq.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    return [[unescape(x) for x in ln.rstrip("\r").split("\t")]
            for ln in raw.split("\n") if ln.strip()]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    warn = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    cfg = json.load(io.open(os.path.join(HERE, "multihop-questions.json"), encoding="utf-8"))
    rows = psql_rows("SELECT c.id, c.seq, c.content, d.name FROM chunks c "
                     "JOIN documents d ON d.id=c.doc_id ORDER BY c.id")
    meta = [(r[1], r[3]) for r in rows]                       # seq, doc
    bodies = [r[2] for r in rows]
    vcache = os.path.join(HERE, "_corpus_vecs.json")
    vecs = json.load(io.open(vcache, encoding="utf-8"))["vecs"]
    norm = lambda s: re.sub(r"\s+", "", s)

    def embed(q):
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": "bge-m3", "input": [q]}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)["embeddings"][0]

    def cos(a, b):
        s = sum(x * y for x, y in zip(a, b))
        return s / ((sum(x * x for x in a) ** .5) * (sum(x * x for x in b) ** .5) + 1e-9)

    bad = []
    print(f"靶子质量（用原始问句检索，警戒线 = rank {warn}）\n")
    for case in cfg["cases"]:
        qv = embed(case["q"])
        order = [j for _, j in sorted(((cos(qv, v), j) for j, v in enumerate(vecs)),
                                      reverse=True)[:400]]
        rowsout = []
        for t in case["targets"]:
            idx = next((i for i, m in enumerate(meta)
                        if m[1] == case["doc"] and m[0] == str(t["seq"])), None)
            if idx is None:
                rowsout.append((t["seq"], 0, "指不到块"))
                continue
            key = norm(bodies[idx])[:40]
            grp = [i for i, m in enumerate(meta)
                   if m[1] == case["doc"] and norm(bodies[i]).startswith(key[:20])] or [idx]
            r = min([order.index(g) + 1 for g in grp if g in order], default=0)
            rowsout.append((t["seq"], r, ""))
            if r == 0 or r > warn:
                bad.append((case["q"], t["seq"], r, t["fp"][:24]))
        mark = "✅" if all(0 < r <= warn for _, r, _ in rowsout) else "⚠"
        print(f"  {mark} {case['q'][:36]}")
        print(f"      名次 {[r or '>400' for _, r, _ in rowsout]}　{f'seq {[s for s,_,_ in rowsout]}'}")
    print(f"\n可疑靶子 {len(bad)} 个：")
    for q, sq, r, fp in bad:
        print(f"  rank={r or '>400'}　seq={sq}　「{fp}」　{q[:30]}")


if __name__ == "__main__":
    main()
