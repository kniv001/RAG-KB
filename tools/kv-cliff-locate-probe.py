# -*- coding: utf-8 -*-
"""
定位 KV 悬崖：既然 32768 时 ollama 仍报 100% 驻留显存，那变慢就不是「削层到 CPU」。

llama.cpp 削层时会直接把 size_vram 打下去（层没进显存），所以「100% 驻留 + 10 倍变慢」
只能是另一回事：WDDM 的显存超额（Windows 允许 cudaMalloc 越过物理显存，
多出来的部分落在系统内存里，走 PCIe 取）。这种情形 ollama 自己看不出来，
只有 nvidia-smi 的显存实况能对上。

本脚本：逐档加载 → 生成 → 同时读 nvidia-smi 的真实占用。

用法：python tools/kv-cliff-locate-probe.py
"""
import json
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:4b"
WINDOWS = [20480, 24576, 28672, 32768]
NONCE = str(int(time.time()))
NVSMI = r"C:\Windows\System32\nvidia-smi.exe"


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def gpu():
    try:
        out = subprocess.run(
            [NVSMI, "--query-gpu=memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20).stdout.strip()
        tot, used, free = [int(x) for x in out.split(",")]
        return tot, used, free
    except Exception as e:
        return None


def ps():
    with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=30) as r:
        return json.load(r).get("models", [])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ver = subprocess.run(["ollama", "--version"], capture_output=True, text=True,
                         shell=True).stdout.strip()
    print("ollama:", ver)
    print(f"\n{'num_ctx':>8}{'ollama报驻留':>14}{'nvidia已用':>12}{'显存总量':>10}{'空闲':>8}{'生成tok/s':>11}")
    for ctx in WINDOWS:
        prompt = f"[{NONCE}-{ctx}] 用一句话说明什么是索引。"
        try:
            resp = post("/api/generate", {
                "model": MODEL, "prompt": prompt, "think": False, "stream": False,
                "options": {"num_ctx": ctx, "num_predict": 32, "temperature": 0},
            })
        except Exception as e:
            print(f"{ctx:>8}   失败：{e}")
            continue

        info = next((m for m in ps() if m.get("model", "").startswith(MODEL)), {})
        vram = info.get("size_vram", 0)
        g = gpu()
        ec, ed = resp.get("eval_count") or 0, resp.get("eval_duration") or 0
        rate = (ec / (ed / 1e9)) if ed else 0
        if g:
            tot, used, free = g
            print(f"{ctx:>8}{vram/1e9:>12.2f}G{used/1024:>10.2f}G{tot/1024:>9.1f}G{free/1024:>7.2f}G{rate:>11.1f}")
        else:
            print(f"{ctx:>8}{vram/1e9:>12.2f}G{'n/a':>12}{'':>10}{'':>8}{rate:>11.1f}")

    print("\n—— 32K 档的常驻实况（含桌面占用）——")
    g = gpu()
    if g:
        tot, used, free = g
        print(f"物理显存 {tot/1024:.1f}G，已用 {used/1024:.2f}G，空闲 {free/1024:.2f}G")


if __name__ == "__main__":
    main()
