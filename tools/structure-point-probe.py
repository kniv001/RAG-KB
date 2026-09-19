# -*- coding: utf-8 -*-
"""
结构推断第二版问法：**指认式**（给连续片段编号，让它指出"新话题从哪一段开始"，没有就答 0）。

为什么换：第一版（`hier-binary-tree-probe`）问"最大的一次转换靠近前半还是后半"——
控制实验证明那个问法**过不了自检**（`tools/split-sanity-probe.py`）：
没有出口（无转换的输入照样答）、读标签不读内容（A/B 定义对调答案不变）、摘录只有 8 个碎片。

这一版改三件事：
  ① **出口内建**：「都在讲同一件事就答 0」，模型不需要编
  ② **指认而非比较**：给它连续的真实片段，回答是**编号**（指向文本），不是抽象的两半之一
  ③ **片段给全**：每段 200 字，且是**连续的一段**（第一版是"前半开头2段+末尾2段"，把变化摆在正中）

先跑控制实验（植入位置已知的转换），过了再看真文档上的边界质量。
判据与第一版对齐：边界两侧都是代码味 = 切在代码中间。

用法：python tools/structure-point-probe.py [--real]
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
K = 8                      # 每次给几个片段
CHAT = os.environ.get("KB_MODEL", "qwen3:4b")
_NG = int(os.environ["KB_NUM_GPU"]) if os.environ.get("KB_NUM_GPU") else None


def _opts(temp, ctx):
    o = {"temperature": temp, "num_ctx": int(os.environ.get("KB_NUM_CTX", ctx))}
    if _NG is not None:
        o["num_gpu"] = _NG
    return o


# 提示词里**一个数字都不能出现**：第一版写了 `{"at": 3}` 当示例，模型五种控制情形**全答 3**。
# 更坑的是第二版：我把"上一版写了 3 所以它答 3"这句话当成说明**留在了提示词里** ——
# 那个 3 照样在提示词里，结果一点没变。教训要写在代码注释里，不是写给模型看。
PROMPT = """下面是文档里**连续的 {k} 个片段**，按顺序编号 1 到 {k}，每个片段截到 200 字。

请指出：**一个新话题从哪一段开始** —— 判定依据是「从这里开始，讲的东西明显换了一件」。

要求：
- 输出 `at` 字段，值是那一段的编号（1 到 {k} 之间的整数）
- **新话题可能从很靠前的地方就开始**（比如第 2、3 段）—— 不要因为后面多数片段都在讲另一件事，
  就认为"没有转换"；那是本题最常见的错法
