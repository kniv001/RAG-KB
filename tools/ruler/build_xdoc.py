# -*- coding: utf-8 -*-
"""
造一个**跨文档多跳**题目集：答案要**综合同一主题的两篇文章**。

为什么值得单做一族（现有 25 题全在**一篇文档内部**）：个人知识库里同一个主题常有多篇，
真实提问往往要综合它们 —— 这是现有尺子完全没测过的检索形状。

**选题依据是量出来的**，不是感觉：同主题两篇的块级最大余弦（中位聚合）
对比全库任意两篇（中位 0.538）：

    模型量化 0.678 · K8s调度 0.743 · 提示词工程 0.772 · HTTP/2 0.773 · 向量数据库 0.788
    MySQL 0.799 · Redis 0.842 · DNS 0.841 · JVM 0.850 · Python GIL 0.870
    Docker 0.897 · 一致性哈希 0.996

**越低越互补**（讲的是不同的侧面），越高越是**同一套话讲第二遍** ——
后者上出跨文档题，前提（"两边都得看"）根本不成立。本集只取**低的那几簇**。

用法：python tools/ruler/build_xdoc.py [--write]
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ruler import cases, corpus    # noqa: E402

# (题目, [(文档名关键词, seq), …])  —— 靶子按内容存，seq 只是构造时的指路牌
SPEC = [
    # ── 模型量化（0.678，最互补：INT8 讲位宽体系 / QLoRA 讲 NF4 与双重量化 / LoRA 讲低秩分解）
    ("FP32、FP16、BF16 的指数位与小数位各是多少？NF4 是多少位，又靠什么进一步压缩存储开销？",
     [("部署系列——神经网络INT8量化", 3), ("深入浅出模型量化与QLoRA", 4)]),
    ("LoRA 训练时到底更新哪些参数？同一个基座换成 QLoRA 之后，基座权重和 adapter 分别是什么状态？",
     [("LoRA 微调原理", 3), ("深入浅出模型量化与QLoRA", 3)]),
    ("对称量化解决了什么问题？QLoRA 用的 NF4 大致相当于多少位的存储水平？",
     [("部署系列——神经网络INT8量化", 9), ("深入浅出模型量化与QLoRA", 4)]),

    # ── K8s 调度（0.743：第一篇讲亲和性的两个长字段名，第二篇补污点容忍）
    ("节点亲和性里的硬策略和软策略分别对应哪两个长字段名？污点与容忍是用来管哪个方向的调度？",
     [("Kubernetes Pod调度说明1-亲和性", 16), ("Kubernetes Pod调度策略：节点亲和性", 3)]),

    # ── JVM G1（0.850：分工不同 —— 一篇讲 Mixed GC 与 Region 分代，另一篇补 Remembered Set 与 Full GC）
    ("G1 的 Mixed GC 靠什么决定回收哪些老年代 Region？「可预测的停顿时间」具体指什么？",
     [("JVM垃圾回收器 ：G1 回收器", 4), ("JVM基础系列：G1垃圾回收器", 4)]),
    ("G1 回收新生代时怎么避免扫描整个老年代？为什么下一次年轻代 GC 必须等根区域扫描完成？",
     [("JVM基础系列：G1垃圾回收器", 8), ("JVM垃圾回收器 ：G1 回收器", 5)]),

    # ── 向量数据库（0.788：一篇讲结构与选型，一篇给基准数字与距离度量）
    ("HNSW 的索引结构该怎么描述？有没有实测的性能数字（什么精度、多少 QPS）？",
     [("不懂向量数据库", 3), ("深入解析：人工智能基础架构与算力之5", 6)]),
    ("向量检索为什么不做精确比对？对二值化或哈希过的向量，常用哪种距离？",
     [("不懂向量数据库", 3), ("深入解析：人工智能基础架构与算力之5", 12)]),
]


def main():
    C = corpus.load()
    print(C.line() + "\n")
    out, bad = [], []
    for n, (q, tg) in enumerate(SPEC, 1):
        ts = []
        for key, sq in tg:
            i = next((k for k in range(C.n) if key in C.doc[k] and C.seq[k] == str(sq)), None)
            if i is None:
                bad.append(f"x{n:02d} 找不到 {key} seq={sq}")
                continue
            ex = C.head(i)
            if not C.find(ex, doc=C.doc[i]):
                bad.append(f"x{n:02d} 摘录解析不到：{ex[:20]}")
            ts.append({"excerpt": ex, "doc": C.doc[i], "seq_hint": sq})
            print(f"  x{n:02d} ← {C.doc[i][:26]:<28} #{sq:<4} {ex[:34]}")
        out.append({"id": f"xd{n:02d}", "q": q, "targets": ts})
    data = {"schema": cases.SCHEMA, "name": "xdoc-8", "kind": "retrieval", "judge": "all-hit",
            "note": "**跨文档多跳**：每题的靶子落在**两篇不同文档**上，答案要综合两边。"
                    "选题按实测的同主题相似度取**最低**（最互补）的几簇 —— "
                    "相似度高的那些（Redis 0.84 / Docker 0.90 / 一致性哈希 1.00）是"
                    "同一套话讲第二遍，上去题不成立。",
            "cases": out}
    if bad:
        print(f"\n⚠ {len(bad)} 处问题：")
        for b in bad:
            print("   " + b)
    if "--write" in sys.argv:
        p = cases.save(data, "xdoc-8")
        print(f"\n已写 {p}（{len(out)} 题）")
    else:
        print(f"\n（预演，未写盘；加 --write 落盘）")
    print(f"跨文档题 {len(out)} 道，靶子 {sum(len(c['targets']) for c in out)} 个")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
