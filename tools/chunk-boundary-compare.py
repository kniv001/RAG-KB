# -*- coding: utf-8 -*-
"""
分块边界质量：旧（字符硬切，库中现存）vs 新（句子边界），游标顺序定位。

两处方法上的坑（都踩过）：
  · 不能用 doc.find(块首12字) 定位 —— 重复前缀会定位到错误位置，报出假象。
    块在文中顺序排列，必须带游标：每块从上一块位置之后开始找。
  · 不能用 psql -F 分隔取 content —— 块内有换行，行分隔会把它冲散。
    用 COPY (SELECT ...) TO STDOUT，换行会被转义成 \\n。

判据：块首在原文中前一个字符是不是句末标点 / 换行（BOUND）。
不是 → 该块从句子中间开始，就是要消掉的那种。

用法：python tools/chunk-boundary-compare.py <doc_id> <原文路径> <新块JSON>
"""
import io
import json
import os
import subprocess
import sys

PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
HERE = os.path.dirname(os.path.abspath(__file__))
BOUND = set("。！？!?；;）)]】》」\"'\n")
BS = chr(92)   # 反斜杠，写成 chr 避免多层转义出错


def old_chunks(doc_id):
    sql = f"COPY (SELECT content FROM chunks WHERE doc_id='{doc_id}' ORDER BY seq) TO STDOUT;"
    f = os.path.join(HERE, "_cb.sql")
    io.open(f, "w", encoding="utf-8").write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    rows = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        for esc, real in ((BS + BS, BS), (BS + "n", "\n"), (BS + "r", "\r"), (BS + "t", "\t")):
            line = line.replace(esc, real)
        rows.append(line)
    return rows


PUNCT = set("。！？!?；;）)]】》」\"'")


def at_boundary(doc, i):
    """
    块首 i 是不是落在「行首」或「句末标点后」。

    必须**向前跳过空白再看**：块会 strip() 掉前导空白，缩进行（代码块）的块首
    在原文里前面是空格而不是换行 —— 直接看 doc[i-1] 会把大批正常的行首判成半句。
    """
    j = i
    while j > 0 and doc[j - 1] in " \t\r":
        j -= 1
    if j == 0 or doc[j - 1] == "\n" or doc[j - 1] in PUNCT:
        return True
    return False


def starts_mid_sentence(doc, chunks, k=3):
    bad, cursor, examples = 0, 0, []
    for c in chunks:
        i = doc.find(c[:20], cursor)
        if i < 0:
            i = doc.find(c[:20])
        if i <= 0:
            continue
        cursor = i
        if not at_boundary(doc, i):
            bad += 1
            if len(examples) < k:
                examples.append((doc[i - 1], c[:34].replace("\n", "⏎")))
    return bad, examples


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    doc_id, path, newjson = sys.argv[1], sys.argv[2], sys.argv[3]
    doc = io.open(path, encoding="utf-8", errors="replace").read()
    old = old_chunks(doc_id)
    new = json.load(io.open(newjson, encoding="utf-8"))

    print(f"原文 {len(doc)} 字")
    for tag, chunks in (("旧（字符硬切）", old), ("新（句子边界）", new)):
        bad, ex = starts_mid_sentence(doc, chunks)
        print(f"\n{tag}：{len(chunks)} 块，长度均 "
              f"{sum(len(c) for c in chunks)//max(len(chunks),1)} 字，"
              f"从句子中间开始 {bad} 个 = {100*bad/max(len(chunks),1):.0f}%")
        for prev, head in ex:
            print(f"    前一字 {prev!r} → {head!r}")


if __name__ == "__main__":
    main()
