# -*- coding: utf-8 -*-
"""
**建句子级索引**：把每块切成带字符区间的句子，写进 `sentences` 表。

## 为什么要有这张表（回到"token 花在哪"）

2026-09-23 实测的账：**decode 占一次问答的 86%，其中 81% 的 token 是思考，
而思考里中位 38% 的字是在复述已经在提示词里的资料**。
"禁止复述"那条提示词**试过、不通**（思考反而变长，符号检验 p=0.007）——
因为**光禁止，模型会换个方式做同一件事**。

剩下一条路是**换任务**：让模型输出**地址**（用哪几句），正文由**代码按地址原样拼**。
地址要成立，句子必须先可寻址 —— 这张表就是那个前提。

## 四条纪律（本项目吃过亏的地方）

1. **区间必须能逐字还原** —— 入库前逐句核 `content[char_start:char_end] == text`，
   一条不符就**整批拒绝**（不写"差不多对"的索引：地址偏一格，展开出来的正文就是错的）
2. **锚内容，不锚位置**（2026-09-25 改）—— 每行存 `chunk_hash = md5(这一块的正文)[:12]`，
   "这一行还有效吗"于是是**一句 SQL 能回答的事实**（`sent_index.freshness`）。
   ⚠️ 原先存的是**全库语料戳**，那个戳 **加一篇或删一篇文档就变** ——
   晋升 40 篇新闻之后，904 块里只有 243 块是新的，整个索引却显示"陈旧"，
   一重建就是 10052 句向量全算。这是这次返工的全部理由
3. **重建是增量的** —— 只重切"新来的 / 内容变了 / 切法变了"的块，其余**原样不动**
   （连 `built_at` 都不动）。见下：为什么"每次都全切"反而让这条成立
4. **原子** —— `BEGIN` 里先删后插，失败即回滚；不留"删了一半"的中间态

## 为什么每次都全切一遍（而不是只切新块）

切法是会变的（`sentence_split` 的 MIN_LEN / 表格处理），而**切法没有版本号** ——
"换了切法记得重建"这类约定，正是本项目反复失效的东西（没人消费的字段等于不存在）。
所以改成：**每次把全部块都重切一遍**（纯 CPU、秒级；真正的开销在向量），
切出来的结果与库里已有的**逐行比对**，一模一样就不动库。
于是"换了切法忘了重建"这个失效模式**根本不存在** —— 不用记得给切法加版本号。

用法：
    python tools/build-sentences.py            # 增量重建 + 只算缺的向量 + 灌库（一条命令到底）
    python tools/build-sentences.py --dry      # 只看统计，不写库
    python tools/build-sentences.py --no-vec   # 只建表，向量以后再说
    python tools/build-sentences.py --force-vec  # 连向量也全部重算（换模型时用）

**为什么默认连向量一起做**：这三步原先是三个脚本串着跑，于是"用什么模型标识"
散在三处 ⇒ 灌库写了 `bge-m3` 而 Java 查 `local:bge-m3`，**一条都命中不了**，
症状是"分层注入 0 句、悄悄退回整块"。合成一条命令，那份约定就只剩一处（见 sent_index）。
"""
import hashlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sentence_split import split                      # noqa: E402
from ruler import corpus as C                         # noqa: E402
import sent_index                                     # noqa: E402

