# -*- coding: utf-8 -*-
"""
**一次性迁移**：把散在各脚本里的题目集，转成 `tools/cases/*.json` 的锚内容格式。

原则：**趁 `seq` 现在还有效，把内容抓下来存住**。之后再重建语料，题目自动重解析，不用再动。
每转一条都**当场核对**（seq 指到的块与旧指纹是否一致），不一致就报出来 —— 不猜。

转换对照：
  multihop-questions.json   (doc, seq, fp)      →  {excerpt: 正文前 24 字}
  recall-baseline-probe     (主题, 问法)         →  {doc_names: [...]}（用 manifest 解析）
  hard-query-probe          (主题, 自带词, 难题)  →  {doc_names: [...]}
  _selfret_questions.json   (qi, qs)            →  {excerpt: 那句问话本身}（它逐字来自块内）
  seg-ruler-annotation.json (windows, must...)  →  kind=segmentation（另一族判据）
"""
import io
import json
import os

from . import cases, corpus

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    return json.load(io.open(os.path.join(TOOLS, name), encoding="utf-8"))


def _manifest_topics():
    m = _read(os.path.join("..", "data", "_grown-docs.json"))
    t = {}
    for d in m["docs"]:
        if d.get("topic"):
            t.setdefault(d["topic"], []).append(d["name"])
    return t


def multihop(C):
    src = _read("multihop-questions.json")
    out, bad = [], []
    for n, c in enumerate(src["cases"], 1):
        ts = []
        for t in c["targets"]:
            i = next((k for k in range(C.n)
                      if C.doc[k] == c["doc"] and C.seq[k] == str(t["seq"])), None)
            if i is None:
                bad.append(f"{c['q'][:24]} seq={t['seq']} 指不到块")
                continue
            head, old = C.head(i), corpus.norm(t.get("fp", ""))
            if old and not head.startswith(old[:12]):
                bad.append(f"{c['q'][:24]} seq={t['seq']} 指纹不符："
                           f"旧「{old[:16]}」现「{head[:16]}」")
            ts.append({"excerpt": head, "seq_hint": t["seq"]})
        out.append({"id": f"mh{n:02d}", "doc": c["doc"], "q": c["q"], "targets": ts})
    return {"schema": cases.SCHEMA, "name": "multihop-25", "kind": "retrieval",
            "judge": "all-hit",
            "note": src.get("note", "") + "　靶子已改为锚内容（excerpt），seq 只作提示。",
            "cases": out}, bad


def _resolve_doc(C, nm, allnames):
    """manifest 里的名字**被截断到 60 字**，要和库里的真名对齐 —— 按前缀解析，多解就报错。"""
    if nm in allnames:
        return nm, None
    cand = sorted({d for d in allnames if d.startswith(nm)})
    if len(cand) == 1:
        return cand[0], None
    return None, f"名字对不上库里的文档（候选 {len(cand)} 个）：{nm[:40]}"


def _two_col_file(C, fname, idx_q, kind, name, note):
    """single-hop / hard-query 共用：CASES 里的元组 + manifest 的主题→文档名。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_m", os.path.join(TOOLS, fname))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    topics = _manifest_topics()
    allnames = set(C.doc)
    out, bad = [], []
    for n, row in enumerate(m.CASES, 1):
        topic = row[0]
        names, miss = [], []
        for nm in topics.get(topic, []):
            real, err = _resolve_doc(C, nm, allnames)
            (names.append(real) if real else miss.append(err or nm))
        if not topics.get(topic):
            bad.append(f"主题「{topic}」在 manifest 里没有对应文档")
        bad += [f"{topic[:20]}：{x}" for x in miss]
        out.append({"id": f"{kind[:2]}{n:02d}", "q": row[idx_q],
                    "targets": [{"doc_names": names}],
                    "topic": topic, "cat": (row[3] if len(row) > 3 else None)})
    return {"schema": cases.SCHEMA, "name": name, "kind": "retrieval",
            "judge": "all-hit", "note": note, "cases": out}, bad


def single_hop(C):
    return _two_col_file(C, "recall-baseline-probe.py", 1, "sh", "single-hop-15",
                         "单跳基准：**自带词**的自然提问（15/15 是必然的，词面能对上）。"
                         "靶子 = 该主题对应的那几篇文档。")


def hard_query(C):
    return _two_col_file(C, "hard-query-probe.py", 2, "hq", "hard-query-15",
                         "难题集：**刻意不带原文词**（口语化/场景式/同义/比较式）。"
                         "靶子 = 该主题对应的那几篇文档。")


def selfretrieval(C):
    """靶子**不是问句**（问句是模型改写的，逐字找不到），而是**问句的来源段落**。

    `_selfret_questions.json` 里 `qi` 是 `data/_bintree-segs.json` 的段落号 ——
    实测 23/23 都能按段落正文定位到块，所以自检索也能锚内容。
    """
    src = _read("_selfret_questions.json")
    segs = json.load(io.open(os.path.join(TOOLS, "..", "data", "_bintree-segs.json"),
                             encoding="utf-8"))
    segs = segs if isinstance(segs, list) else segs.get("segs")
    out, bad = [], []
    for n, (qi, q) in enumerate(zip(src["qi"], src["qs"]), 1):
        para = segs[qi] if qi < len(segs) else ""
        g = C.find(para[:C.EXCERPT]) if para else []
        if not g:
            bad.append(f"来源段落定位不到块：段 {qi}「{para[:20]}」")
        out.append({"id": f"sr{n:02d}", "q": q, "seg": qi,
                    "targets": [{"excerpt": corpus.norm(para)[:24]}] if g else []})
    return {"schema": cases.SCHEMA, "name": "selfretrieval-23", "kind": "retrieval",
            "judge": "self-hit",
            "note": "自检索（提问版）：问句是模型**改写**的（不逐字来自块内），"
                    "靶子锚的是**问句的来源段落**。问的是「切块切得好不好」——"
                    "**排第一才算好**（与多跳不同，不要求全中）。",
            "cases": out}, bad


def segmentation(C):
    src = _read("seg-ruler-annotation.json")
    out = []
    for n, w in enumerate(src["windows"], 1):
        out.append({"id": f"sg{n:02d}", "lo": w["lo"], "hi": w["hi"],
                    "must": [x["at"] for x in w.get("must", [])],
                    "optional": [x["at"] for x in w.get("optional", [])],
                    # forbidden 是**区间**不是点：{lo, hi, why}（代码/日志碎片内部）
                    "forbidden": [[x["lo"], x["hi"]] for x in w.get("forbidden", [])],
                    "why": [x.get("why", "") for x in w.get("must", [])]})
    return {"schema": cases.SCHEMA, "name": "segmentation-10", "kind": "segmentation",
            "judge": "windowdiff", "doc": src["doc"], "seg_file": src["seg_file"],
            "rules": src.get("rules", []), "note": src.get("note", ""),
            "cases": out}, []


def run(C=None):
    C = C or corpus.load()
    os.makedirs(cases.CASE_DIR, exist_ok=True)
    print(C.line() + "\n")
    for fn, maker in [("multihop-25", multihop), ("single-hop-15", single_hop),
                      ("hard-query-15", hard_query), ("selfretrieval-23", selfretrieval),
                      ("segmentation-10", segmentation)]:
        data, bad = maker(C)
        p = cases.save(data, fn)
        print(f"{fn:<18} {len(data['cases']):>3} 题 → {os.path.basename(p)}")
        for b in bad:
            print(f"    ⚠ {b}")
    print("\n迁移完成。之后语料重建不用再动题目 —— 靶子按内容自动重解析。")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ruler import migrate
    migrate.run()
