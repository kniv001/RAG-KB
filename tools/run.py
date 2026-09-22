# -*- coding: utf-8 -*-
"""
**统一运行记录器** —— 让每一次跑都留下可回溯的完整日志。

## 为什么需要它

探针的输出此前**只活在终端里**，于是：
  · 跑长任务时习惯性 `| tail -20`，**前面的全丢了**（实测踩过：一次 A/B 的
    臂边界和第一臂整段没了，只能靠判分日志反推）
  · 事后想回溯"那次用的什么参数、什么语料戳"—— **答不上来**，只能靠记忆
  · 崩在半路时，堆栈和它前面几百行上下文一起没了

判分那边本来就有落盘（`tools/eval/_runs/*.json`），但它存的是**结果**，
不是**过程**。这里补的是过程。

## 用法（一条命令，不挑平台）

    python tools/run.py <名字> -- <命令...>

例：
    python tools/run.py latency -- node tools/latency-probe.mjs --n 5
    python tools/run.py topk -- powershell -File tools/ab.ps1 -Switch KB_TWO_STAGE

产物：`tools/_runs/<名字>/<时间戳>/`
    `run.log`    —— 完整的 stdout + stderr（一字不删）
    `meta.json`  —— 参数、git 提交、语料戳、开关状态、起止时间、退出码

## **语料戳与 git 提交一定要记**

这个项目反复吃过"数字对不上，查半天发现是语料重建过 / 代码版本不同"的亏
（`corpus.stamp` 与"换解析器不让缓存失效"都是这么来的）。
记下来是零成本，事后补是做不到的。
"""
import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT_ROOT = os.path.join(HERE, "_runs")


def _git_commit(cwd):
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd,
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except Exception:
        return None


def _git_dirty(cwd):
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                           capture_output=True, text=True, timeout=10)
        return bool(r.stdout.strip())
    except Exception:
        return None


def _corpus_stamp():
    """语料戳 —— 拿不到就记 null（**别让记录器本身成为失败点**）。"""
    try:
        sys.path.insert(0, HERE)
        from ruler import corpus
        return corpus.load().stamp
    except Exception:
        return None


def meta(name, argv):
    """本次跑的"身份证"。事后回溯全靠它。"""
    return {
        "name": name,
        "cmd": argv,
        "cwd": os.getcwd(),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ragkbCommit": _git_commit(REPO),
        "ragkbDirty": _git_dirty(REPO),
        "ledgerCommit": _git_commit(os.path.join(REPO, "..")),
        "corpusStamp": _corpus_stamp(),
        # 开关状态 —— 这几个直接决定"同一条命令跑出什么结果"
        "switches": {k: v for k, v in sorted(os.environ.items()) if k.startswith("KB_")},
        "python": sys.version.split()[0],
    }


def ls(limit=25):
    """回溯入口：不进去翻目录就能看到跑过什么。

    **按时间倒序、带语料戳与退出码** —— 这三个是事后最常要对的：
    "那次是什么时候跑的""语料动过没有""跑成功了没有"。
    """
    if not os.path.isdir(OUT_ROOT):
        print("还没有任何运行记录")
        return 0
    rows = []
    for name in os.listdir(OUT_ROOT):
        nd = os.path.join(OUT_ROOT, name)
        if not os.path.isdir(nd):
            continue
        for ts in os.listdir(nd):
            mp = os.path.join(nd, ts, "meta.json")
            if os.path.exists(mp):
                try:
                    m = json.load(io.open(mp, encoding="utf-8"))
                except Exception:
                    continue
                rows.append((ts, name, m))
    rows.sort(reverse=True)
    print(f"{'时间':<16}{'名字':<18}{'退码':>5}{'秒':>7}  语料戳 / 提交")
    for ts, name, m in rows[:limit]:
        print(f"{ts:<16}{name:<18}{str(m.get('exitCode')):>5}{str(m.get('seconds')):>7}  "
              f"{m.get('corpusStamp')} / {m.get('ragkbCommit')}"
              f"{'*' if m.get('ragkbDirty') else ''}")
    print(f"\n共 {len(rows)} 次。完整日志：tools/_runs/<名字>/<时间戳>/run.log")
    return 0


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ("ls", "list"):
        return ls()
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print(__doc__)
        return 2
    name = sys.argv[1]
    cmd = sys.argv[3:]

    ts = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(OUT_ROOT, name, ts)
    os.makedirs(d, exist_ok=True)
    log_path = os.path.join(d, "run.log")
    meta_path = os.path.join(d, "meta.json")
    m = meta(name, cmd)
    json.dump(m, io.open(meta_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    banner = (f"==== {name}　{ts} ====\n"
              f"命令 {' '.join(cmd)}\n"
              f"语料戳 {m['corpusStamp']}　ragkb {m['ragkbCommit']}"
              f"{'（有未提交改动）' if m['ragkbDirty'] else ''}\n"
              f"开关 {m['switches'] or '（无）'}\n"
              f"{'=' * 60}\n")
    print(banner, end="")

    t0 = time.time()
    # **两个流都收，且一字不删** —— 崩在半路时前面几百行正文才是关键上下文
    with io.open(log_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(banner)
        f.flush()
        # 子进程自己也吐 utf-8，别让父进程的代码页把它截断
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=env, bufsize=1, text=True,
                             encoding="utf-8", errors="replace")
        for line in p.stdout:
            f.write(line)
            f.flush()                       # **逐行 flush**：崩了也留得下
            sys.stdout.write(line)
            sys.stdout.flush()
        rc = p.wait()

    m["exitCode"] = rc
    m["seconds"] = round(time.time() - t0, 1)
    m["log"] = log_path
    json.dump(m, io.open(meta_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n==== 退出码 {rc}　耗时 {m['seconds']}s ====")
    print(f"完整日志 → {log_path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
