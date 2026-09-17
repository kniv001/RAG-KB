# -*- coding: utf-8 -*-
"""
句子边界分块能不能治弃抽。

诊断结论：可复现弃抽的块，共同特征是从文章中间硬切、首尾都是半句。
现有切分是 600 字定长硬切（chunk.size 600 / overlap 80），不看句子边界。

本探针在原文档上做对照：同一个区域，一份按现状硬切的原文，一份按句子边界重切，
各抽 3 遍，比空率。抽得出来就说明根因确实是切分，而不是提示词或模型。

用法：python tools/sentence-chunk-probe.py
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
STORAGE = r"D:\vs\rag-kb"
HERE = os.path.dirname(os.path.abspath(__file__))
SQL_FILE = os.path.join(HERE, "_sc.sql")
CHAT, TRIES = "qwen3:4b", 3
SIZE, OVERLAP = 600, 80

PROMPT = """把下面这段资料拆成若干条**自足的事实命题**。

硬性要求：
1. 每条命题必须脱离上下文就能读懂 —— 不许出现「它」「该」「此」「上述」「前者」这类指代，
   指代对象要写出来。
2. 只写资料里说过的事实。**不许补充资料之外的任何内容，不许推论**。
   资料里没有的数字、结论、因果关系一律不得出现。
3. 一条命题只讲一件事。同一条事实被反复说，只保留一次。
4. 保持原有术语与数字，不要改写数值。"""
SCHEMA = {"type": "object",
          "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
          "required": ["facts"]}


def psql(sql):
    with open(SQL_FILE, "w", encoding="utf-8") as f:
        f.write(sql)
    env = dict(os.environ)
    env["PGPASSWORD"] = open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    p = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                        "-t", "-A", "-F", "\t", "-f", SQL_FILE], capture_output=True, env=env)
    return p.stdout.decode("utf-8", "replace")


def post(path, body, timeout=300):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def extract(text):
    r = post("/api/chat", {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
                           "options": {"temperature": 0.1, "num_ctx": 8192},
                           "messages": [{"role": "system", "content": PROMPT},
                                        {"role": "user", "content": text}]})
    c = r.get("message", {}).get("content", "")
    try:
        return len(json.loads(c).get("facts", []))
    except Exception:
        m = re.search(r'"facts"\s*:\s*\[(.*?)\]', c, re.S)
        return len(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))) if m else 0


def hard_split(text, size=SIZE, overlap=OVERLAP):
    step = max(size - overlap, 1)
    return [text[i:i + size] for i in range(0, len(text), step) if text[i:i + size].strip()]


def sent_split(text, target=SIZE, overlap=OVERLAP):
    """先切句，再按目标字数把整句拼块：块首尾一定落在句子边界上"""
    sents = [s for s in re.split(r"(?<=[。！？!?；;])\s*|\n+", text) if s.strip()]
    out, buf = [], ""
    for s in sents:
        if len(buf) + len(s) > target and buf:
            out.append(buf)
            # 重叠：把上一块末尾的整句带到下一块开头
            keep, k = "", 0
            for prev in reversed(out[-1].split("\n")):
                if k >= overlap:
                    break
                keep = prev + "\n" + keep
                k += len(prev)
            buf = keep if keep.strip() else ""
        buf += s + "\n"
    if buf.strip():
        out.append(buf)
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # 取那几段可复现弃抽的块，以及它所属文档的落盘路径
    rows = psql("SELECT c.id, c.doc_id, c.seq, c.content, d.stored_path, d.name "
                "FROM chunks c JOIN documents d ON d.id = c.doc_id "
                "WHERE c.content LIKE 'rk发生时%' OR c.content LIKE '1%%以下的判断失误率%' "
                "   OR c.content LIKE 'ns:0 carrier:0%';")
    cases = []
    for line in rows.splitlines():
        p = line.split("\t")
        if len(p) >= 5:
            cases.append({"cid": p[0], "doc": p[1], "seq": int(p[2]), "text": p[3],
                          "path": p[4], "name": p[5]})
    print(f"可复现弃抽的块：{len(cases)} 段\n")

    for c in cases:
        full = os.path.join(STORAGE, c["path"].replace("/", os.sep))
        try:
            doc = open(full, encoding="utf-8", errors="replace").read()
        except Exception as e:
            print(f"— {c['name'][:30]}：读不到原文（{e}）")
            continue
        # 找到原块在全文中的位置，取它所在的那一段区域
        probe = c["text"][:30]
        at = doc.find(probe)
        if at < 0:
            print(f"— {c['name'][:30]}：原文里找不到该块（可能是网页正文提取后的文本）")
            continue
        region = doc[max(0, at - 200): at + len(c["text"]) + 200]

        hard = [x for x in hard_split(region) if probe[:20] in x]
        soft = [x for x in sent_split(region) if probe[:20] in x]
        hard_s = hard[0] if hard else region[:SIZE]
        soft_s = soft[0] if soft else region[:SIZE]

        print(f"—— {c['name'][:34]}（块 {c['cid'][-6:]}）——")
        print(f"  硬切片段头: {hard_s[:34]!r}")
        print(f"  句切片段头: {soft_s[:34]!r}")
        for tag, s in (("硬切", hard_s), ("句切", soft_s)):
            ns = [extract(s) for _ in range(TRIES)]
            empt = sum(1 for n in ns if n == 0)
            print(f"  {tag}：三遍条数 {ns}　空 {empt}/{TRIES}")
        print()


if __name__ == "__main__":
    main()
