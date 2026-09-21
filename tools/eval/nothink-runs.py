# -*- coding: utf-8 -*-
"""
**拿刚做好的尺子量 no-think 那条路**：它比 think 路径掉多少。

怎么做到可比：**资料与判据都不动，只换生成路径**。
  · 资料：直接取自基线那次采集里每题**真的召回到的那批**（同一批块、同一套编号）
  · 判据：`tools/eval/judges.py` 一个字不改
  · 生成：`think:false` + `format` 信封（`answer` / `general` 分工）

**要说清的一点**：**系统提示词与基线不同** —— 基线用应用里的 `ANSWER_SYSTEM`（三段式），
这里用分工版提示词（`SPLIT_SYS`）。这个差别**是干预本身的一部分**（分工就是靠提示词+schema
实现的），不是可以抹平的变量。所以下面那句"与基线同一份"只对**资料与判据**成立。

三个条件缺一不可（都是实测出来的）：
  · `think:false` —— 关掉思考通道（循环没有发生的地方）
  · `format` 包成 JSON 信封 —— 不加约束的话，推理会从 thinking 通道**转进 content**
  · **`minLength` 下限** —— 没有它就交最省力的合法输出：**把问题原样抄回来**（26 字）

用法：python tools/eval/nothink-runs.py [题数，默认全部]
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
VARIANT = os.environ.get("KB_NOTHINK_VARIANT", "v1")
OUT = os.path.join(HERE, "_runs",
                   f"answer-quality__qwen3-4b-nothink-{VARIANT}.json")

# **两种取答形态**（用户 2026-09-21 要试第 2 种）：
#   v1 有 `minLength: 100` —— 逼它凑字数 ⇒ 实测**把系统提示词抄进答案**（4/21 = 19%）
#   v2 **不约束长度** —— 探针里这样会交最省力的合法输出（26 字抄回问题），
#      但那是 **6 段短材料**上的读数；**真实是 24 段**，不能外推，所以要在全套上重测
SCHEMAS = {
    "v1": {"type": "object",
           "properties": {"answer": {"type": "string", "minLength": 100},
                          "general": {"type": "array", "items": {"type": "string"}}},
           "required": ["answer", "general"]},
    "v2": {"type": "object",
           "properties": {"answer": {"type": "string"},
                          "general": {"type": "array", "items": {"type": "string"}}},
           "required": ["answer"]},
    # v3 = 复活条件③：**材料先由代码压到几段**
    # （探针的条件是 6 段 / ~600 字，那是唯一能让这条路工作的条件）
    "v3": {"type": "object",
           "properties": {"answer": {"type": "string", "minLength": 100},
                          "general": {"type": "array", "items": {"type": "string"}}},
           "required": ["answer", "general"]},
}
SCHEMA = SCHEMAS[VARIANT]

SPLIT_SYS = """你是个人知识库助手。严格依据【参考资料】回答问题。

**回答分成两部分，各放各的：**
- `answer`：**只能写【参考资料】里有的内容**，而且要用自己的话组织，**不要整句照抄**。
  引用处用 [编号] 标注来源。资料里没有的数字、配置项名、命令，一律**不要**写进 answer。
- `general`：资料里没有、但你认为对用户有用的补充（你的通用知识），一条一句放这里。
  没有就留空数组。**不要**把资料里的内容搬到这里。

