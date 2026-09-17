# -*- coding: utf-8 -*-
"""
共享显存探针：32768 那档到底有多少 KV 落到了系统内存。

为什么必须量这个：nvidia-smi 只报专用显存，看不到共享显存（系统内存那部分）。
而 32768 时专用显存并没有顶满（还有 748MiB 空闲），速度却掉了 10 倍 ——
两种解释的后果完全不同：

  A. KV 大部分落在共享显存 → 每个 token 都要把整份 KV 走 PCIe 读一遍
     → 这已经是带宽极限，自研「分块流式」也好不了多少
  B. 只落了一小部分，但驱动在反复换入换出（抖动）
     → 是路径病态，自研的确定性流式有可能明显更好

Windows 的性能计数器能分开这两种：\\GPU Adapter Memory(*)\\Shared Usage。

用法：python tools/shared-vram-probe.py
"""
import json
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
NONCE = str(int(time.time()))
COUNTERS = [r"\GPU Adapter Memory(*)\Shared Usage",
            r"\GPU Adapter Memory(*)\Dedicated Usage"]


def post(path, body, timeout=600):
    req = urllib.request.Request(OLLAMA + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def counters():
    ps = ("Get-Counter -Counter '%s','%s' -ErrorAction SilentlyContinue | "
          "ForEach-Object { $_.CounterSamples } | "
          "ForEach-Object { \"{0}|{1}\" -f $_.Path, [int]$_.CookedValue }"
          % (COUNTERS[0], COUNTERS[1]))
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=90).stdout
    shared = ded = 0
    for line in out.splitlines():
        if "|" not in line:
            continue
        path, val = line.rsplit("|", 1)
        try:
            v = int(val)
        except ValueError:
            continue
        # 累加所有物理适配器实例（多卡/集显会各有一条）
        if "shared usage" in path.lower():
            shared += v
        elif "dedicated usage" in path.lower():
            ded += v
    return shared, ded


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    s0, d0 = counters()
    print(f"基线（无模型）：共享 {s0/2**30:.2f}G  专用 {d0/2**30:.2f}G\n")
    print(f"{'num_ctx':>8}{'生成tok/s':>11}{'共享显存':>11}{'专用显存':>11}{'共享增量':>11}")
    for ctx in (28672, 32768):
        resp = post("/api/generate", {
            "model": CHAT, "prompt": f"[{NONCE}-{ctx}] 用一句话说明什么是索引。",
            "think": False, "stream": False,
            "options": {"num_ctx": ctx, "num_predict": 24, "temperature": 0},
        })
        ec, ed = resp.get("eval_count") or 0, resp.get("eval_duration") or 0
        rate = (ec / (ed / 1e9)) if ed else 0
        s, d = counters()
        print(f"{ctx:>8}{rate:>11.1f}{s/2**30:>10.2f}G{d/2**30:>10.2f}G{(s-s0)/2**30:>10.2f}G")

    try:
        post("/api/generate", {"model": CHAT, "prompt": "x", "keep_alive": 0,
                               "options": {"num_predict": 1}}, timeout=60)
    except Exception:
        pass
    print("\n（聊天模型已卸载）")


if __name__ == "__main__":
    main()
