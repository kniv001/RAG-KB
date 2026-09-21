# -*- coding: utf-8 -*-
"""
**代码替他自查**：no-think 生成 → 机械判据 → 不过就带着反馈重试一次。

由来：`think:false` + `format` + `minLength` 把 decode 从 2358 token 压到 **317 token**，
但答案里编了个资料里没有的「1GB」、还把 fork 机制读成了触发条件。
**thinking 丢掉的那部分能力里，「自查」是唯一可以机械化补回来的** ——
而判据今天已经有两台现成的（数字是否在资料里找得到、引用编号是否有效）。

所以设计是：生成 → **代码当守门人** → 不合格就把「哪几个数字没落地」塞回去重试。

本探针验三件事：
  ① 机械自检能不能抓到那次幻觉（对照 578 字答案里的「1GB」）
  ② 带反馈重试能不能修掉它（而不是换个幻觉）
  ③ 修不好的话，**降级路径**是否可用（退回 think:true 的旧路径）

用法：python tools/selfcheck-retry-probe.py [轮数，默认 2]
"""
import io
import json
import re
import sys
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:4b"
SYS = "你是个人知识库助手。严格依据【参考资料】回答问题。引用处用 [编号] 标注来源。"

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
          "properties": {"answer": {"type": "string", "minLength": 120}},
          "required": ["answer"]}

_NUM = re.compile(r"\d+(?:\.\d+)?")
_CITE = re.compile(r"\[(\d{1,2})\]")


def gen(user, timeout=300):
    body = {"model": MODEL, "stream": False, "think": False,
            "messages": [{"role": "system", "content": SYS},
                         {"role": "user", "content": user}],
            "format": SCHEMA, "options": {"temperature": 0.2, "num_ctx": 8192}}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    try:
        ans = json.loads(d["message"]["content"])["answer"]
    except Exception:
        ans = d["message"]["content"]
    return ans, d.get("eval_count", 0)


def check(ans, doc):
    """机械自检：返回 (没落地的数字, 无效引用编号)。**与 eval/judges 同一套口径。**"""
    body = _CITE.sub(" ", ans)                       # 先剥引用编号，别把它当数字
    ctx = re.sub(r"\s+", "", doc)
    bad_num = [n for n in _NUM.findall(re.sub(r"\s+", "", body)) if len(n) >= 2 and n not in ctx]
    bad_cite = [n for n in _CITE.findall(ans) if not (1 <= int(n) <= 3)]
    return sorted(set(bad_num)), sorted(set(bad_cite))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    user = DOC + f"\n【问题】\n{Q}"
    tot = 0
    for i in range(1, rounds + 1):
        ans, n = gen(user)
        tot += n
        bad_n, bad_c = check(ans, DOC)
        print(f"—— 第 {i} 次（{n} token）")
        print(f"   答案 {len(ans)} 字\n   {ans[:150].replace(chr(10),' ')}")
        print(f"   **自检：未落地数字 {bad_n or '无'}　无效引用 {bad_c or '无'}**")
        if not bad_n and not bad_c:
            print(f"\n⇒ 通过（累计 {tot} token）")
            return
        # **带反馈重试**：把具体的毛病指出来，而不是笼统说"别编造"
        user = (DOC + f"\n【问题】\n{Q}\n\n"
                + f"【上一次的回答有以下问题，请修正后重写】\n"
                + (f"- 这些数字在【参考资料】里根本不存在：{bad_n}。"
                   f"资料里没有的数字一律不要写，改用资料里的说法，或说明资料未给出具体值。\n" if bad_n else "")
                + (f"- 这些引用编号不存在：{bad_c}。只能用资料里出现过的编号。\n" if bad_c else "")
                + "只输出修正后的 JSON。")
    print(f"\n⇒ {rounds} 次都没通过（累计 {tot} token）—— 该走**降级路径**：退回 think:true 的旧路径")


if __name__ == "__main__":
    main()