COLS = ["chunk_id", "doc_id", "seq", "char_start", "char_end",
        "kind", "text", "sent_hash", "chunk_hash"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    dry = "--dry" in sys.argv
    no_vec = "--no-vec" in sys.argv
    force_vec = "--force-vec" in sys.argv
    c = C.load()
    rows = C.psql_rows("SELECT c.id, c.doc_id, c.content FROM chunks c ORDER BY c.id")
    print(f"语料 {c.n} 块 · 戳 {c.stamp}　库里 {len(rows)} 块")

    # 库里已有什么：按块分组。**不读 text 也能比**（比的是 seq/区间/类型/句子哈希），
    # 但读出来一起比更严 —— psql_rows 走的是 COPY，正文里的换行与制表符都是转义过的，
    # 能逐字还原（多行代码块不会把一行拆成两行）。
    old = {}
    for r in C.psql_rows("SELECT chunk_id, chunk_hash, seq, char_start, char_end, kind, text, sent_hash "
                         "FROM sentences ORDER BY chunk_id, seq"):
        e = old.setdefault(r[0], {"anchors": set(), "rows": []})
        e["anchors"].add(r[1])
        e["rows"].append(tuple(r[2:8]))          # (seq, 起, 止, 类型, 正文, 句子哈希)

    todo = []                                     # 要写的块：[(chunk_id, doc, anchor, [新行...])]
    kinds, nsent, bad = {}, 0, 0
    per = []
    why = {"新来的": 0, "内容变了": 0, "切法变了": 0}
    for cid, doc, content in rows:
        anchor = sent_index.chunk_anchor(content)
        new = []
        for k, (a, b, kind, txt) in enumerate(split(content), start=1):
            # **逐字还原** —— 这是整张表存在的前提，不满足就整批拒收
            if content[a:b] != txt:
                bad += 1
                if bad <= 2:
                    print(f"  ✗ 区间不符：chunk {cid} #{k}　{a}:{b} 原文={content[a:b][:40]!r}")
                continue
            new.append((str(k), str(a), str(b), kind, txt,
                        hashlib.sha1(txt.strip().encode("utf-8")).hexdigest()[:12]))
            kinds[kind] = kinds.get(kind, 0) + 1
            nsent += len(txt)
        per.append(len(new))
        have = old.get(cid)
        if have and anchor in have["anchors"] and have["rows"] == new:
            continue                              # **一模一样 —— 一个字都不动**
        why["新来的" if not have else
            ("内容变了" if anchor not in have["anchors"] else "切法变了")] += 1
        todo.append((cid, doc, anchor, new))
    if bad:
        raise SystemExit(f"✗ {bad} 条区间无法还原原文 —— **整批不写**（宁可没有索引，不要错位的索引）")

    v = sorted(per)
    print(f"\n全库切出 {sum(per)} 句（总 {nsent} 字，均 {nsent/max(1,sum(per)):.0f} 字/句）")
    print("  按类型：" + "　".join(f"{k} {v2}" for k, v2 in sorted(kinds.items(), key=lambda x: -x[1])))
    print(f"  每块句数 中位 {v[len(v)//2]}　范围 {v[0]}~{v[-1]}　"
          f"（{sum(1 for x in per if x)} 块有句子，{sum(1 for x in per if not x)} 块切出 0 句）")
    print(f"  **复用 {len(rows) - len(todo)} 块**　重切 {len(todo)} 块"
          + ("（" + "　".join(f"{k} {n}" for k, n in why.items() if n) + "）" if todo else ""))
    if dry:
        print("\n--dry：不写库")
        print("  " + sent_index.freshness_line(c))
        return

    buf = io.StringIO()
    buf.write("BEGIN;\n")
    if todo:
        ids = ",".join(t[0] for t in todo)
        buf.write(f"DELETE FROM sentences WHERE chunk_id IN ({ids});\n")
        buf.write(f"COPY sentences ({','.join(COLS)}) FROM STDIN;\n")
        for cid, doc, anchor, new in todo:
            for r in new:
                buf.write("\t".join(C.copy_escape(str(x))
                                    for x in (cid, doc) + r + (anchor,)) + "\n")
        buf.write("\\.\n")
    buf.write("COMMIT;\n")
    C.psql_script(buf.getvalue(), tag="sentences")
    print("\n  " + sent_index.freshness_line(c))

    if no_vec:
        print("\n--no-vec：跳过向量。**注意**：跳过之后分层注入会因为查不到向量而退回整块。")
        return
    # **建完顺手算向量并灌库**（一条命令到底）——见模块开头的说明：
    # 这三步原先分散在三个脚本里，"用什么模型标识"就有三份说法，
    # 灌库那份写错时**不报错**，只让分层注入静默退回整块。
    rows_s = sent_index.load_rows()
    if not rows_s:
        raise SystemExit("✗ sentences 表是空的 —— 上一步没写进去？")
    V = sent_index.embed_and_cache([r[3] for r in rows_s], force=force_vec)
    # ⚠️ 传的是**全量行**：只按内容锚复用，所以**灌的向量与该行正文必然对得上**
    #（老格式缓存那条路也按 hash 查，不按位置搬）。
    sent_index.fill_db(rows_s, V)


if __name__ == "__main__":
    main()
