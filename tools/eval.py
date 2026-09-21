# -*- coding: utf-8 -*-
"""
**模型测评平台的入口**（与管线尺子 `tools/ruler.py` 分开）。

  python tools/eval.py ls                       # 有哪些基准
  python tools/eval.py audit                    # 基准自检（不过就拒跑）
  python tools/eval.py collect answer-quality --model qwen3:4b   # 只采集（跑模型，落盘）
  python tools/eval.py score   answer-quality --model qwen3:4b   # 只判分（**改判据不用重跑模型**）
  python tools/eval.py run     answer-quality --model qwen3:4b   # 采集 + 判分
  python tools/eval.py run answer-quality --model qwen3:8b --repeat 2
  python tools/eval.py speed   --model qwen3:4b --n 5             # 速度维度（分阶段耗时）
  python tools/eval.py compare answer-quality qwen3:4b qwen3:8b   # 对比两个模型的落盘结果

**这个平台量两个维度，换一个模型两个都能量**：质量（`run`/`score`）与速度（`speed`）。

**为什么单独一个模块**：换个模型要重跑的是这一套，不是检索那一套。
报告带的是**模型名**（管线尺子带的是**语料戳**）—— 一条判死必须写清"用哪把尺子量的"，
而这两把尺子的量纲不同，混在一起就没法引用了。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from eval import runner  # noqa: E402


def cmd_ls():
    for b in runner.benches():
        print("  " + runner.describe(runner.load_bench(b)))


def cmd_audit():
    bad = 0
    for b in runner.benches():
        try:
            print(runner.audit_bench(runner.load_bench(b)))
        except SystemExit as e:
            bad += 1
            print(e)
    print(f"\n基准 {len(runner.benches())} 个，通过 {len(runner.benches()) - bad} 个")
    return bad


def cmd_run(argv):
    if not argv:
        raise SystemExit("用法：python tools/eval.py run <基准> --model <模型名> [--limit N] [--repeat N] [--tag 后缀]")
    name, argv = argv[0], argv[1:]
    a = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            a[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    runner.run(name, a.get("model", "qwen3:4b"),
               limit=int(a.get("limit", 0) or 0),
               repeat=int(a.get("repeat", 1) or 1),
               tag=a.get("tag", ""))


def cmd_collect(argv):
    name, argv = argv[0], argv[1:]
    a = _flags(argv)
    runner.collect(name, a.get("model", "qwen3:4b"), int(a.get("limit", 0) or 0),
                   tag=a.get("tag", ""))


def cmd_score(argv):
    name, argv = argv[0], argv[1:]
    a = _flags(argv)
    runner.score(name, a.get("model", "qwen3:4b"), tag=a.get("tag", ""))


def _flags(argv):
    a, i = {}, 0
    while i < len(argv):
        if argv[i].startswith("--"):
            a[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return a


def cmd_speed(argv):
    a = _flags(argv)
    runner.speed(a.get("model", "qwen3:4b"), int(a.get("n", 5) or 5), a.get("bench", "multihop-25"))


def cmd_compare(argv):
    """对比两个模型在同一基准上的落盘结果 —— 逐题比，不跨题平均。"""
    import glob
    import json
    if len(argv) < 3:
        raise SystemExit("用法：python tools/eval.py compare <基准> <模型A> <模型B>")
    bench, a, b = argv[0], argv[1], argv[2]
    def grab(m):
        p = os.path.join(runner.HERE, "_runs", f"{bench}__{m.replace(':','-').replace('/','-')}.json")
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
    da, db = grab(a), grab(b)
    if not da or not db:
        raise SystemExit(f"缺落盘结果：{a if not da else ''} {b if not db else ''} —— 先各跑一次")
    from eval import judges
    wa = wb = tie = 0
    print(f"—— {bench}：{a} vs {b} ——")
    for ra, rb in zip(da["results"], db["results"]):
        ok_a = judges.passes(judges.judge(ra["kind"], ra["answer"], ra["sources"] or [], ra["q"]), ra["kind"])
        ok_b = judges.passes(judges.judge(rb["kind"], rb["answer"], rb["sources"] or [], rb["q"]), rb["kind"])
        if ok_b and not ok_a:
            wb += 1
            print(f"  B 赢  [{ra['kind']}] {ra['q'][:36]}")
        elif ok_a and not ok_b:
            wa += 1
            print(f"  A 赢  [{ra['kind']}] {ra['q'][:36]}")
        else:
            tie += 1
    print(f"\n  {a} 独赢 {wa}　{b} 独赢 {wb}　平 {tie}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
    elif cmd == "ls":
        cmd_ls()
    elif cmd == "audit":
        sys.exit(1 if cmd_audit() else 0)
    elif cmd in ("run", "collect", "score"):
        if not rest:
            raise SystemExit(f"用法：python tools/eval.py {cmd} <基准> --model <模型名>")
        if cmd == "run":
            cmd_run(rest)
        elif cmd == "collect":
            cmd_collect(rest)
        else:
            cmd_score(rest)
    elif cmd == "compare":
        cmd_compare(rest)
    elif cmd == "speed":
        cmd_speed(rest)
    else:
        raise SystemExit(f"不认识：{cmd}\n\n{__doc__}")


if __name__ == "__main__":
    main()
