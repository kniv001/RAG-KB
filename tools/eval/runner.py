# -*- coding: utf-8 -*-
"""
**跑一个模型，出一份报告** —— 平台的执行层。

与管线尺子（`tools/ruler/`）的关键区别：
  · 管线尺子量的是**语料与管线**（检索排第几、分段切得准不准）—— 换模型不影响它
  · 这里量的是**模型本身**（分类判断、引用纪律、诚实性、速度）—— 换个模型就该重跑

所以本模块的一切都以 `--model` 为一等参数，报告里也**必须带模型名** ——
跟管线尺子必须带语料戳是同一条规矩。

跑法是**真实链路**（`/api/chat/stream`，agent 模式）：因为要判的正是端到端行为，
复刻一个"生成"没有意义（模型侧没有便宜的影子）。
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
BENCH_DIR = os.path.join(HERE, "benches")

SCHEMA = "eval/bench@1"


def benches():
    if not os.path.isdir(BENCH_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(BENCH_DIR) if f.endswith(".json"))


def load_bench(name):
    p = os.path.join(BENCH_DIR, name if name.endswith(".json") else name + ".json")
    if not os.path.exists(p):
        raise SystemExit(f"没有这个基准：{name}（现有：{', '.join(benches())}）")
    b = json.load(open(p, encoding="utf-8"))
    if b.get("schema") != SCHEMA:
        raise SystemExit(f"{name} 的 schema 是 {b.get('schema')}，本模块要 {SCHEMA}")
    return b


def audit_bench(b):
    """基准自检：题型必须认识、判据必须存在、id 唯一。**不过就拒跑**。"""
    from . import judges
    bad = []
    ids = [c.get("id") for c in b["cases"]]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        bad.append(f"id 重复：{sorted(dup)}")
    for c in b["cases"]:
        k = c.get("kind")
        if k not in judges.PASS:
            bad.append(f"{c.get('id')} 题型不认识：{k}（应为 {'/'.join(judges.PASS)}）")
        if not c.get("q"):
            bad.append(f"{c.get('id')} 没有题目")
    kinds = {}
    for c in b["cases"]:
        kinds[c.get("kind")] = kinds.get(c.get("kind"), 0) + 1
    lines = [f"基准 {b['name']}（schema {SCHEMA}）· {len(b['cases'])} 题 · "
             + "　".join(f"{k} {v}" for k, v in sorted(kinds.items()))]
    if bad:
        lines.append(f"  ✗ {len(bad)} 处问题：" + "；".join(bad[:5]))
        raise SystemExit("\n".join(lines) + "\n基准没通过审计 —— 先修，别跑数")
    lines.append(f"  ✓ 题型与判据齐备，id 唯一")
    return "\n".join(lines)


def describe(b):
    return f"{b['name']:<18}{len(b['cases']):>3} 题　{b.get('note','')[:64]}"


# ── 采集与判分**分开** ────────────────────────────────────────────────────
# 为什么必须分开：**改判据不该重跑模型**。第一版把它们揉在一个 run() 里，
# 结果为了收紧两条判据又跑了一遍 15 题（≈5 分钟 + 一轮随机波动），
# 而且两次的分不可比。判分是纯函数：给定落盘结果 → 分数。
import subprocess  # noqa: E402

from . import judges  # noqa: E402

_RUNS = os.path.join(HERE, "_runs")


def run_path(bench, model, tag=""):
    return os.path.join(_RUNS, f"{bench}__{model.replace(':', '-').replace('/', '-')}{tag}.json")


_BASE = os.environ.get("KB_BASE", "http://127.0.0.1:8080")
_JAR = os.path.join(os.path.dirname(TOOLS), "rag-kb-web", "target", "rag-kb.jar")


def app_up(timeout=3):
    import urllib.request
    try:
        with urllib.request.urlopen(_BASE + "/actuator/health", timeout=timeout) as r:
            return b'"status":"UP"' in r.read()
    except Exception:
        return False


def ensure_app(wait=120):
    """应用没起来就**自己起**，起来了就直接返回。

    <p>为什么要它：`collect` 假定应用已经在跑，而"手动启动 + 盯着 health 转 20 秒"
    是每次评测都要付的一遍，且**忘了起就直接连接被拒** —— 失败信息还长得像网络问题。
    分段跑（`--only` / 续跑）本来就是为了少等，这一步不自动化就白省了。

    起不来的原因就那几类（没打包 / 密码读不到 / 端口占用），所以失败时把三条都打出来。
    """
    import subprocess
    import time
    if app_up():
        return True
    if not os.path.exists(_JAR):
        raise SystemExit(f"应用没在跑，而且没有 {_JAR} —— 先打包："
                         f"mvn -pl rag-kb-web -am -DskipTests package")
    env = dict(os.environ)
    # 库密码与其它脚本同一个来源
    pw = os.path.join(os.path.dirname(TOOLS), "..", "rag-kb", "data", "pgapp.txt")
    if os.path.exists(pw):
        env.setdefault("KB_DB_PASSWORD", open(pw, encoding="utf-8").read().strip())
    print("（应用没在跑，正在起……）", flush=True)
    subprocess.Popen(["java", "-jar", _JAR], cwd=os.path.dirname(TOOLS), env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(wait // 2):
        time.sleep(2)
        if app_up():
            print("（起来了）", flush=True)
            return True
    raise SystemExit(f"起了 {wait}s 还没 UP —— 查：① 有没有打包 ② KB_DB_PASSWORD 读没读到 "
                     f"③ 8080 是不是被别的进程占了。日志：{os.path.dirname(TOOLS)}/data/app.log")


def collect(bench, model, limit=0, tag="", only=""):
    ensure_app()
    """采集：跑模型，落盘。

    `only` 按 id 或 kind 过滤 ⇒ **不必每次全量测试**（只复验几道题、或只跑某一类）。
    采集端本身**逐题落盘 + 断点续跑**，所以中断了重跑就是接着跑。
    """
    out = run_path(bench, model, tag)
    cmd = ["node", os.path.join(HERE, "collect.mjs"),
           "--bench", bench, "--model", model, "--out", out]
    if limit:
        cmd += ["--limit", str(limit)]
    if only:
        cmd += ["--only", only]
    # 与其它探针一致：从仓库根跑（探针里的路径都是仓库根相对的）
    subprocess.run(cmd, cwd=os.path.dirname(TOOLS), check=False)
    if not os.path.exists(out):
        raise SystemExit(f"采集没产出结果：{out}")
    return out


def score(bench_name, model, paths=None, quiet=False, tag=""):
    """判分：在**已落盘的结果**上跑判据，不碰模型。paths 给多个时取「全部通过」。"""
    b = load_bench(bench_name)
    if not quiet:
        print(audit_bench(b), "\n")
    paths = paths or [run_path(bench_name, model, tag)]
    for p in paths:
        if not os.path.exists(p):
            raise SystemExit(f"没有落盘结果：{p}\n先跑：python tools/eval.py collect {bench_name} --model {model}")
    runs = [json.load(open(p, encoding="utf-8")) for p in paths]
    first = runs[0]

    per = []
    for i, res in enumerate(first["results"]):
        rows = [judges.judge(r["results"][i]["kind"], r["results"][i]["answer"],
                             r["results"][i]["sources"] or [], r["results"][i]["q"],
                             ((r["results"][i].get("stats") or {}).get("cites")))
                for r in runs]
        need = judges.PASS[res["kind"]]
        ok = all(judges.passes(r, res["kind"]) for r in rows)
        per.append({"id": res["id"], "kind": res["kind"], "q": res["q"], "ok": ok,
                    "rows": rows, "answer": res["answer"], "n_sources": len(res["sources"] or []),
                    "ms": res["ms"], "ttftMs": res.get("ttftMs", 0), "error": res.get("error")})
        detail = "　".join(f"{k}={v}" for k, v in rows[0].items() if not k.startswith("_"))
        print(f"  {'✅' if ok else '❌'} [{res['kind']:<9}] {res['q'][:30]:<32}{detail}")

    print(f"\n—— {b['name']} · 模型 {model}"
          + (f" · {len(paths)} 次取全通过" if len(paths) > 1 else "") + " ——")
    kinds = {}
    for p in per:
        k = kinds.setdefault(p["kind"], [0, 0])
        k[1] += 1
        k[0] += p["ok"]
    for k, (ok, n) in sorted(kinds.items()):
        print(f"  {k:<10}{ok}/{n} = {100*ok/n:.0f}%")
    ok = sum(1 for p in per if p["ok"])
    print(f"  {'总计':<10}{ok}/{len(per)} = {100*ok/len(per):.0f}%")

    lat = sorted(p["ms"] for p in per if p["ms"])
    tt = sorted(p["ttftMs"] for p in per if p["ttftMs"])
    if lat:
        print(f"\n  耗时中位 {lat[len(lat)//2]/1000:.1f}s"
              + (f"　TTFT 中位 {tt[len(tt)//2]/1000:.1f}s" if tt else ""))
    # ── token 用量（2026-09-23 加）────────────────────────────────────────
    #
    # **为什么必须单列**："省 token"是一条独立的优化线（分层注入：装入 4894 → 2712，
    # 而耗时基本不变）。此前平台只记 ms / ttftMs ⇒ 那条线的收益**在落盘里一个字都看不到**，
    # 只能靠一次性的速度探针顺手打。判据换了而仪器没跟上，就是"尺子量不到要做的事"。
    #
    # 取 `stats` 事件（Ollama 末帧的原生计时）。**缓存命中时没有 stats** ⇒
    # 顺便把"真跑了几题"报出来（这此前要靠耗时猜，而耗时是连着的）。
    # 只取**带 token 数**的那条：回放路径也会发一条只带 cites 的 stats
    st = [r["stats"] for r in first["results"]
          if (r.get("stats") or {}).get("promptTokens")]
    if st:
        pt = sorted(int(x.get("promptTokens") or 0) for x in st)
        et = sorted(int(x.get("evalTokens") or 0) for x in st)
        pm = sorted(int(x.get("promptMs") or 0) for x in st)
        em = sorted(int(x.get("evalMs") or 0) for x in st)
        m = len(st) // 2
        print(f"\n  装入 token 中位 {pt[m]}（{pt[0]}~{pt[-1]}）"
              f"　生成 token 中位 {et[m]}（{et[0]}~{et[-1]}）"
              f"　合计中位 **{pt[m]+et[m]}**")
        print(f"  prefill 中位 {pm[m]/1000:.1f}s"
              f"　decode 中位 {em[m]/1000:.1f}s"
              f"　decode 速率中位 {et[m]*1000.0/max(1,em[m]):.1f} tok/s")
        if len(st) < len(first["results"]):
            print(f"  ⚠ 只有 {len(st)}/{len(first['results'])} 题带 token 数（其余是**缓存命中**，"
                  f"上面的数只对真跑的那几题成立）")
    # ── 判据命中率（只有多次运行时才给）──────────────────────────────────
    #
    # **为什么必须有这个视图**：2026-09-22 发现答案侧基准已经**饱和** ——
    # 10 次运行里 18/21 题恒过、0 题恒挂，而且**没有任何一题靠近判据边缘**
    # （最长照抄段最高才 47 字/阈值 100、逐字重合最高 38%/阈值 60）。
    # 那样的尺子只会说"过"，**说不出两臂的差别**。
    #
    # 而这一路真正的信号是**数出来的**：『标签泄漏 4/63 vs 0/63，Fisher p≈0.014』——
    # 那是**缺陷率**，不是通过率。通过率把"18 题恒过"和"偶尔挂一次"压成同一个数，
    # 缺陷率不会。所以把每个判据挂了多少次**摊开**，让稀有缺陷自己浮出来。
    if len(runs) > 1:
        tot = len(per) * len(runs)
        cnt = {}
        for i, res in enumerate(first["results"]):
            for r in runs:
                row = judges.judge(r["results"][i]["kind"], r["results"][i]["answer"],
                                   r["results"][i]["sources"] or [], r["results"][i]["q"])
                for k in judges.PASS[res["kind"]]:
                    if row.get(k) is not None:
                        cnt.setdefault(k, [0, 0])
                        cnt[k][1] += 1
                        if not row.get(k):
                            cnt[k][0] += 1
        print(f"\n—— 判据命中率（{len(runs)} 次 × {len(per)} 题 = {tot} 个观测）——")
        for k, (bad_n, n) in sorted(cnt.items(), key=lambda x: -x[1][0]):
            mark = "  ←" if bad_n else ""
            print(f"  {k:<12}{bad_n:>4}/{n:<5}= {100*bad_n/max(1,n):>4.1f}%{mark}")
        print("  （通过率会把'恒过'和'偶尔挂'压成同一个数；缺陷率不会 ——"
              " 两臂比较要看这一列）")

    bad = [p for p in per if not p["ok"]]
    if bad:
        print(f"\n  没通过的 {len(bad)} 题：")
        for p in bad:
            need = judges.PASS[p["kind"]]
            # **None 是"不适用"，不算失败** —— 这里要与 passes() 同一套语义，
            # 否则显示出来的失败原因会包含根本没判的那条（第一版就错在这里）。
            why = "、".join(f"{k}={p['rows'][0].get(k)}" for k in need
                            if p["rows"][0].get(k) is not None and not p["rows"][0].get(k))
            print(f"    ❌ [{p['kind']}] {p['q'][:30]}　（{why or '多轮不一致'}）")
            head = (p["answer"] or "").replace("\n", " ")[:86]
            print(f"       答：{head or p['error'] or '(空)'}")
            if p["rows"][0].get("_数字未落地"):
                print(f"       未落地的数字：{p['rows'][0]['_数字未落地']}")
    return per


def run(bench_name, model, limit=0, repeat=1, tag="", only=""):
    """采集 N 次 → 判分（N>1 时取「全部通过」）。

    `tag` 用来区分**同一模型的不同条件**（如两个开关臂）—— 不留 tag 的话
    第二臂会**覆盖**第一臂的落盘结果，A/B 就只剩后一臂（实测踩过）。
    """
    paths = []
    for r in range(repeat):
        t = (f"__r{r+1}" if repeat > 1 else "") + tag
        print(f"—— 采集 {bench_name} × {model}{tag}" + (f"（第 {r+1}/{repeat} 次）" if repeat > 1 else "") + " ——")
        paths.append(collect(bench_name, model, limit, t, only=only))
        # **每跑完一次就判一次**：分段式输出 —— 不等全部跑完才知道结果
        score(bench_name, model, [paths[-1]], quiet=True)
        print()
    return score(bench_name, model, paths, tag=tag)


def speed(model, n=5, bench="multihop-25"):
    """**速度维度**：跑一次问答并给出分阶段耗时。

    与质量基准共用 `--model` —— 这才是"小型测评平台"该有的样子：
    **换一个模型，质量与速度都能量到**。分阶段耗时见 `latency-probe.mjs`。
    """
    import subprocess
    cmd = ["node", os.path.join(HERE, "latency-probe.mjs"),
           "--n", str(n), "--bench", bench, "--model", model]
    return subprocess.run(cmd, cwd=os.path.dirname(TOOLS), check=False).returncode
