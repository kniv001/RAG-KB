# -*- coding: utf-8 -*-
"""
**题目层** —— 题目集只有一种格式，靶子**锚内容不锚位置**。

为什么锚内容（2026-09-20 的教训）：靶子原本按 `(doc, seq)` 存，`seq` 是**位置** ——
语料一重切就指向别的块，**而所有指标不会报错，只会静默变低**。这就是"每次都要调题目"的来源。
改成存**正文摘录**之后，只要那段内容还在语料里，题目就**自动重解析**，重建语料不用动题。

四种靶子形态（覆盖现有全部尺子）：

| kind | 靶子写法 | 用在 |
|---|---|---|
| `chunk` | `{"excerpt": "正文前 24 字"}` | 多跳（原 doc+seq） |
| `doc`   | `{"doc_like": ["HTTP/2", "多路复用"]}` | 单跳 / 难题（按文档名含词） |
| `self`  | `{"self": true}` | 自检索（块自身就是靶子） |
| `pos`   | `{"at": 72}`（must/optional/forbidden） | 分段（另一套判据，见 judges） |

**审计**（`audit`）在每次跑之前自动做，任何一条不过就**大声报出来**：
  ① 语料戳；② 每个靶子能否解析（解析不到 = 这题废，不是 0 分）；
  ③ 解析到几块（多块正常，0 块才是问题）；④ 有没有空组；⑤ 文档名是否存在。
"""
import io
import json
import os

from . import corpus

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
CASE_DIR = os.path.join(TOOLS, "cases")

SCHEMA = "ruler/cases@1"


class Target:
    __slots__ = ("kind", "spec", "group", "why")

    def __init__(self, kind, spec, group, why=""):
        self.kind, self.spec, self.group, self.why = kind, spec, group, why

    def hit(self, picked):
        return any(j in picked for j in self.group)


