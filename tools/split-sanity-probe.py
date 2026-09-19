# -*- coding: utf-8 -*-
"""
二值切分探针的**控制实验**：恒选一侧，到底是模型不会判断，还是这道题本身有病？

可疑的三处（读 `hier-binary-tree-probe.py` 的 `excerpts()` 得到）：
  ① 摘录太薄：每半边只有「开头 2 段 + 末尾 2 段」，各截到 110 字 ——
     让模型对一段它**看不见**的区间判断"最大的一次主题转换在哪一半"
  ② 问题预设了不存在的答案：没有「这里没有转换」这个选项，模型必须选一边
  ③ 摘录构造把"变化"摆在正中：`前半` 的末尾两段紧贴 mid、`后半` 的开头两段紧接 mid，
     模型看到的最显眼的"换了一件事"就在两组交界处 —— 而那既不属于前半也不属于后半

做法：**用边界位置已知的合成区间**去问同一个提示词，四种情形各问 2 次：
  A 转换在前半（前半内部换题，后半全是同一题）  → 正确答案 A
  B 转换在后半（前半全是同一题，后半内部换题）  → 正确答案 B
  C 全程同一题（根本没有转换）                → 没有正确答案，看它怎么答
  D 同 B 的输入，但提示词里把 A/B 的含义**对调**  → 若跟着内容走应答 A；跟着标签走仍答 B

语料取两篇**主题差得最远**的真实文档（Redis 持久化 / Kubernetes 调度），
所以"换了一件事"对人类是一眼可见的 —— 模型若还答不出，就不是"题太难"。

用法：python tools/split-sanity-probe.py
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
BS = chr(92)
CHAT = os.environ.get("KB_MODEL", "qwen3:4b")
_NG = int(os.environ["KB_NUM_GPU"]) if os.environ.get("KB_NUM_GPU") else None


def _opts(temp, ctx):
    o = {"temperature": temp, "num_ctx": int(os.environ.get("KB_NUM_CTX", ctx))}
    if _NG is not None:
        o["num_gpu"] = _NG
    return o


SPLIT_PROMPT = """下面是同一篇文档中一段连续区间的内容摘录，分「前半」和「后半」两组。

请判断：这段区间里**最大的一次主题转换**靠近哪一半？

只输出 JSON：{"half":"A"}

要求：
- "A" 表示转换点在前半，"B" 表示在后半
- 判断依据是「从这里开始讲的东西明显换了一件」，不是文字风格变化
- 两组看起来仍在讲同一件事时，选**内容变化更明显**的那一半
- 不要解释"""

SWAPPED_PROMPT = SPLIT_PROMPT.replace('- "A" 表示转换点在前半，"B" 表示在后半',
                                      '- "A" 表示转换点在**后半**，"B" 表示在**前半**')

# 示例值可以是个坑：提示词里写 `{"half":"A"}`，模型可能**原样抄那个字母**。
# （同一天在 structure-point-probe 上撞实了：示例写 `{"at": 3}` → 五种控制情形**全答 3**，
#  连"没有就答 0"都压不过它。）加 KB_NOEXAMPLE=1 去掉字面示例，只描述形状，用来分辨
#  "模型偏好某个字母" 与 "模型在抄示例"。
if os.environ.get("KB_NOEXAMPLE") == "1":
    for _name in ("SPLIT_PROMPT", "SWAPPED_PROMPT"):
        _p = globals()[_name]
        _p = _p.replace('只输出 JSON：{"half":"A"}',
                        '只输出 half 字段，值是 "A" 或 "B"（按上面两条的含义选一个）')
        globals()[_name] = _p

SPLIT_SCHEMA = {"type": "object", "properties": {"half": {"type": "string"}}, "required": ["half"]}


def psql_rows(sql):
    f = os.path.join(HERE, "_ss.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    out = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        for esc, real in ((BS + BS, BS), (BS + "n", "\n"), (BS + "r", "\r"), (BS + "t", "\t")):
            line = line.replace(esc, real)
        out.append(line)
    return out


def pick_segs():
    """从两篇主题最远的文档各取几段（确定性：按 md5 排序）。"""
    def from_doc(pat):
        return psql_rows(
            "SELECT content FROM chunks WHERE doc_id = ("
            f"SELECT id FROM documents WHERE name ILIKE '%{pat}%' "
            "ORDER BY md5(id::text) LIMIT 1) ORDER BY md5(id::text) LIMIT 3")
    a, b = from_doc("Redis"), from_doc("Kubernetes")
    if len(a) < 3 or len(b) < 3:
        print(f"语料里找不到这两篇（Redis {len(a)} 段 / Kubernetes {len(b)} 段），换个选取条件")
        sys.exit(1)
    return a, b


def ask(system, user):
    body = {"model": CHAT, "stream": False, "think": os.environ.get("KB_THINK") == "1",
            "format": SPLIT_SCHEMA, "options": _opts(0.1, 16384),
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        raw = json.load(r).get("message", {}).get("content", "")
    try:
        return str(json.loads(raw).get("half", "")).strip().upper()[:1] or "?"
    except Exception:
        return "?"


def excerpts_visible(halfA, halfB, n=2):
    """**照抄** hier-binary-tree-probe 的 excerpts()：每半边 = 开头 n 段 + 末尾 n 段。
    这里直接给"可见的 4 段"，等价于那边的 segs[lo..lo+n] + segs[mid-n..mid]。"""
    fmt = lambda xs: "\n".join(f"- {x[:110].replace(chr(10), ' ')}" for x in xs)
    return f"【前半】\n{fmt(halfA[:n] + halfA[-n:])}\n\n【后半】\n{fmt(halfB[:n] + halfB[-n:])}"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    A, B = pick_segs()
    print(f"模型 {CHAT}　think={os.environ.get('KB_THINK') == '1'}\n")
    print("两篇文档（Redis 持久化 / K8s 调度）各取 3 段做素材\n")

    cases = [
        # (标签, 前半可见 4 段, 后半可见 4 段, 正确答案, 用哪个提示词)
        ("A 转换在前半", [A[0], A[1], B[0], B[1]], [B[0], B[1], B[2], B[0]], "A", SPLIT_PROMPT),
        ("B 转换在后半", [A[0], A[1], A[2], A[0]], [A[1], A[2], B[0], B[1]], "B", SPLIT_PROMPT),
        ("C 全程同一题", [A[0], A[1], A[2], A[0]], [A[1], A[2], A[0], A[1]], "（无）", SPLIT_PROMPT),
        ("D 同 B，但标签对调", [A[0], A[1], A[2], A[0]], [A[1], A[2], B[0], B[1]], "A（内容在后半）",
         SWAPPED_PROMPT),
    ]
    for name, fa, fb, want, prompt in cases:
        got = []
        for _ in range(2):
            got.append(ask(prompt, excerpts_visible(fa, fb)))
        print(f"{name:<18} 期望 {want:<12} 实得 {'/'.join(got)}")
        sys.stdout.flush()

    print("\n读法：")
    print("  · A/B 两例全对 ⇒ 方法在**良构输入**上能用，之前的'恒选一侧'是输入病（问题太薄/无解）")
    print("  · A/B 两例仍偏一侧 ⇒ 模型在二选一上就是有位置/标签偏好，这条探针作废")
    print("  · C 例（没有转换）答什么 ⇒ 看它是不是被迫编一个答案（提示词没有'无转换'选项）")
    print("  · D 例跟着内容走（答 A）⇒ 没有标签崇拜；跟着标签走（答 B）⇒ 读到的是位置不是内容")


if __name__ == "__main__":
    main()
