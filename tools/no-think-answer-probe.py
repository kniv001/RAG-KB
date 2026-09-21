# -*- coding: utf-8 -*-
"""
**不给它想的机会**：关掉思考，用 format 把采样器钉住。

想法（用户提的）：前面四次都在"让它自己想得更少"，全部失败（形状约束只压 14%、
两段式思考没变短、注入思考反而放大 5.7 倍）。**换个方向 —— 不给它想的地方**：
`think:false` 关掉思考通道，代码把"扫描"替它做完。

**已知障碍**（代码注释里记着实测）：只关思考不加 `format`，推理**不会消失**，
它会从 thinking 通道转进 content —— 「首先，用户的问题是……」直接混进答案。

**绕法**：把答案装进 JSON 字段。`format` 是硬约束，采样器**无法输出 `{` 之外的第一字符**，
于是开场白与推理**写不出来**；而信封还能承载**代码注入**的字段。

三组对照：
  甲 think:false，无 format           —— 复现"推理漏进正文"
  乙 think:false + {"answer":string}  —— 采样器被钉住
  丙 乙 + 代码生成的资料索引           —— 代码替他"扫描"

用法：python tools/no-think-answer-probe.py
"""
import io
import json
import re
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:4b"

SYS = "你是个人知识库助手。严格依据【参考资料】回答问题。引用处用 [编号] 标注来源。"

# 代码生成的"思考"：编号 / 来源 / 语境行（库里本来就有），模型不必自己扫
INDEX = """【资料索引（系统预先生成，直接用，不必自己核对）】
[1] Redis持久化原理 — RDB与AOF详细解释 #13 · 本段可回答：AOF 重写期间的新写命令会丢吗
[2] Redis持久化之RDB&AOF #14 · 本段可回答：AOF 的文件为什么会越来越大
[3] Redis持久化之RDB&AOF #2 · 本段可回答：fork 时父子进程的内存是怎么处理的
"""

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

CASES = [
    ("丁 answer 设 minLength:120", json.dumps(
        {"type": "object", "properties": {"answer": {"type": "string", "minLength": 120}},
         "required": ["answer"]})),
    ("戊 points≥3 且 answer 设下限", json.dumps(
        {"type": "object",
         "properties": {"points": {"type": "array", "minItems": 3, "items": {"type": "string"}},
                        "answer": {"type": "string", "minLength": 120}},
         "required": ["points", "answer"]})),
    # 甲放最后跑：它最慢（1778 token）

    ("乙 answer 无下限", json.dumps(
        {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]})),
    ("丙 answer 无下限 + 代码生成的资料索引", json.dumps(
        {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]})),
]

# 答案开头的"推理污染"特征
POLLUTE = re.compile(r"(首先|用户的问题是|让我|我需要|思考|分析一下|嗯|好的)")


def chat(user, schema):
    body = {"model": MODEL, "stream": False, "think": False,
            "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user}],
            "options": {"temperature": 0.2, "num_ctx": 8192}}
    if schema:
        body["format"] = json.loads(schema)
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for name, schema in CASES:
        user = (INDEX if "索引" in name else "") + DOC + f"\n【问题】\n{Q}"
        print(f"===== {name}")
        try:
            d = chat(user, schema)
        except Exception as e:
            print(f"  ✗ {e}\n"); continue
        raw = d.get("message", {}).get("content", "") or ""
        think = d.get("message", {}).get("thinking", "") or ""
        gen = d.get("eval_count", 0)
        # 乙丙要解析信封
        ans = raw
        if schema and name.startswith("戊"):
            try:
                j = json.loads(raw); ans = j.get("answer", raw)
            except Exception:
                ans = raw
        elif schema:
            try:
                ans = json.loads(raw).get("answer", raw)
            except Exception:
                ans = raw
        head = ans.strip()[:70].replace("\n", " ")
        print(f"  thinking 通道 {len(think)} 字　生成 {gen} token")
        print(f"  答案 {len(ans)} 字，开头：{head!r}")
        print(f"  ⇒ 开场白污染：{'有 ❌' if POLLUTE.match(ans.strip()) else '**无** ✅'}\n")


if __name__ == "__main__":
    main()
