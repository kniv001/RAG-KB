# -*- coding: utf-8 -*-
"""
**类型判定能不能从昂贵的思考里挪到一次便宜的专用调用？**

背景（2026-09-21）：思考里与输出契约有关的句子占 **20%**（chitchat 题高达 49%），
而契约那张长表模型**每题都要重推一遍**。想把它挪走，前提是**判定本身可靠**。

已经死了两条路（都不是调参问题，是信号结构性地不对）：
  · `contexts.isEmpty()`  —— 检索**从不返回空**（21 题里连库外问题也召回 8~15 段）
  · `assess.enough`       —— **恒为 true**，而且理由常与判定自相矛盾
  两者量的都是"有没有东西可用"，而乙/丙问的是"**知识库覆不覆盖这个主题**"。

**剩下这一条**：模型在思考里其实判得对（基线 21/21 全过），
问题只是"判得贵"。那就单独问一次，看**专用短调用**判得准不准。
准 ⇒ 挪走可行（省下 20% 思考）；不准 ⇒ 这条路死得更深（判定本身就不可靠）。

判据：拿基准的 `kind` 当参照。**grounded-partial 两种出口都算对**
（库里有相关内容但没有那个具体值 ⇒ 判乙或判丙都说得通）。

用法：python tools/eval/category-probe.py
"""
import io
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:4b"
BASE_RUN = os.path.join(HERE, "_runs", "answer-quality__qwen3-4b.json")

# **只问判定，不问别的** —— 与 ANSWER_SYSTEM 里那张表同样的三分法，但要求只输出一个字母。
# 关键差别：这里模型**没有"顺手把答案也写了"的余地**，只能做这一件事。
CAT_SYS = """你是问题分类器。判断用户问题属于哪一类，**只输出一个字母**。

甲 —— 与知识库无关：闲聊、问你的身份、问本次对话之前说过什么、需要靠上下文解析指代。
乙 —— 知识性问题，且【参考资料】里有回答问题所需要的内容。
丙 —— 知识性问题，但【参考资料】里没有回答问题所需要的内容。

**判断一律以【参考资料】为准。**
注意：**"资料里提到了相关话题"不等于"资料能回答这个问题"** ——
若资料只泛泛提到该主题、却给不出问题所要的具体内容，应当判**丙**。

只输出 JSON，不要解释：{"类别":"乙"}"""

_CAT = re.compile(r"[甲乙丙]")


def full_text(C, s):
    i = next((k for k in range(C.n)
              if C.doc[k] == s.get("docName") and C.seq[k] == str(s.get("seq"))), None)
    return C.body[i] if i is not None else (s.get("preview") or "")


def ask(material, question):
    user = material + f"\n【问题】\n{question}"
    body = {"model": MODEL, "stream": False, "think": False,
            "messages": [{"role": "system", "content": CAT_SYS},
                         {"role": "user", "content": user}],
            "format": {"type": "object", "properties": {"类别": {"type": "string"}},
                       "required": ["类别"]},
            "options": {"temperature": 0.0, "num_ctx": 8192}}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    txt = d.get("message", {}).get("content", "")
    try:
        return json.loads(txt).get("类别", "").strip()
    except Exception:
        m = _CAT.search(txt)
        return m.group(0) if m else "?"


# 基准 kind → 期望类别。grounded-partial **两种都算对**（见 docstring）。
EXPECT = {"grounded": {"乙"}, "ungrounded": {"丙"}, "chitchat": {"甲"},
          "grounded-partial": {"乙", "丙"}}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.path.insert(0, TOOLS)
    from ruler import corpus
    C = corpus.load()

    rows = json.load(io.open(BASE_RUN, encoding="utf-8"))["results"]
    ok = 0
    conf = {}
    for x in rows:
        srcs = x.get("sources") or []
        material = ("【参考资料】\n" + "\n".join(
            f"[{k+1}] 来源：{s['docName']}（第 {s['seq']} 块）\n{full_text(C, s)}"
            for k, s in enumerate(srcs))) if srcs else "【参考资料】\n（本次未检索到与问题相关的资料。）\n"
        got = ask(material + "\n", x["q"])
        want = EXPECT[x["kind"]]
        good = got in want
        ok += good
        conf[(x["kind"], got)] = conf.get((x["kind"], got), 0) + 1
        print(f"  {'✅' if good else '❌'} 判{got}　应为{'/'.join(sorted(want))}"
              f"　[{x['kind']}]　{x['q'][:30]}", flush=True)

    n = len(rows)
    print(f"\n**判定准确率 {ok}/{n} = {100*ok/n:.0f}%**")
    print("\n混淆（题型 → 判定）：")
    for k in ("grounded", "grounded-partial", "ungrounded", "chitchat"):
        d = {g: c for (kk, g), c in conf.items() if kk == k}
        print(f"  {k:<17} {d}")


if __name__ == "__main__":
    main()
