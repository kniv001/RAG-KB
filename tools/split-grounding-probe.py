# -*- coding: utf-8 -*-
"""
**给通用知识一个合法的出口**：把"有据"和"通用知识"分进两个字段。

由来：no-think 把 decode 从 2358 token 压到 ~320，但实测**8/8 都掺了资料里没有的东西**
（`auto-aof-rewrite-min-size`、`bgrewriteaof`、`1GB`…）—— 而机械判据**分不出**
"正确的通用知识"与"编造"，因为两者都只是"不在资料里"。

**所以给它一个字段**：`answer` 只允许写资料里有的，资料没有的补充一律放 `general`。
这样：
  · 守门人**只盯 `answer` 字段**（口径明确，"不在资料里"就是违规，不用再猜对错）
  · `general` 由**代码渲染**成「以下为通用知识」，把信任边界显式化 ——
    而三段式契约本来就要求这么标（【丙】那条），只是【乙】里没有安放它的地方

判据（与 eval/judges 同一套口径）：
  · answer 字段里未落地的数字 / 术语
  · general 是否被真的用起来（用不起来说明模型没接受这个分工）

用法：python tools/split-grounding-probe.py [次数，默认 6]
"""
import io
import json
import re
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:4b"

SYS = """你是个人知识库助手。严格依据【参考资料】回答问题。

**回答分成两部分，各放各的**：
- `answer`：**只能写【参考资料】里有的内容**。引用处用 [编号] 标注来源。
  资料里没有的数字、配置项名、命令一律**不要**写进 answer。
- `general`：资料里没有、但你认为对用户有用的补充（你的通用知识），
  **一条一句**放这里。没有就留空数组。**不要**把资料里的内容搬到这里。
只输出 JSON。"""

DOC = """【参考资料】
[1] 来源：Redis持久化原理 — RDB与AOF详细解释（第 13 块）
而一旦新 AOF 文件创建完毕，Redis 就会从旧 AOF 文件切换到新 AOF 文件，并开始对新 AOF 文件进行追加操作。
[2] 来源：Redis持久化之RDB&AOF（第 14 块）
是的，的确会变得庞大，但 redis 会有优化的策略，比如你对一个 key1 键的操作，set key1 001, set key1 002，
那优化的结果就是将前两条去掉。
[3] 来源：Redis持久化之RDB&AOF（第 2 块）
Fork 发生时，父子进程内存共享，所以为了不影响子进程做数据快照，在这期间修改的数据将会被复制一份。
"""
Q = "AOF 文件越来越大怎么办？什么条件会自动触发重写？"

SCHEMA = {"type": "object",
          "properties": {"answer": {"type": "string", "minLength": 100},
                         "general": {"type": "array", "items": {"type": "string"}}},
          "required": ["answer", "general"]}

_CITE = re.compile(r"\[(\d{1,2})\]")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}")


def gen(user):
    body = {"model": MODEL, "stream": False, "think": False,
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": user}],
            "format": SCHEMA, "options": {"temperature": 0.2, "num_ctx": 8192}}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.load(r)
    try:
        j = json.loads(d["message"]["content"])
    except Exception:
        j = {"answer": d["message"]["content"], "general": []}
    return j, d.get("eval_count", 0)


def check(text, doc):
    plain = _CITE.sub(" ", text)
    flat = re.sub(r"\s+", "", plain)
    ctx = re.sub(r"\s+", "", doc)
    bad_n = sorted({n for n in _NUM.findall(flat) if len(n) >= 2 and n not in ctx})
    bad_t = sorted({t for t in _TERM.findall(plain)
                    if t.lower() not in ctx.lower() and len(t) >= 3})
    return bad_n, bad_t


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    user = DOC + f"\n【问题】\n{Q}"
    ok = used = 0
    toks = []
    for i in range(1, n + 1):
        j, tk = gen(user)
        toks.append(tk)
        bn, bt = check(j.get("answer", ""), DOC)
        g = j.get("general") or []
        passed = not bn and not bt
        ok += passed
        used += bool(g)
        print(f"{i}. {tk:>4} token　answer {len(j.get('answer',''))} 字"
              f"　general {len(g)} 条　⇒ {'✅ 通过' if passed else f'❌ 未落地 数字{bn} 术语{bt}'}")
        if i == 1:
            print(f"   answer：{j.get('answer','')[:110].replace(chr(10),' ')}")
            for x in g[:3]:
                print(f"   general：{x[:90]}")
    print(f"\nanswer 字段通过 **{ok}/{n}**（对照：不分工时 术语未落地 8/8、数字 1/8）")
    print(f"general 被用起来 **{used}/{n}**　token 中位 {sorted(toks)[len(toks)//2]}")


if __name__ == "__main__":
    main()