- 只有当**确实找不到任何一处**「讲的东西换了一件」时才写 0（那是最后手段，不是默认答案）
- 片段被截断过，判断只依据看到的内容
- 不要解释"""

SCHEMA = {"type": "object", "properties": {"at": {"type": "integer"}}, "required": ["at"]}

CJK = re.compile(r"[\u4e00-\u9fff]")
CODEY = re.compile(r"[{}();=<>\[\]]|^\s*(public|private|import|class|def|SELECT|while|for|if)\b",
                   re.MULTILINE)


def psql_rows(sql):
    f = os.path.join(HERE, "_sp.sql")
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


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ask(segs):
    body = {"model": CHAT, "stream": False, "think": os.environ.get("KB_THINK") == "1",
            "format": SCHEMA, "options": _opts(0.1, 16384),
            "messages": [{"role": "system", "content": PROMPT.format(k=len(segs))},
                         {"role": "user",
                          "content": "\n".join(f"{i+1}. {s[:200].replace(chr(10), ' ')}"
                                               for i, s in enumerate(segs))}]}
    with urllib.request.urlopen(urllib.request.Request(
            OLLAMA + "/api/chat", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}), timeout=900) as r:
        raw = json.load(r).get("message", {}).get("content", "")
    try:
        v = json.loads(raw).get("at")
        return int(v) if isinstance(v, (int, float)) else -1
    except Exception:
        return -1


def codey(seg):
    cjk = len(CJK.findall(seg))
    return (cjk / max(len(seg), 1) < 0.35) or bool(CODEY.search(seg))


def controls(A, B):
    """植入位置已知的转换：答案必须**跟着植入位置走**，无转换时必须答 0。

    **两版都栽过的地方**：
      ① 素材要用**连续段落**（按 seq）。第一版拿的是 `ORDER BY md5(id)` 随机挑的块 ——
         8 个互不相干的片段摆在一起，"哪一段是新话题"根本没有定义（每段都是），
         控制实验本身不成立。
      ② 提示词里不能出现任何示例数字（写了 `{"at": 3}` 就五次全答 3）。
    """
    R, Kk = A, B      # 各自 8 段连续段落
    print("—— 控制实验（转换位置已知，答案要跟着走）——")
    cases = [
        ("C1 接缝在第 3 段", R[:2] + Kk[:6], 3),
        ("C2 接缝在第 6 段", R[:5] + Kk[:3], 6),
        ("C3 全程同一篇（无接缝）", R[:8], 0),
        ("C4 接缝就在第 2 段", R[:1] + Kk[:7], 2),
        ("C5 反向：先 K8s 后 Redis，接缝在第 6 段", Kk[:5] + R[:3], 6),
    ]
    ok = near = 0
    for name, segs, want in cases:
        got = [ask(segs) for _ in range(2)]
        hit = sum(1 for g in got if g == want)
        # ±1 算「检出」：分段任务里差一格与"没找到"是两回事（后者才致命）
        near += sum(1 for g in got if want != 0 and abs(g - want) == 1)
        ok += hit
        print(f"  {name:<34} 期望 {want}　实得 {'/'.join(str(g) for g in got)}　"
              f"{'✅' if hit == 2 else ('◐' if hit == 1 or near else '❌')}")
        sys.stdout.flush()
    print(f"  精确 {ok}/{len(cases)*2}　±1 检出再加 {near} 次")
    return ok + near, len(cases) * 2


def real(segs):
    """真文档：滑动窗口走一遍，收集「新话题开始」的位置。"""
    print("\n—— 真文档：滑窗指认（窗口 8 段，命中就跳过窗口，否则前进 4 段）——")
    ats, bounds, i = [], [], 0
    while i + K <= len(segs):
        at = ask(segs[i:i + K])
        ats.append(at)
        if 1 <= at <= K:
            j = i + at - 1
            if j > 0:
                bounds.append(j)
            i = i + at          # 跳过这个转换点，接着找下一个
        else:
            i += 4
    print(f"  调用 {len(ats)} 次，边界 {len(bounds)} 个")
    from collections import Counter
    print(f"  回答分布：{dict(sorted(Counter(ats).items()))}"
          f"　{'⚠️ 常数' if len(set(ats)) == 1 else ''}")
    bad = [j for j in bounds if 0 < j < len(segs) and codey(segs[j - 1]) and codey(segs[j])]
    print(f"  两侧都是代码味的边界：**{len(bad)}/{len(bounds)}**"
          f"（第一版二值递归：37/65 = 57%）")
    for j in bad[:6]:
        print(f"    ✗ 段{j}→{j+1}: …{segs[j-1][-38:].strip()}  ||  {segs[j][:38].strip()}…")
    print("\n  前 3 个边界两侧原文：")
    for j in bounds[:3]:
        print(f"    …{segs[j-1][-44:].strip()}")
        print(f"     {segs[j][:44].strip()}…")


def scan_window(segs, lo, hi):
    """在 [lo,hi) 段内滑窗指认，返回切点（1-based 段号）。与 real() 同一套推进规则。"""
    ats, bounds, i = [], [], lo
    while i + K <= hi:
        at = ask(segs[i:i + K])
        ats.append(at)
        if 1 <= at <= K:
            j = i + at - 1
            if j > lo:
                bounds.append(j + 1)      # 1-based
            i = i + at
        else:
            i += 4
    return bounds, ats


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--window" in sys.argv:
        k = sys.argv.index("--window")
        lo, hi = int(sys.argv[k + 1]), int(sys.argv[k + 2])
        out = sys.argv[k + 3] if len(sys.argv) > k + 3 else "tools/_bounds.json"
        cache = os.path.join(HERE, "..", "data", "_bintree-segs.json")
        segs = json.load(io.open(cache, encoding="utf-8"))["segs"]
        b, ats = scan_window(segs, lo, hi)
        io.open(out, "w", encoding="utf-8").write(
            json.dumps({"model": CHAT, "window": [lo, hi], "bounds": b, "answers": ats},
                       ensure_ascii=False, indent=1))
        print(f"窗口 {lo+1}-{hi}　调用 {len(ats)} 次　切点 {len(b)} 个 → {out}")
        print(f"回答分布 {dict(sorted(__import__('collections').Counter(ats).items()))}")
        return
    def from_doc(pat, n):
        """**按 seq 取连续段落** —— 随机挑块会让"哪一段是新话题"失去定义（控制实验因此不成立过一版）。
        文档取**匹配里块数最多的那篇**，否则可能挑到只有几块的短篇。"""
        return psql_rows(
            f"SELECT content FROM chunks WHERE doc_id = ("
            f"SELECT id FROM documents WHERE name ILIKE '%{pat}%' "
            f"ORDER BY (SELECT count(*) FROM chunks c WHERE c.doc_id = documents.id) DESC "
            f"LIMIT 1) ORDER BY seq LIMIT {n}")
    A, B = from_doc("Redis", 8), from_doc("Kubernetes", 8)
    if len(A) < 8 or len(B) < 8:
        print(f"素材不足（Redis {len(A)} / K8s {len(B)}，各需 8 段连续段落）")
        sys.exit(1)
    print(f"模型 {CHAT}　think={os.environ.get('KB_THINK') == '1'}　窗口 {K} 段\n")
    ok, total = controls(A, B)
    if ok < 8:      # 植入接缝 4 处（8 次）差一格也算检出；只放过 ≤1 次误报
        print("\n  ⇒ 控制实验没过，**先别拿它测真文档**（这正是第一版栽的地方）")
        return
    if "--real" in sys.argv:
        cache = os.path.join(HERE, "..", "data", "_bintree-segs.json")
        if not os.path.exists(cache):
            print("\n（没有 data/_bintree-segs.json，先跑一次 hier-binary-tree-probe 生成）")
            return
        real(json.load(io.open(cache, encoding="utf-8"))["segs"])
    else:
        print("\n（控制实验通过。加 --real 再跑真文档）")


if __name__ == "__main__":
    main()