class CaseSet:
    def __init__(self, data, C):
        self.name = data["name"]
        self.kind = data.get("kind", "retrieval")
        self.judge = data.get("judge", "all-hit")
        self.note = data.get("note", "")
        self.rules = data.get("rules", [])
        self.C = C
        self.cases = data["cases"]
        self.problems = []
        self._resolve()

    # ── 解析 ────────────────────────────────────────────────────────────
    def _resolve(self):
        C = self.C
        docs = C.doc_index()
        for c in self.cases:
            c["_targets"] = []
            for t in c.get("targets", []):
                if "excerpt" in t:
                    g = C.find(t["excerpt"], doc=c.get("doc"))
                    if not g:
                        self.problems.append(
                            f"{c.get('id','?')} 靶子解析不到：「{t['excerpt'][:24]}」"
                            f"（语料里没有这段内容 —— 这题作废，不是 0 分）")
                    c["_targets"].append(Target("chunk", t, g))
                elif "doc_names" in t or "doc_like" in t:
                    if "doc_names" in t:                       # 精确文档名（迁移时已解析好）
                        g = [i for nm in t["doc_names"] for i in docs.get(nm, [])]
                        miss = [nm for nm in t["doc_names"] if nm not in docs]
                        if miss:
                            self.problems.append(
                                f"{c.get('id','?')} 靶子文档不在语料里：{miss}")
                    else:                                      # 名字含这些词（宽松，易漂）
                        g = [i for d, idx in docs.items()
                             if all(w.lower() in d.lower() for w in t["doc_like"]) for i in idx]
                        if not g:
                            self.problems.append(
                                f"{c.get('id','?')} 靶子文档不存在：{'/'.join(t['doc_like'])}")
                    c["_targets"].append(Target("doc", t, g))
                elif t.get("self"):
                    c["_targets"].append(Target("self", t, []))       # 运行期填，见 selfretr
                else:
                    self.problems.append(f"{c.get('id','?')} 靶子形态不认识：{t}")

    # ── 审计 ────────────────────────────────────────────────────────────
    def audit(self, strict=True):
        C = self.C
        lines = [f"题目集 {self.name}（schema {SCHEMA}）· {len(self.cases)} 题 · "
                 f"判据 {self.judge} · {C.line()}"]
        if self.kind == "segmentation":
            # 分段族没有"靶子"，它的标注是 must/optional/forbidden 的**位置**。
            # 这类**不随语料变**（位置是相对于那篇文档的段落序列，不是块的），所以只查编号合法。
            bad = []
            nseg = None
            for c in self.cases:
                if c["lo"] >= c["hi"]:
                    bad.append(f"{c['id']} 窗口 {c['lo']}..{c['hi']} 是空的")
                for x in c["must"] + c["optional"]:
                    if not (c["lo"] <= x <= c["hi"]):
                        bad.append(f"{c['id']} must/optional 点 {x} 落在窗口外")
                for lo, hi in c["forbidden"]:
                    if lo > hi:
                        bad.append(f"{c['id']} forbidden 区间 {lo}..{hi} 反了")
                nseg = max(nseg or 0, c["hi"])
            lines.append(f"  ✓ 窗口 {len(self.cases)} 个，跨度到段 {nseg}（相对那篇文档的段落序列）")
            lines.append(f"  ✓ must {sum(len(c['must']) for c in self.cases)} / "
                         f"optional {sum(len(c['optional']) for c in self.cases)} / "
                         f"forbidden 区间 {sum(len(c['forbidden']) for c in self.cases)}")
            if bad:
                lines.append(f"  ✗ 标注问题 {len(bad)} 条：" + "；".join(bad[:6]))
            if strict and bad:
                raise SystemExit("分段题目集没通过审计 —— 先修标注")
            return "\n".join(lines)
        empty = [c.get("id", "?") for c in self.cases
                 if not c["_targets"] or any(not t.group for t in c["_targets"]
                                             if t.kind != "self")]
        multi = sum(1 for c in self.cases for t in c["_targets"] if len(t.group) > 1)
        dup = [c.get("id", "?") for c in self.cases
               if len({tuple(sorted(t.group)) for t in c["_targets"] if t.group}) <
               len([t for t in c["_targets"] if t.group])]
        if self.problems:
            lines.append(f"  ✗ 解析问题 {len(self.problems)} 条：")
            lines += [f"      {p}" for p in self.problems]
        if empty:
            lines.append(f"  ✗ **空靶子组 {len(empty)} 题**（那题永远不可赢，必须修）：{empty}")
        else:
            lines.append(f"  ✓ 无空靶子组")
        lines.append(f"  ✓ 多块命中 {multi} 处（同段落在相邻块里出现，正常）")
        if dup:
            lines.append(f"  ⚠ 一题内多靶落在同一块（全中率会虚高）：{dup}")
        n_t = sum(len(c["_targets"]) for c in self.cases)
        lines.append(f"  {'✓' if not (self.problems or empty) else '✗'} 靶子 {n_t} 个，"
                     f"可解析 {n_t - sum(1 for c in self.cases for t in c['_targets'] if not t.group)} 个")
        if strict and (self.problems or empty):
            raise SystemExit("题目集没通过审计 —— 先修题目，别跑数（错数比没数坏）")
        return "\n".join(lines)


def names():
    if not os.path.isdir(CASE_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(CASE_DIR) if f.endswith(".json"))


def load(name, C=None, strict=True):
    C = C or corpus.load()
    p = os.path.join(CASE_DIR, name if name.endswith(".json") else name + ".json")
    if not os.path.exists(p):
        raise SystemExit(f"没有这个题目集：{name}（现有：{', '.join(names())}）")
    cs = CaseSet(json.load(io.open(p, encoding="utf-8")), C)
    print(cs.audit(strict=strict))
    return cs


def save(data, name):
    os.makedirs(CASE_DIR, exist_ok=True)
    p = os.path.join(CASE_DIR, name + ".json")
    io.open(p, "w", encoding="utf-8").write(
        json.dumps(data, ensure_ascii=False, indent=1))
    return p
