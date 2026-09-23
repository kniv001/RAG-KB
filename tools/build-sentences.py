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

## 三条纪律（本项目吃过亏的地方）

1. **区间必须能逐字还原** —— 入库前逐句核 `content[char_start:char_end] == text`，
   一条不符就**整批拒绝**（不写"差不多对"的索引：地址偏一格，展开出来的正文就是错的）
2. **带上语料戳** —— 一切按位置对齐的缓存/索引都会随语料重建静默错位
   （向量缓存那次 661 条里 438 条错，且不报错）。戳不一致就是陈旧，要重建
3. **重建是原子的** —— 只在一张表上做，`BEGIN` 里先删后插，失败即回滚；
   不留"删了一半"的中间态

用法：
    python tools/build-sentences.py            # 重建**并算向量、灌库**（一条命令到底）
    python tools/build-sentences.py --dry      # 只看统计，不写库
    python tools/build-sentences.py --no-vec   # 只建表，向量以后再说

**为什么默认连向量一起做**：这三步原先是三个脚本串着跑，于是"用什么模型标识"
散在三处 ⇒ 灌库写了 `bge-m3` 而 Java 查 `local:bge-m3`，**一条都命中不了**，
症状是"分层注入 0 句、悄悄退回整块"。合成一条命令，那份约定就只剩一处（见 sent_index）。
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sentence_split import split                      # noqa: E402
from ruler import corpus as C                         # noqa: E402
import hashlib                                        # noqa: E402

COLS = ["chunk_id", "doc_id", "seq", "char_start", "char_end",
        "kind", "text", "sent_hash", "stamp"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    dry = "--dry" in sys.argv
    no_vec = "--no-vec" in sys.argv
    c = C.load()
    rows = C.psql_rows("SELECT c.id, c.doc_id, c.content FROM chunks c ORDER BY c.id")
    print(f"语料 {c.n} 块 · 戳 {c.stamp}　库里 {len(rows)} 块")

    out = []
    kinds = {}
    bad = 0
    nsent = 0
    for cid, doc, content in rows:
        for k, (a, b, kind, txt) in enumerate(split(content), start=1):
            # **逐字还原** —— 这是整张表存在的前提，不满足就整批拒收
            if content[a:b] != txt:
                bad += 1
                if bad <= 2:
                    print(f"  ✗ 区间不符：chunk {cid} #{k}　{a}:{b} 原文={content[a:b][:40]!r}")
                continue
            out.append((cid, doc, k, a, b, kind, txt,
                        hashlib.sha1(txt.strip().encode("utf-8")).hexdigest()[:12],
                        c.stamp))
            kinds[kind] = kinds.get(kind, 0) + 1
            nsent += len(txt)
    if bad:
        raise SystemExit(f"✗ {bad} 条区间无法还原原文 —— **整批不写**（宁可没有索引，不要错位的索引）")

    print(f"\n切出 {len(out)} 句（总 {nsent} 字，均 {nsent/max(1,len(out)):.0f} 字/句）")
    print("  按类型：" + "　".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda x: -x[1])))
    per = {}
    for r in out:
        per[r[0]] = per.get(r[0], 0) + 1
    v = sorted(per.values())
    print(f"  每块句数 中位 {v[len(v)//2]}　范围 {v[0]}~{v[-1]}　"
          f"（{len(v)} 块有句子，{len(rows)-len(v)} 块切出 0 句）")
    if dry:
        print("\n--dry：不写库")
        return

    buf = io.StringIO()
    buf.write("BEGIN;\n")
    buf.write("DELETE FROM sentences;\n")
    buf.write(f"COPY sentences ({','.join(COLS)}) FROM STDIN;\n")
    for r in out:
        buf.write("\t".join(C.copy_escape(str(x)) for x in r) + "\n")
    buf.write("\\.\n")
    buf.write("COMMIT;\n")
    C.psql_script(buf.getvalue(), tag="sentences")

    back = C.psql_rows("SELECT count(*), count(distinct chunk_id) FROM sentences")
    print(f"\n已写入　库内 {back[0][0]} 句 / {back[0][1]} 块（戳 {c.stamp}）")
    print("  ⚠ 换切法（MIN_LEN / 表格处理）会让句数变化 —— 届时本索引与历史读数不可直接比。")

    if no_vec:
        print("\n--no-vec：跳过向量。**注意**：跳过之后分层注入会因为查不到向量而退回整块。")
        return
    # **建完顺手算向量并灌库**（一条命令到底）——见模块开头的说明：
    # 这三步原先分散在三个脚本里，"用什么模型标识"就有三份说法，
    # 灌库那份写错时**不报错**，只让分层注入静默退回整块。
    import sent_index
    rows_s = sent_index.load_rows()
    V = sent_index.embed_and_cache(c, rows_s, force=True)
    sent_index.fill_db(c, rows_s, V)


if __name__ == "__main__":
    main()
