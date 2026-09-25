# -*- coding: utf-8 -*-
"""
把句子向量灌进 `sentences.embedding`（分层注入要用）。

## 为什么要落库（而不是让 Java 现算）

Java 侧要在**提示词组装时**按问题对"已召回块里的句子"排序 —— 那是一次 SQL 查询里
`ORDER BY embedding <=> :q LIMIT M` 的事，前提是向量在库里。
现算的话，等于把 6003 条向量搬进 JVM 内存再自己算余弦 —— 没道理。

## 数据从哪来

`tools/_sentence_vecs.json` —— **按内容锚**（`sha1(嵌进去的那串字)[:16] → 向量`，
`sent_index.embed_and_cache` 是它唯一的读写处）。灌库时按 `(chunk_id, seq)` 对行，
而向量是按**正文**取的 ⇒ 两者对得上的前提是**行还是那些行**；
所以灌之前先核 `sent_index.freshness()`（块内容锚），对不上就该重建而不是硬灌。

用法：python tools/fill-sentence-vecs.py
"""
import sys

import sent_index


def main():
    """**只重灌向量**（不改表）—— 建表那一步请用 build-sentences.py（它默认连向量一起做）。

    保留这个入口是因为有一种情形需要它：换了向量模型 / 重算了向量，
    但句子表本身没动（不想重切一遍）。**换模型时这里要 `force=True`**
    （缓存里记着模型标识，模型没变而向量要重算，缓存是认不出来的）。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("  " + sent_index.freshness_line())
    rows = sent_index.load_rows()
    if not rows:
        raise SystemExit("sentences 表是空的 —— 先跑 python tools/build-sentences.py")
    V = sent_index.embed_and_cache([r[3] for r in rows], force=True)
    sent_index.fill_db(rows, V)


if __name__ == "__main__":
    main()
