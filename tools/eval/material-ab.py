# -*- coding: utf-8 -*-
"""**待办 ⑧ 的材料 A/B** —— 只采材料，不等生成（`tools/eval/material-probe.mjs`）。

为什么把材料与答案分开采：一次 `eval.py run` 是 **21 题 × ~25s**，而 **80% 是生成**
（decode 里 84~86% 是思考）。可"注入了什么"**在生成之前就定完了**，服务端那一刻就打了日志：

    相邻块补全（±1）：命中 N 段 → 新增 M 段 → 候选 K 段
    材料地板 0.65（<前 20 字>）：过滤后剩 N 句 / M 块

⇒ 21 题从 9 分钟降到 **1~2 分钟**。**判据要什么就采什么**：多采的部分不只是浪费，
还会把噪声（生成抖动）引进来 —— 而这一支要判的东西**一点生成都不需要**。

三臂（`off` 是生产默认，一条都不能少）：
    off  {}                                    —— 基线
    nb   {KB_NEIGHBOR: 1}                      —— 只补邻居（全局窗口）
    nbk  {KB_NEIGHBOR: 1, KB_SENT_CHUNK_K: 5}  —— 补邻居 + 逐块 5 句（探针量出的那档）
"""
import io
import json
import os
import re
import socket
import subprocess
import sys
import time

# **控制台是 GBK** —— 不设这个，任何 `⚠`/`⇒` 都会让整个驱动崩在半路
# （实测：第三臂因此**根本没跑**，而输出里只留下一段 traceback）。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TOOLS = os.path.join(REPO, "tools")
LOG = os.path.join(REPO, "data", "app.log")
BENCH = "answer-quality"
JAVA = r"C:\Program Files\Java\jdk-21\bin\java.exe"

ARMS = {
    "off": {},
    "nb": {"KB_NEIGHBOR": "1"},
    "nbk": {"KB_NEIGHBOR": "1", "KB_SENT_CHUNK_K": "5"},
    # **地板按块判**（`mat-floor-mode=block`）：块的最大相似度过关 ⇒ 整块留下。
    # 依据见 `RagProperties.Agent#matFloorMode`（逐句过地板把块内其余句子一起扔了）。
    "blk": {"KB_MAT_FLOOR_MODE": "block"},
}

FLOOR_RE = re.compile(r"材料地板 ([\d.]+)（(.{0,20})）：过滤后剩 (\d+) 句 / (\d+) 块")
NB_RE = re.compile(r"相邻块补全（±(\d+)）：命中 (\d+) 段 → 新增 (\d+) 段 → 候选 (\d+) 段")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def read_log_since(t0):
    """把 `t0`（本臂开始时刻）之后的日志行读出来，**按时间戳筛，不按文件偏移**。

    为什么必须这样（2026-09-29 踩的坑）：第一版记 `app.log` 的**字节偏移**，
    而它会 **10MB 就轮转**（本机开着 DEBUG，实测 ~50 分钟一轮）—— 轮转把
    **本臂前半段的行搬进了 `app.log.1.gz`**，于是偏移法只读到后半段，
    症状是"采到的题数比题集少"，**看起来像探针没发出去**。
    与"只 grep 当前文件得到'没有证据'"是同一条教训（本项目记过一次）。
    ⇒ 时间戳是**跨文件可比**的量，偏移不是。 `.gz` 与当前文件一起扫，按行首时间比较。
    """
    import glob as _g
    import gzip
    out = []
    for path in sorted(_g.glob(LOG + "*")):
        try:
            op = gzip.open if path.endswith(".gz") else io.open
            with op(path, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = TS_RE.match(line)
                    if m and m.group(1) >= t0:
                        out.append(line)
        except Exception as e:
            print(f"  ！（{os.path.basename(path)} 读不动：{e}）")
    return out


def up(port=8080):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except Exception:
        return False


def kill_others():
    """**动手前先确认没有别的实验在跑**（2026-09-29 加，血的教训）。

    实测踩到的样子：上一个驱动（`_neighbor-ab.py`）我杀了它的**子进程**，
    而它自己没死 —— 于是它接着跑下一臂，**两个实验同时抢同一个 8080 和同一块 GPU**：
      · 它以为自己在量 `nb` 臂，其实应用是**我这个驱动刚用 `off` 起的**
      · 我的 21 题里混着它发的另一个基准的请求
    两边的读数**全废**（已隔离到 `_runs/_polluted/`）。

    与本项目反复吃的亏同族（**残留应用实例占 8080**、日志轮转、构建产物没更新、
    答案回放）：**仪器说没事，而事情没做**。⇒ 判据要落在"**有没有别人在动**"上，
    这条与 `kill_stale()` 是一对：**先清场，再开跑**。
    """
    # ⚠️ **只认 python.exe / node.exe**：`bash.exe` / `powershell.exe` 的**命令行里就含
    # 我这条命令的原文**（模式串自己会匹配自己）⇒ 第一版把三个 bash 外壳当成入侵者拒跑了。
    # 这又是一次"判据要先确认它量的是我以为的那个东西"。
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | "
                          "Where-Object { $_.Name -in @('python.exe','node.exe') -and "
                          "$_.CommandLine -match 'neighbor-ab|nb_material|collect.mjs|"
                          "material-probe|eval.py run' } | "
                          "ForEach-Object { \"$($_.ProcessId) $($_.Name)\" }"],
                         capture_output=True, text=True)
    lines = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
    mine = str(os.getpid())
    intruders = [l for l in lines if l.split()[0] != mine]
    if intruders:
        raise SystemExit("！还有别的实验进程在跑，先清干净：\n  " + "\n  ".join(intruders))


