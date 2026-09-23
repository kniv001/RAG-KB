# -*- coding: utf-8 -*-
"""
把句子向量灌进 `sentences.embedding`（分层注入要用）。

## 为什么要落库（而不是让 Java 现算）

Java 侧要在**提示词组装时**按问题对"已召回块里的句子"排序 —— 那是一次 SQL 查询里
`ORDER BY embedding <=> :q LIMIT M` 的事，前提是向量在库里。
现算的话，等于把 6003 条向量搬进 JVM 内存再自己算余弦 —— 没道理。

## 数据从哪来

`tools/_sentence_vecs.json`（`sent-recall.py` 建的缓存，**按内容锚**：stamp + 逐条 hash）。
**写入前必须核对 hash 列表** —— 对不上说明表被重建过，那些向量对应的是**别的句子**，
而按位置灌进去**不会报错**，只会让分层注入挑错句（本项目在向量缓存上吃过一次：
661 条里 438 条按位置对齐错位、不报错、只让所有指标一起变低）。

用法：python tools/fill-sentence-vecs.py
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ruler import corpus                                  # noqa: E402

VECFILE = os.path.join(HERE, "_sentence_vecs.json")


def main():
    """**只重灌向量**（不改表）—— 建表那一步请用 build-sentences.py（它默认连向量一起做）。

    保留这个入口是因为有一种情形需要它：换了向量模型 / 重算了向量，
    但句子表本身没动（不想重切一遍）。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    import sent_index
    C = corpus.load()
    rows = sent_index.load_rows()
    if not rows:
        raise SystemExit("sentences 表是空的 —— 先跑 python tools/build-sentences.py")
    V = sent_index.embed_and_cache(C, rows)
    sent_index.fill_db(C, rows, V)


if __name__ == "__main__":
    main()
