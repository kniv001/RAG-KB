# -*- coding: utf-8 -*-
"""**待办 ⑧「相邻块补全」的离线预量** —— 在花一小时跑 A/B 之前，先问一句：
邻居块里**到底有没有**活得过材料地板的句子？

## 为什么要先离线量

真跑一遍三臂 A/B 是 **3 臂 × 21 题 × 2 遍 ≈ 126 次生成 ≈ 1 小时**，而它要回答的问题
其实分两半：
  · **材料那一半**（邻居块有没有带来地板之上的句子）—— 纯检索/相似度，不需要模型
  · **答案那一半**（模型会不会因此答得更好）—— 需要真跑
先量材料那一半。若答案是"几乎从不带来"，整条 ⑧ 就该先停，别烧那一小时。

## 口径（**必须写清楚，否则数字没意义**）

锚点 = 该题**落盘结果里的 `sources`**（`docId` + `seq`）—— 那是**过滤之后**的那几块。
⚠️ 生产里 `expandNeighbors` 跑在**过滤之前**，锚点是**过滤前**那 8~17 块 ⇒
**这里的邻居数是下界**（锚点更少 ⇒ 补出来的更少）。所以本探针只会**低估** ⑧ 的收益。
反过来，它是**保守的**：若下界都显示有收益，真跑只会更好。

判据与生产同口径：
  · 句子过滤 `kind <> 'head'` 且**不含「目录」的长句**（`sent-no-head=true`）
  · 相似度 = `1 - (embedding <=> 问题向量)`（与 `SentenceMapper` 同一条算式）
  · 材料地板 = `KB_MAT_FLOOR` 那一档（默认报 0.60/0.65/0.70 三档）

用法：python tools/neighbor-probe.py [run.json…] [--floor 0.60,0.65,0.70] [--span 1]
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from ruler import corpus                                        # noqa: E402

RUNS = os.path.join(HERE, "eval", "_runs")


def arg(name, dflt=None):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return dflt


def vec_literal(v):
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def main():
    paths = [a for a in sys.argv[1:] if a.endswith(".json")]
    if not paths:
        paths = [os.path.join(RUNS, "answer-quality__qwen3-4b__r1__acc.json")]
    floors = [float(x) for x in arg("--floor", "0.60,0.65,0.70").split(",")]
    span = int(arg("--span", "1"))
    model = arg("--model", "local:bge-m3")

    cases = {}
    rows = []
    for p in paths:
        d = json.load(io.open(p, encoding="utf-8"))
        for x in d.get("results", []):
            if not (x.get("sources") or []):
                continue
            key = (os.path.basename(p), x["id"])
            cases[x["id"]] = x
            rows.append(x)

    # (doc_id, seq) → chunk id：块表上 (doc_id, seq) 是唯一约束，一次查全。
    # ⚠️ 键必须是 **`doc_id`**（`sources` 里的 `docId`），不是 `docName` ——
    # 第一版按名字键，于是**一条都对不上、所有计数为 0**，而表还老老实实打出了全 0
    # （正是本项目反复吃亏的那类："没采到"长得像"没有"）。所以下面加了覆盖率断言。
    # ⚠️ `psql -t` 出来**全是字符串**（`'36'` 而不是 `36`），而 JSON 里 `seq` 是整数 ——
    # 不归一化就一条都对不上。第一版就是这么错的（键名错 → 改成 docId 后仍然 0/46，
    # 是覆盖率断言第二次把它抓出来的：**先证"能对上"，再谈数字**）。
    docseq2id = {}
    for cid, doc, seq in corpus.psql_rows(
            "SELECT c.id, c.doc_id, c.seq FROM chunks c", tag="nbp_chunks"):
        docseq2id.setdefault((doc, str(seq)), int(cid))

    print(f"题数 {len(rows)}（有材料的）· span ±{span} · 地板 {floors}")

    # **覆盖率断言**：`sources` 里的 (docId, seq) 有多少能在块表里对上。
    # 没有它，键写错时的症状是**整张表全 0** —— 而"全 0"与"确实没有"长得一模一样
    # （第一版按 docName 键就是这么错的）。宁可在这里炸，也不要让全 0 混进结论。
    tot_src = sum(len(x["sources"]) for x in rows)
    hit_src = sum(1 for x in rows for s in x["sources"]
                  if (s.get("docId"), str(s.get("seq"))) in docseq2id)
    if hit_src < tot_src:
        raise SystemExit(f"！sources 对不上块表：{hit_src}/{tot_src} —— "
                         f"键错了（docId 不是 docName），别拿这张表下结论")
    print(f"（对表核对：{hit_src}/{tot_src} 条 sources 都在块表里 ✓）")
    qtexts = [x["q"] for x in rows]
    qv = corpus.embed(qtexts)
    print(f"问题向量：{len(qv)} 条（{model}）", flush=True)

    per = []
    for x, v in zip(rows, qv):
        hits = []
        for s in x["sources"]:
            cid = docseq2id.get((s.get("docId"), str(s.get("seq"))))
            if cid is None:
                continue
            hits.append((s["docId"], s["seq"], cid))
        # 邻居 = 命中块的 seq±span（同文档），减去本来就是命中的
        have = {(d_, s_) for d_, s_, _ in hits}
        nbs = []
        seen = set()
        for doc, seq, _ in hits:
            for k in range(-span, span + 1):
                if k == 0:
                    continue
                key = (doc, seq + k)
                if key in have or key in seen:
                    continue
                seen.add(key)
                cid = docseq2id.get((key[0], str(key[1])))
                if cid is not None:
                    nbs.append((key[0], key[1], cid))
        all_ids = [c for _, _, c in hits] + [c for _, _, c in nbs]
        if not all_ids:
            continue
        sql = (
            "SELECT chunk_id, id, "
            "1 - (embedding <=> '%s'::vector) AS sim "
            "FROM sentences "
            "WHERE chunk_id IN (%s) AND embed_model = '%s' "
            "  AND embedding IS NOT NULL "
            "  AND kind <> 'head' "
            "  AND NOT (text LIKE '%%目录%%' AND length(text) > 80)"
            % (vec_literal(v), ",".join(str(i) for i in all_ids), model))
        srows = corpus.psql_rows(sql, tag="nbp")
        # ⚠️ **第三处同一个坑**：`psql -t` 出来的 `chunk_id` 也是字符串，
        # 而下面拿**整数**去查 ⇒ 每块都查到空列表 ⇒ **整张表又是全 0**
        # （而生产日志明明白白写着"过滤后剩 9 句 / 5 块"）。
        # ⇒ 教训升级：`psql_rows` 的返回**一切皆字符串**，凡是要与别处对键的字段，
        #    **在边界处一次性转干净**，不要指望"我记得转"。
        sims = {}
        for cid, sid, sim in srows:
            sims.setdefault(int(cid), []).append(float(sim))
        hit_ids = {c for _, _, c in hits}
        per.append({
            "id": x["id"], "kind": x.get("kind"), "q": x["q"],
            "n_hit_blk": len(hits), "n_nb_blk": len(nbs),
            "hit": {c: sims.get(c, []) for c in hit_ids},
            "nb": {c: sims.get(c, []) for c in (n for _, _, n in nbs) if c not in hit_ids},
        })

    print("\n===== 材料地板之下：命中块 vs 命中块+邻居块 =====")
    hdr = (f"{'地板':<6}{'命中存活句':>11}{'+邻居存活句':>13}{'新增':>7}"
           f"{'命中存活块':>11}{'+邻居存活块':>13}{'新增块':>8}{'变多的题':>9}")
    print(hdr)
    for fl in floors:
        a = b = ab = bb = 0
        gain_q = 0
        for r in per:
            ha = sum(1 for v_ in r["hit"].values() for s in v_ if s >= fl)
            hb = sum(1 for v_ in r["hit"].values() for s in v_ if s >= fl)
            nbb = sum(1 for v_ in r["nb"].values() for s in v_ if s >= fl)
            kba = sum(1 for v_ in r["hit"].values() if any(s >= fl for s in v_))
            kbb = kba + sum(1 for v_ in r["nb"].values() if any(s >= fl for s in v_))
            a += ha
            b += hb + nbb
            ab += kba
            bb += kbb
            if hb + nbb > ha:
                gain_q += 1
        print(f"{fl:<6}{a:>11}{b:>13}{b - a:>+7}{ab:>11}{bb:>13}{bb - ab:>+8}{gain_q:>7}/{len(per)}")

    print("\n逐题（地板 0.65）：")
    for r in per:
        fl = 0.65
        ha = sum(1 for v_ in r["hit"].values() for s in v_ if s >= fl)
        kba = sum(1 for v_ in r["hit"].values() if any(s >= fl for s in v_))
        nbn = sum(1 for v_ in r["nb"].values() for s in v_ if s >= fl)
        nbb = sum(1 for v_ in r["nb"].values() if any(s >= fl for s in v_))
        mark = "  ←" if nbn else ""
        print(f"  {r['id']:<8}{r['kind']:<18}命中 {r['n_hit_blk']:>2} 块 → 存活 {kba} 块/{ha} 句"
              f"　邻居 {r['n_nb_blk']:>2} 块 → 再存活 {nbb} 块/{nbn} 句{mark}")
    print("\n⚠️ 锚点是**过滤后**的 sources ⇒ 邻居数/收益都是**下界**（生产在过滤前补，锚点更多）。")
    print("   所以这张表的用法是**证伪**：若连下界都几乎没有新增，⑧ 就不该真跑。")


if __name__ == "__main__":
    main()
