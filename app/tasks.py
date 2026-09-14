"""后台任务：把耗时操作从 HTTP 请求里挪出去。

为什么必须异步：Cloudflare 免费版对源站响应有 **100 秒硬超时**（524），
免费版无法延长。一篇大 PDF 的切分+向量化很容易超过 100 秒，
同步接口必然被 CF 掐断。改成"立刻返回 202 + 任务 id，前端轮询进度"后，
耗时就与 CF 的超时无关了。

任务状态写在 PostgreSQL 里（index_tasks 表），所以刷新页面、重启应用都不丢。
工作线程用 threading.Thread(daemon=True) —— 单进程 uvicorn + 单用户的规模下足够。
若将来上多 worker，把 submit() 换成真正的任务队列（RQ / arq / Celery）即可，
上层接口不变。
"""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from typing import Any, Callable

from app import db


def submit(doc_id: str, fn: Callable[[Callable[[int, int, str], None]], dict], kind: str = "index") -> str:
    """建任务并起线程。fn 接收一个 report(done, total, message) 回调，返回结果字典。"""
    tid = uuid.uuid4().hex[:12]
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO index_tasks (id, doc_id, kind) VALUES (%s, %s, %s)",
            (tid, doc_id, kind),
        )

    def report(done: int, total: int, message: str = "") -> None:
        update(tid, progress=done, total=total, message=message)

    def run() -> None:
        try:
            result = fn(report)
            update(tid, status="done", result=result,
                   message=f"完成：{result.get('chunks', 0)} 块")
        except Exception as exc:  # noqa: BLE001 - 任务失败要落库，不能让线程静默死掉
            update(tid, status="error",
                   message=f"{type(exc).__name__}: {exc}",
                   result={"traceback": traceback.format_exc()[-2000:]})

    threading.Thread(target=run, daemon=True, name=f"task-{tid}").start()
    return tid


def update(tid: str, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k} = %s")
        vals.append(json.dumps(v, ensure_ascii=False) if k == "result" and v is not None else v)
    if "result" in fields and fields["result"] is not None:
        cols[list(fields).index("result")] = "result = %s::jsonb"
    vals.append(tid)
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE index_tasks SET {', '.join(cols)}, updated_at = now() WHERE id = %s",
            vals,
        )


def get(tid: str) -> dict | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM index_tasks WHERE id = %s", (tid,))
        return cur.fetchone()


def latest_for(doc_id: str) -> dict | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM index_tasks WHERE doc_id = %s ORDER BY created_at DESC LIMIT 1",
            (doc_id,),
        )
        return cur.fetchone()


def running() -> list[dict]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM index_tasks WHERE status = 'running' ORDER BY created_at DESC"
        )
        return cur.fetchall()


def reap_orphans() -> int:
    """进程重启后，库里残留的 running 任务其实已经死了 —— 启动时标成 error。"""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE index_tasks SET status = 'error', message = '应用重启，任务中断',"
            " updated_at = now() WHERE status = 'running'"
        )
        return cur.rowcount
