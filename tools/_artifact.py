# -*- coding: utf-8 -*-
"""
**给探针留完整产物的那一半** —— 与 `run.py` 配套。

日志里通常只打**节选**（`思考[:120]` 那种），因为终端看不了几千字。
而事后回溯时最缺的恰恰是**被截掉的那部分**：想知道那次的完整思考长什么样、
那题的完整答案是什么、中间那次 JSON 返回了什么 —— 光有前 120 字没用。

用法（探针里两行）：

    from _artifact import save
    save("thinking", x["thinking"])          # → <运行目录>/thinking.txt
    save("answers.json", {"q": q, "a": a})   # dict/list 自动 json 落盘

没有 `KB_RUN_DIR`（没走 `run.py` 直接跑）时**写到临时目录并把路径打出来** ——
不静默丢弃，也不报错（记录器不该成为新的失败点）。
"""
import io
import json
import os
import tempfile

_FALLBACK = None


def run_dir():
    d = os.environ.get("KB_RUN_DIR")
    if d:
        os.makedirs(d, exist_ok=True)
        return d
    global _FALLBACK
    if _FALLBACK is None:
        _FALLBACK = tempfile.mkdtemp(prefix="kb-artifact-")
        print(f"（没有 KB_RUN_DIR —— 产物写到 {_FALLBACK}；"
              f"想归档请用 `python tools/run.py <名字> -- <命令>`）")
    return _FALLBACK


def save(name, content):
    """把**完整**内容写进本次运行目录。返回落盘路径。"""
    p = os.path.join(run_dir(), name)
    if isinstance(content, (dict, list)):
        json.dump(content, io.open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    else:
        io.open(p, "w", encoding="utf-8", errors="replace").write(str(content))
    return p
