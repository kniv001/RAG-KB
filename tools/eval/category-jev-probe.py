# -*- coding: utf-8 -*-
"""
**判定改用 Jev 式（读概率），判定之后交给代码** —— 用户 2026-09-21 提的。

推理链（踩在项目自己的证据上）：
  · 上一轮量到的失败是 **76%，且错误方向完全一致**（把答不了的问题一律判【乙】）——
    那就是台账里点名的退化解「**恒选一侧**」。
  · 而「读概率」这条路的原始动机正是：那些退化解
    （抄示例值 / 空数组 / **恒选一侧** / 拒答填 0）**全长在"生成"这条路上**；
    `raw:true` + 同形状 few-shot ⇒ 首 token 就是答案。
  · 它的成败分界是「**离散分类行、连续程度不行**」（主题判断 8/8；
    精排 / 分段 / 第二标注者全败）—— 而 甲/乙/丙 **正是离散分类**。

所以把判定拆成**两次二值判断**，各自都是 Jev 证明过的那种形状：

  ① 甲？   —— 「这是闲聊/问身份/问本次对话之前说过什么的问题吗」
  ② 乙/丙？ —— 对**每一块**资料问「这段能直接回答上面的问题吗」，**代码取或**

**"判定之后"全部由代码做**：`OR` 聚合、以及最终该类对应的处置（那 20% 的契约推理里，
规则执行那半本来就该是代码的事）。

判据：拿基准的 kind 当参照。grounded-partial **两种出口都算对**。

用法：python tools/eval/category-jev-probe.py
"""
import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

# `logprob-judge.py` 名字带连字符，导不进来 —— 按路径加载，
# **不复制它的实现**（它里面有踩过坑的细节：'是' 与 ' 是' 撞键那条）。
_spec = importlib.util.spec_from_file_location("logprob_judge",
                                               os.path.join(TOOLS, "logprob-judge.py"))
lpj = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lpj)

BASE_RUN = os.path.join(HERE, "_runs", "answer-quality__qwen3-4b.json")
# 逐块判定是大头，缓存住 —— 改 ① 时不必重烧 ②
CACHE = os.path.join(HERE, "_cache-chunkp.json")

# ① 甲判定。
#
# **第一版写坏了**：`lpj.judge()` 自己会拼 `问：{question}\n答：`，而我把整句
# 「这是闲聊…吗？」当 question 传了进去 ⇒ 形状变成
# `问：这是闲聊…吗？\n问法：<真问题>\n答：` —— 错位，于是**一律判甲**（P(甲)=0.75~0.99）。
# 那是**又一个「恒选一侧」退化解**，而且长在"我拼错了 shape"上，不是机制的问题。
#
# 正确形状：把「这是闲聊…吗？」当成**问题那行的一部分**，few-shot 用同一形状。
IS_CHITCHAT_FEWSHOT = (
    "问：你是谁？\n这是闲聊、问身份、或问本次对话之前说过什么的问题吗？\n答：是\n"
    "问：Redis 挂了重启后数据还在吗？\n这是闲聊、问身份、或问本次对话之前说过什么的问题吗？\n答：否\n"
)


def is_chitchat(question):
    # 拼成 `问：<真问题>\n这是闲聊…吗？\n答：` —— 与 few-shot 同形状
    return lpj.judge(f"{question}\n这是闲聊、问身份、或问本次对话之前说过什么的问题吗？",
                     fewshot=IS_CHITCHAT_FEWSHOT)


def chunk_text(C, s):
    i = next((k for k in range(C.n)
              if C.doc[k] == s.get("docName") and C.seq[k] == str(s.get("seq"))), None)
    return C.body[i] if i is not None else (s.get("preview") or "")


EXPECT = {"grounded": {"乙"}, "ungrounded": {"丙"}, "chitchat": {"甲"},
          "grounded-partial": {"乙", "丙"}}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from ruler import corpus
    C = corpus.load()
    rows = json.load(io.open(BASE_RUN, encoding="utf-8"))["results"]
    cache = json.load(io.open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}

    ok = 0
    conf = {}
    for x in rows:
        srcs = x.get("sources") or []

        # ① 甲？
        p_chat, _, top5 = is_chitchat(x["q"])
        chat = (p_chat is not None and p_chat > 0.5)

        # ② 乙/丙：对每块问"这段能直接回答上面的问题吗"，**代码取或**
        ps = []
        for s in srcs:
            # **逐块调**：块多时这一步是大头，所以每块只问一次、只读一个 token。
            # 带缓存 —— 改 ① 的时候不必把 ② 重烧一遍（21 题 × ~10 块 ≈ 200 次调用）。
            key = f"{x['id']}|{s.get('docName')}#{s.get('seq')}"
            if key in cache:
                p = cache[key]
            else:
                p = lpj.judge_relevance(x["q"], chunk_text(C, s))
                cache[key] = p
                json.dump(cache, io.open(CACHE, "w", encoding="utf-8"))
            if p is not None:
                ps.append(p)
        best = max(ps) if ps else 0.0
        n_yes = sum(1 for p in ps if p > 0.5)

        got = "甲" if chat else ("乙" if best > 0.5 else "丙")
        want = EXPECT[x["kind"]]
        good = got in want
        ok += good
        conf[(x["kind"], got)] = conf.get((x["kind"], got), 0) + 1
        print(f"  {'✅' if good else '❌'} 判{got}　应为{'/'.join(sorted(want))}"
              f"　[{x['kind']:<16}] P(甲)={p_chat if p_chat is None else round(p_chat,3)}"
              f"　最高块P(能答)={round(best,3)}　是{ n_yes }/{len(ps)}块　{x['q'][:20]}",
              flush=True)

    n = len(rows)
    print(f"\n**判定准确率 {ok}/{n} = {100*ok/n:.0f}%**")
    print("\n混淆（题型 → 判定）：")
    for k in ("grounded", "grounded-partial", "ungrounded", "chitchat"):
        d = {g: c for (kk, g), c in conf.items() if kk == k}
        print(f"  {k:<17} {d}")


if __name__ == "__main__":
    main()
