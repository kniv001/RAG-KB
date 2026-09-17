# -*- coding: utf-8 -*-
"""
联合上限 v2：判据换成延迟，并加对照组。

v1 用 ollama ps 的 size_vram/size 判定，结果 embed 一律报 0% —— 但它的实际延迟
只有 30~60ms（GPU 速度）。也就是说这个字段在 embed 模型上不可信，
文档里那个「22% 在显存」的判据不能直接照搬。

改用两个可直接观察的量：
  · embed 延迟：全驻留约 56ms（项目既有实测），掉 CPU 是 5474ms
  · 生成速度：全速约 92~100 tok/s

并加对照组：先把聊天模型按 32768 装载（已实测会掉到 ~9 tok/s），
看那时 embed 延迟是多少 —— 若确实跳到数千毫秒，判据即被验证。

用法：python tools/joint-ceiling-v2-probe.py
"""
import json
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
EMBED = "bge-m3"
NONCE = str(int(time.time()))
NVSMI = r"C:\Windows\System32\nvidia-smi.exe"


def post(path, body, timeout=600):
    req = urllib.request.Request(OLLAMA + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def gpu_used():
    try:
        out = subprocess.run([NVSMI, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return int(out) / 1024
    except Exception:
        return None


def load_chat(ctx):
    return post("/api/generate", {
        "model": CHAT, "prompt": f"[{NONCE}-{ctx}] 用一句话说明什么是索引。",
        "think": False, "stream": False,
        "options": {"num_ctx": ctx, "num_predict": 24, "temperature": 0},
    })


def embed_latency(tag):
    t0 = time.time()
    post("/api/embed", {"model": EMBED, "input": f"检索延迟探测 {NONCE}-{tag}"})
    return (time.time() - t0) * 1000


def probe(ctx, rounds=3):
    out = []
    for r in range(rounds):
        resp = load_chat(ctx)
        ms = embed_latency(f"{ctx}-{r}")
        ec, ed = resp.get("eval_count") or 0, resp.get("eval_duration") or 0
        rate = (ec / (ed / 1e9)) if ed else 0
        out.append((ms, rate, gpu_used()))
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"{'num_ctx':>8}{'轮':>4}{'embed延迟':>11}{'生成tok/s':>11}{'显存已用':>10}   判定")
    print("—— 对照组：已知会掉 CPU 的 32768 ——")
    for r, (ms, rate, used) in enumerate(probe(32768), 1):
        print(f"{32768:>8}{r:>4}{ms:>10.0f}m{rate:>11.1f}{(used or 0):>9.2f}G   "
              f"{'❌ 掉 CPU' if ms > 800 else '✅ 全驻留'}")

    for ctx in (24576, 28672):
        print(f"—— 试 {ctx} ——")
        for r, (ms, rate, used) in enumerate(probe(ctx), 1):
            print(f"{ctx:>8}{r:>4}{ms:>10.0f}m{rate:>11.1f}{(used or 0):>9.2f}G   "
                  f"{'❌ 掉 CPU' if ms > 800 else '✅ 全驻留'}")

    try:
        post("/api/generate", {"model": CHAT, "prompt": "x", "keep_alive": 0,
                               "options": {"num_predict": 1}}, timeout=60)
    except Exception:
        pass
    print("\n（聊天模型已卸载）")


if __name__ == "__main__":
    main()
