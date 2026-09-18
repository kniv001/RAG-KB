# -*- coding: utf-8 -*-
"""
摘要改形状（段落 → 一行一条）到底有没有副作用：新旧两版提示词，同一批输入对打。

**只改形状**是刻意的 —— 合并、增量、异步、上限都没动，出问题时好归因。
所以这个探针也只问形状与语义有没有被改坏：

  用例A 含推翻 + 用户偏好：旧的「块大小 600」被新的「改成 450」推翻，
        外加一句「以后不要用首先其次」。看：旧条目丢没丢、偏好留没留、会不会两条并存
  用例B 首次（无已有条目）：格式对不对
  用例C 杂而长：会不会守「≤12 条 / 每条 ≤40 字」

判据：条数、每条字数、有没有出现空数组（语法约束下 {"items":[]} 是最省的合法输出，
等于把记忆清空 —— 代码里已挡住，这里看它出现的频率）。**打印全文，判定要读。**

用法：python tools/summary-shape-probe.py
"""
import io
import json
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"

OLD_PROMPT = """你是对话摘要器。把「新增对话」并入「已有摘要」，输出一段连贯的中文摘要。

要求：
1. 保留具体信息：讨论的主题、得出的结论、用户明确表达过的偏好或约束、尚未解决的问题。
2. 用户明确要求记住的任何内容必须原样保留 —— 名字、代号、数字、约定、日期。
   这类信息一旦丢掉就再也找不回来，比主题概括重要得多。
3. 不要写成「用户问了…助手回答了…」的流水账，直接写内容本身。
4. 已有摘要里仍然重要的信息要保留，只有被推翻或过时的才丢掉。
5. 全文不超过 300 字。

只输出 JSON：{"summary":"摘要正文"}"""
OLD_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}},
              "required": ["summary"]}

NEW_PROMPT = """你是对话记忆的整理器。把「新增对话」并入「已有记忆」，输出**一行一条**的条目。

要求：
1. **新增对话是新信息的唯一来源，每条结论都必须落到条目上** —— 已有条目为空时同样如此。
   任何情况下都不许返回空数组。
2. 一条一个事实或结论，不要写成段落，不要写「用户问了…助手回答了…」的流水账。
3. 保留具体信息：讨论的主题、得出的结论、用户明确表达过的偏好或约束、尚未解决的问题。
   用户明确要求记住的任何内容必须原样保留 —— 名字、代号、数字、约定、日期。
   这类信息一旦丢掉就再也找不回来，比主题概括重要得多。
4. 已有条目里仍然成立的**原样保留**，不要改写、不要合并同义条目。
5. 只有被新增对话推翻或过时的条目才丢掉。
6. 最多 12 条，每条不超过 40 字。

只输出 JSON：{"items":["一条结论","另一条结论"]}"""
NEW_SCHEMA = {"type": "object",
              "properties": {"items": {"type": "array", "items": {"type": "string"}}},
              "required": ["items"]}

CASES = [
    ("A 含推翻+偏好",
     "用户在做个人 RAG 知识库；块大小定为 600 字；用户偏好表格而非选项式提问",
     "用户：我想把块大小从 600 改成 450，因为有些块抽不出事实。\n"
     "助手：450 字边界会更碎，召回条数会变多。\n"
     "用户：另外记住，以后回答不要用「首先其次」这种套话。\n"
     "助手：明白，直接给结论。"),
    ("B 首次（无已有）",
     "",
     "用户：KV 缓存为什么在长上下文下会掉速？\n"
     "助手：因为每生成一个 token 都要把整份 KV 读一遍，越界到系统内存就受 PCIe 带宽限制。\n"
     "用户：那结论是先别开 32K？\n"
     "助手：对，16K 够用，真要开先做 q4 量化。"),
    ("C 杂而长",
     "项目用 PostgreSQL + pgvector；主题树 11 个簇",
     "用户：今天把分块改成按句子边界了，半句开头的块从 75% 降到 13%。\n"
     "助手：召回没回归，已全量重建索引。\n"
     "用户：抓取那边也改成保留 Markdown 结构了。\n"
     "助手：只对新文档生效，旧的还是一行标题。\n"
     "用户：还有个坑，psql -A 会把多行内容冲散，得用 COPY。\n"
     "助手：已记下。\n"
     "用户：明天想试试把摘要改成一行一条，你觉得呢？\n"
     "助手：可以先小范围试，注意空数组会把记忆清空。"),
]


def post(model, system, user, schema, timeout=120, tries=3):
    body = {"model": model, "stream": False, "think": False, "format": schema,
            "options": {"temperature": 0.2, "num_ctx": 8192},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    for _ in range(tries):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r).get("message", {}).get("content", "")
        except Exception:
            time.sleep(2)
    return ""


def build_user(old, fresh):
    return (f"已有摘要：\n{old or '（无，这是第一次）'}\n\n新增对话：\n{fresh}"
            if True else "")


def run(tag, old, fresh, prompt, schema, kind):
    t0 = time.time()
    txt = post(CHAT, prompt, build_user(old, fresh), schema)
    ms = int((time.time() - t0) * 1000)
    try:
        o = json.loads(txt)
    except Exception:
        print(f"    [{kind}] 解析失败：{txt[:80]}")
        return None
    print(f"    [{kind}] {ms}ms")
    if "items" in o:
        items = [str(x) for x in (o.get("items") or [])]
        if not items:
            print("      ！空数组 —— 语义上等于清空记忆（代码里会跳过不覆盖）")
        over = [x for x in items if len(x) > 40]
        print(f"      条数 {len(items)}　超 40 字的 {len(over)} 条")
        for x in items:
            print(f"       · {x}")
        return items
    s = str(o.get("summary", ""))
    print(f"      字数 {len(s)}")
    print(f"       {s}")
    return s


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("同一批输入，新旧提示词各跑一遍\n")
    for tag, old, fresh in CASES:
        print(f"—— {tag} ——")
        print("  旧（段落）：")
        run(tag, old, fresh, OLD_PROMPT, OLD_SCHEMA, "旧")
        print("  新（一行一条）：")
        run(tag, old, fresh, NEW_PROMPT, NEW_SCHEMA, "新")
        print(flush=True)


if __name__ == "__main__":
    main()
