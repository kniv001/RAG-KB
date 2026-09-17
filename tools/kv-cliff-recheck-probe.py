# -*- coding: utf-8 -*-
"""
悬崖复现：同一档连跑三轮，看 32768 到底是恒慢还是时快时慢。

起因：前后三次测量互相矛盾 ——
  · 第 1 次（逐档升到 32768）：9.0 tok/s
  · 第 2 次（同样逐档升）  ：8.8 tok/s
  · 第 3 次（联合上限探针）：第 1 轮 95.7，第 2/3 轮 10.4 / 10.2
  · 第 4 次（共享显存探针）：32768 得 96.9 tok/s，全速

项目文档里已经写过「单次扫描会给出假阳性」，所以这里不猜机制，
先把可复现性本身测出来：同一档连跑三轮 + 记录专用/共享显存。

用法：python tools/kv-cliff-recheck-probe.py
"""
import json
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
NVSMI = r"C:\Windows\System32\nvidia-smi.exe"
NONCE = str(int(time.time()))
PLAN = [(10240, 2), (20480, 2), (28672, 2), (32768, 3), (20480, 2), (32768, 3)]


def post(path, body, timeout=600):
    req = urllib.request.Request(OLLAMA + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def gpu():
    try:
        out = subprocess.run([NVSMI, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return int(out) / 1024
    except Exception:
        return 0


def shared_gb():
    ps = ("(Get-Counter -Counter '\\GPU Adapter Memory(*)\\Shared Usage' "
          "-ErrorAction SilentlyContinue).CounterSamples | "
          "ForEach-Object { [int]$_.CookedValue } | Measure-Object -Sum | "
          "Select-Object -ExpandProperty Sum")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=90).stdout.strip()
    try:
        return int(out) / 2**30
    except ValueError:
        return 0.0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"{'num_ctx':>8}{'轮':>4}{'生成tok/s':>11}{'专用显存':>10}{'共享显存':>10}")
    for ctx, rounds in PLAN:
        for r in range(1, rounds + 1):
            resp = post("/api/generate", {
                "model": CHAT, "prompt": f"[{NONCE}-{ctx}-{r}] 用一句话说明什么是索引。",
                "think": False, "stream": False,
                "options": {"num_ctx": ctx, "num_predict": 24, "temperature": 0},
            })
            ec, ed = resp.get("eval_count") or 0, resp.get("eval_duration") or 0
            rate = (ec / (ed / 1e9)) if ed else 0
            print(f"{ctx:>8}{r:>4}{rate:>11.1f}{gpu():>9.2f}G{shared_gb():>9.2f}G")
    try:
        post("/api/generate", {"model": CHAT, "prompt": "x", "keep_alive": 0,
                               "options": {"num_predict": 1}}, timeout=60)
    except Exception:
        pass


if __name__ == "__main__":
    main()
