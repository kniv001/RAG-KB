# -*- coding: utf-8 -*-
"""**改判据之前，先看它会翻转多少历史结论**。

## 为什么必须有它

判据是量答案价值的那把尺子 —— **改尺子比改管线危险**：管线改错了，读数会动；
尺子改错了，**读数不动而结论悄悄变了**，而且旧结论还留在台账里与新的不可比。

项目里已经形成过这个动作（`_MISSING` 扩词表那次：普查 1327 条历史答案 ⇒
**结论翻转 0 条** ⇒ "这次改动不改变任何结论，它改的是**诊断行是否可信**"）。
本工具把它做成一条命令。

## 用法

    python tools/eval/judge-diff-probe.py HEAD          # 与 HEAD 版本的 judges.py 比
    python tools/eval/judge-diff-probe.py HEAD~1

对每份落盘、每题：用**旧判据**与**新判据**各判一次，报出**翻转的题**与
**分判据的翻转数**（`走对了出口` / `引用全对` / `未标引用值` 这些）。

读法：
  · 翻转全落在"判据自己错"的那一类 ⇒ 改动是**修仪器**
  · 出现**任何一处"新判据把真缺陷放过去"** ⇒ 改动是**放松**，要重新考虑
"""
import glob
import io
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    rev = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    src = subprocess.run(["git", "show", f"{rev}:tools/eval/judges.py"],
                         cwd=REPO, capture_output=True, text=True, encoding="utf-8")
    if src.returncode != 0:
        raise SystemExit(f"取不到 {rev} 版的 judges.py：{src.stderr[:200]}")
    tmp = os.path.join(REPO, "data", "_judges_old.py")
    io.open(tmp, "w", encoding="utf-8").write(src.stdout)

    old = load(tmp, "judges_old")
    from eval import judges as new

    # 判据名单从两边各取一遍 —— 新加的判据也要能被看见
    names = sorted(set(old.PASS) | set(new.PASS))
    rows_by_key = {}
    flips, percrit, n_items, n_runs = [], {}, 0, 0

    files = []
    for b in ("answer-quality", "news-live"):
        files += sorted(glob.glob(os.path.join(HERE, "_runs", f"{b}__*.json")))
    for f in files:
        if f.endswith(".polluted"):
            continue
        try:
            d = json.load(io.open(f, encoding="utf-8"))
        except Exception:
            continue
        n_runs += 1
        for x in (d.get("results") or []):
            kind = x.get("kind")
            ans = x.get("answer") or ""
            if not ans.strip() or kind not in new.PASS:
                continue
            n_items += 1
            args = (kind, ans, x.get("sources") or [], x.get("q") or "",
                    (x.get("stats") or {}).get("cites"))
            try:
                ro, rn = old.judge(*args), new.judge(*args)
            except Exception as e:
                continue
            key = (os.path.basename(f), x.get("id"))
            for c in set(list(ro) + list(rn)):
                if isinstance(ro.get(c), bool) or isinstance(rn.get(c), bool):
                    if ro.get(c) != rn.get(c):
                        percrit[c] = percrit.get(c, 0) + 1
                        flips.append((key, c, ro.get(c), rn.get(c)))
            rows_by_key[key] = (ro, rn, kind)

    print(f"比对 {rev} 与当前：扫 {n_runs} 份落盘 / {n_items} 条结果\n")
    if not flips:
        print("✅ **翻转 0 处** —— 这次改判据不改变任何历史结论（只让读数更准）。")
        return 0
    print("分判据的翻转数：")
    for c, n in sorted(percrit.items(), key=lambda x: -x[1]):
        print(f"  {c:<14}{n:>4}")
    print(f"\n逐处（最多列 25）：")
    for (fn, i), c, a, b in flips[:25]:
        print(f"  {str(i):<8}{c:<14}{a} → {b}    {fn[:44]}")
    ids = sorted({i for (_, i), _, _, _ in flips})
    print(f"\n**翻转只涉及 {len(ids)} 道题**：{'、'.join(str(i) for i in ids)}")
    print(f"共 {len(flips)} 处翻转。**逐条看方向**：")
    print("  · False→True = 旧判据冤枉了它（改对了）")
    print("  · True→False = 新判据开始拒绝它（**要警惕：这是收紧，可能把合法答案判挂**）")
    up = sum(1 for _, _, a, b in flips if (a, b) == (False, True))
    dn = sum(1 for _, _, a, b in flips if (a, b) == (True, False))
    print(f"  实测方向：False→True {up} 处、True→False {dn} 处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