若是闲聊/身份/与知识库无关的问题，直接写在 answer 里即可，不要提"知识库中没有"。
只输出 JSON。"""


def answer_system():
    """从 Java 源码里取 ANSWER_SYSTEM —— **与基线用同一份**，否则不可比。"""
    p = os.path.join(os.path.dirname(TOOLS), "rag-kb-service/src/main/java/com/kniv/ragkb/service/agent/AgenticRagService.java")
    s = io.open(p, encoding="utf-8").read()
    m = re.search(r'ANSWER_SYSTEM\s*=\s*"""(.*?)"""', s, re.S)
    return "\n".join(l.strip() for l in m.group(1).splitlines()) if m else ""



# ── v3：**材料由代码压缩** ────────────────────────────────────────────────
# 探针的条件（6 段 / ~600 字）唯一能让 no-think 工作的原因，很可能是**材料短**。
# 那就用代码把它压短 —— 而**不是删内容**：压成信息密度高的形式。
#   · 每段保留：**语境行**（「本段可回答什么」，库里本来就有）+ **关键句**（代码抽取）
#   · 段数封顶（取检索序前 N 段）
# 关键句的取法是纯机械的：块内与问题词重叠最高的那一句（不调模型）。
STOP = set("的了吗呢和与及或在是有为对从把被这那你我他它一个如何什么怎么哪些为什么"
           "么样可以需要应该会能要不")


def key_sentence(text, question, maxlen=80):
    qs = {c for c in re.sub(r"\s+", "", question) if c not in STOP}
    sents = [x.strip() for x in re.split(r"[。；\n]]", text) if len(x.strip()) >= 8]
    if not sents:
        return text[:maxlen]
    best = max(sents, key=lambda x: len({c for c in re.sub(r"\s+", "", x) if c not in STOP} & qs))
    return best[:maxlen]


def compress(corpus, srcs, question, topn=6):
    """→ (压缩后的材料文本, 保留的那几条 source)。**返回的 sources 要用在判分里** ——
    否则判据会以为模型看到了 24 段，而它只看到 6 段（cites_valid 会松掉）。"""
    keep = srcs[:topn]
    lines = [f"【资料（系统已按问题筛选压缩，共 {len(keep)} 段；引用用这些编号）】"]
    for k, s in enumerate(keep, 1):
        body = corpus_body(corpus, s)
        # **ctx 要从语料里查**：客户端拿到的 sources 里没有它（那是库里的一列）
        i = next((j for j in range(corpus.n)
                  if corpus.doc[j] == s.get("docName") and corpus.seq[j] == str(s.get("seq"))), None)
        ctx = corpus.ctx[i] if i is not None else ""
        lines.append(f"[{k}] {s['docName']}（第 {s['seq']} 块）"
                     + (f"｜本段可回答：{ctx}" if ctx else ""))
        lines.append(f"    关键句：{key_sentence(body, question)}")
    return "\n".join(lines) + "\n", keep


def corpus_body(C, s):
    i = next((k for k in range(C.n)
              if C.doc[k] == s.get("docName") and C.seq[k] == str(s.get("seq"))), None)
    return C.body[i] if i is not None else (s.get("preview") or "")

def full_text(src):
    sys.path.insert(0, TOOLS)
    from ruler import corpus
    C = corpus.load()
    i = next((k for k in range(C.n)
              if C.doc[k] == src.get("docName") and C.seq[k] == str(src.get("seq"))), None)
    return C.body[i] if i is not None else (src.get("preview") or "")


def chat(system, user, timeout=900):
    body = {"model": MODEL, "stream": False, "think": False,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "format": SCHEMA, "options": {"temperature": 0.2, "num_ctx": 8192}}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    try:
        j = json.loads(d["message"]["content"])
    except Exception:
        j = {"answer": d["message"]["content"], "general": []}
    return j, d.get("eval_count", 0)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    base = json.load(io.open(BASE_RUN, encoding="utf-8"))
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    cases = base["results"][:n] if n else base["results"]
    SYS = answer_system()
    print(f"跑 {len(cases)} 题（no-think + 分工）· 资料取自基线那次采集\n")

    results, toks = [], []
    for i, r in enumerate(cases, 1):
        srcs = r.get("sources") or []
        material = "【参考资料】\n" + "\n".join(
            f"[{k+1}] 来源：{s['docName']}（第 {s['seq']} 块）\n{full_text(s)}"
            for k, s in enumerate(srcs))
        if not srcs:
            material = "【参考资料】\n（本次未检索到与问题相关的资料。）\n"
        try:
            j, tk = chat(SPLIT_SYS, material + f"\n【问题】\n{r['q']}")
        except Exception as e:
            # **逐题容错**：一题超时不该让整批白跑。
            # 实测踩过两次 —— 输出是最后才写的，前面跑的全丢。
            print(f"  {i:>2}. ✗ {type(e).__name__}: {str(e)[:40]}　{r['q'][:26]}", flush=True)
            results.append({**{k: r[k] for k in ("id", "kind", "q") if k in r},
                            "answer": "", "thinking": "", "ttftMs": 0, "ms": 0,
                            "sources": srcs, "error": str(e)[:80]})
            json.dump({"bench": "answer-quality", "model": MODEL + f" (no-think+split {VARIANT})",
                       "at": "no-think", "results": results},
                      io.open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            continue
        toks.append(tk)
        json.dump({"bench": "answer-quality", "model": MODEL + f" (no-think+split {VARIANT})",
                   "at": "no-think", "results": results},
                  io.open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        ans = j.get("answer", "")
        gen = j.get("general") or []
        # **按应用会渲染的样子拼**（判据看的是用户看到的东西）
        rendered = ans + ("\n\n以下为通用知识，未引用你的知识库：\n"
                          + "\n".join(f"- {g}" for g in gen) if gen else "")
        # 用基线那次的同一条 system 也记一下，便于以后核对
        results.append({**{k: r[k] for k in ("id", "kind", "q") if k in r},
                        "answer": rendered, "thinking": "", "ttftMs": 0,
                        "ms": 0, "sources": srcs, "error": None})
        print(f"  {i:>2}. {tk:>5} tok　answer {len(ans):>4} 字　general {len(gen)} 条　{r['q'][:26]}",
              flush=True)

    json.dump({"bench": "answer-quality", "model": MODEL + f" (no-think+split {VARIANT})",
               "at": "no-think", "results": results},
              io.open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\ntoken 中位 {sorted(toks)[len(toks)//2]}　→ {OUT}")
    print("（**资料与判据**与基线同一份；**系统提示不同** —— 分工就是靠提示词+schema 实现的）")


if __name__ == "__main__":
    main()