def kill_stale():
    """**收掉任何占着 8080 的残留实例** —— 不清的话新实例绑定失败而死，而 `up()` 仍为真
    （旧实例在听）⇒ **整臂被旧配置服务**，读数看着完全正常（实测吃过一次，整轮作废）。"""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
                    "Where-Object {$_.CommandLine -like '*rag-kb.jar*'} | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                   capture_output=True)
    time.sleep(2)


def start(arm, proc):
    kill_stale()
    if proc is not None:
        proc.terminate()
        for _ in range(30):
            time.sleep(0.5)
            if proc.poll() is not None:
                break
        else:
            proc.kill()
    for _ in range(60):
        if not up():
            break
        time.sleep(0.5)
    env = dict(os.environ)
    env["KB_DB_PASSWORD"] = io.open(r"D:\vs\rag-kb\data\pgapp.txt",
                                    encoding="utf-8").read().strip()
    env["KB_FEED_POLL"] = "true"
    env["KB_WEB_ENABLED"] = "true"
    env.update(ARMS[arm])
    p = subprocess.Popen([JAVA, "-jar", os.path.join(REPO, "rag-kb-web", "target",
                                                     "rag-kb.jar")],
                         cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(2)
        if up():
            time.sleep(3)
            return p
        if p.poll() is not None:
            raise SystemExit(f"应用启动即退出（arm={arm}）")
    raise SystemExit("应用没起来")


def probe():
    subprocess.run(["node", os.path.join(TOOLS, "clear-answers.mjs")],
                   cwd=REPO, check=False, stdout=subprocess.DEVNULL)
    subprocess.run(["node", os.path.join(TOOLS, "eval", "material-probe.mjs"),
                    "--bench", BENCH, "--model", "qwen3:4b", "--wait", "9000"],
                   cwd=REPO)


def main():
    kill_others()
    want = [a.strip() for a in sys.argv[1:] if a.strip()] or list(ARMS)
    bench = json.load(io.open(os.path.join(TOOLS, "eval", "benches", BENCH + ".json"),
                              encoding="utf-8"))
    qs = {c["id"]: c["q"] for c in bench["cases"]}
    qs = {i: q for i, q in qs.items() if q}          # 题面可能被替换，只留真跑的

    proc, data = None, {}
    for arm in want:
        print(f"\n========== 臂 {arm}　{ARMS[arm] or '（生产默认）'} ==========", flush=True)
        proc = start(arm, proc)
        t0 = time.strftime("%Y-%m-%dT%H:%M:%S")
        probe()
        lines = read_log_since(t0)
        floor = {}
        for line in lines:
            m = FLOOR_RE.search(line)
            if m:
                # 同一题在一次问答里会打两遍（装配一次、回答前再装配一次）——取**后一条**，
                # 那是真正进提示词的那次（与本项目其它读数同一口径）
                floor[m.group(2)] = (int(m.group(3)), int(m.group(4)))
        nbs = [(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
               for m in (NB_RE.search(l) for l in lines) if m]
        data[arm] = {"floor": floor, "nb": nbs}
        print(f"  采到地板行 {len(floor)} 题 / 补全行 {len(nbs)} 条"
              f"（题集 {len(qs)} 题，本臂日志 {len(lines)} 行）", flush=True)
        if len(floor) < len(qs):
            print(f"  ！只采到 {len(floor)}/{len(qs)} 题的地板行 —— 别拿它当全集")

    print("\n\n===== 逐题材料对照（过滤后剩 句/块）=====")
    print(f"{'题':<8}{'off':>12}{'nb':>12}{'nbk':>12}   变化")
    tot = {a: [0, 0] for a in want}
    gain = {"nb": 0, "nbk": 0}
    for i, q in qs.items():
        pre = q[:20]
        cells, vals = [], {}
        for a in want:
            v = data[a]["floor"].get(pre)
            vals[a] = v
            cells.append(f"{v[0]}/{v[1]}" if v else "—")
            if v:
                tot[a][0] += v[0]
                tot[a][1] += v[1]
        d = ""
        if vals["off"] and vals["nb"]:
            if vals["nb"] > vals["off"]:
                d = "nb ↑"
                gain["nb"] += 1
            elif vals["nb"] < vals["off"]:
                d = "nb ↓"
        if vals["off"] and vals["nbk"] and vals["nbk"] > vals["off"]:
            gain["nbk"] += 1
        print(f"{i:<8}{cells[0]:>12}{cells[1]:>12}{cells[2]:>12}   {d}")

    print(f"\n{'合计':<8}" + "".join(f"{tot[a][0]}/{tot[a][1]:<10}" for a in want)
          + f"\n（句/块 合计）　材料变多的题："
          + "、".join(f"{a} {gain[a]} 道" for a in want if a in gain) + f" / 共 {len(qs)} 道")
    print("\n补邻居的读数：")
    for a in [x for x in want if x != "off"]:
        if data[a]["nb"]:
            h = sum(x[1] for x in data[a]["nb"])
            n = sum(x[2] for x in data[a]["nb"])
            c = sum(x[3] for x in data[a]["nb"])
            print(f"  {a}: {len(data[a]['nb'])} 次调用　命中 {h} 段 → **新增 {n} 段** → 候选 {c} 段"
                  f"（新增率 {100.0 * n / max(1, h):.0f}%）")

    proc = start("off", proc)
    print("\n（应用已按生产默认重启）")
    print("判据：① 地板存活句/块**上去**（这一支要治的就是它）② 上去的**代价**（块数 = token）")


if __name__ == "__main__":
    main()
