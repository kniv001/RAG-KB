# -*- coding: utf-8 -*-
"""
KV 量化档位 × 容量墙：q8_0 已经把每 token 从 147KB 压到 ~82KB。
再降一档到 q4_0 能不能把墙从 24576~28672 再推一倍？

判据（与 kv-cliff / joint-ceiling 一致，两个都看）：
  · embed 延迟：全驻留 30~100ms；掉 CPU 是数千毫秒
  · 生成速度：全速 90~100 tok/s；掉 CPU 是 ~10 tok/s
外加 nvidia-smi 实况（ollama ps 会把 WDDM 超额也报成 100% 驻留）。

每 token 的 KV 用「两档 size 差 / 档位差」算，不依赖公式。

用法：python tools/kv-quant-wall-probe.py [档位...]
      默认 2048 16384 32768 49152 65536
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
ROUNDS = 3


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
    except Exception:
        return None


def ps():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=30) as r:
            return json.load(r).get("models", [])
    except Exception:
        return []


def server_cfg():
    """从 ollama ps 拿不到量化档位，读 server.log 的 server config 行。"""
    import os
    log = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Ollama", "server.log")
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "server config" in line and "OLLAMA_KV_CACHE_TYPE" in line:
                    seg = line.split("OLLAMA_KV_CACHE_TYPE:")[1].split()[0]
                    fa = line.split("OLLAMA_FLASH_ATTENTION:")[1].split()[0]
                    last = (seg, fa)
        return last
    except Exception:
        return ("?", "?")


def chat_size(ctx):
    """装载到指定档位，返回模型在 ollama ps 里报的 size（字节）。"""
    post("/api/generate", {
        "model": CHAT, "prompt": f"[{NONCE}-{ctx}] 用一句话说明什么是索引。",
        "think": False, "stream": False,
        "options": {"num_ctx": ctx, "num_predict": 24, "temperature": 0},
    }, timeout=900)
    info = next((m for m in ps() if m.get("model", "").startswith(CHAT)), {})
    return info.get("size", 0), info.get("size_vram", 0)


def probe(ctx):
    """一轮：生成 + embed 延迟，两个判据一起看。"""
    resp = post("/api/generate", {
        "model": CHAT, "prompt": f"[{NONCE}-{ctx}-r] 用一句话说明什么是索引。",
        "think": False, "stream": False,
        "options": {"num_ctx": ctx, "num_predict": 32, "temperature": 0},
    }, timeout=900)
    ec, ed = resp.get("eval_count") or 0, resp.get("eval_duration") or 0
    rate = (ec / (ed / 1e9)) if ed else 0
    t0 = time.time()
    post("/api/embed", {"model": EMBED, "input": f"检索延迟探测 {NONCE}-{ctx}"})
    ms = (time.time() - t0) * 1000
    info = next((m for m in ps() if m.get("model", "").startswith(CHAT)), {})
    return rate, ms, info.get("size", 0), info.get("size_vram", 0)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ctxs = [int(x) for x in sys.argv[1:]] or [2048, 16384, 32768, 49152, 65536]
    seg, fa = server_cfg()
    print(f"ollama {subprocess.run(['ollama', '--version'], capture_output=True, text=True, shell=True).stdout.strip()}")
    print(f"服务端配置：KV_CACHE_TYPE={seg}  FLASH_ATTENTION={fa}\n")

    sizes = {}
    print(f"{'num_ctx':>8}{'轮':>3}{'生成tok/s':>11}{'embed延迟':>11}"
          f"{'size':>9}{'size_vram':>11}{'nvidia已用':>11}   判定")
    for ctx in ctxs:
        sizes[ctx] = chat_size(ctx)[0]
        verdicts = []
        for r in range(1, ROUNDS + 1):
            rate, ms, size, vram = probe(ctx)
            g = gpu()
            used = f"{g[1]/1024:.2f}G" if g else "n/a"
            bad = ms > 800 or rate < 50
            verdicts.append(not bad)
            print(f"{ctx:>8}{r:>3}{rate:>11.1f}{ms:>10.0f}m"
                  f"{size/1e9:>8.2f}G{vram/1e9:>10.2f}G{used:>11}   "
                  f"{'❌ 掉 CPU' if bad else '✅ 全驻留'}", flush=True)
        if all(verdicts):
            print(f"{'':>8}   —— {ctx} 稳定 ✅")
        else:
            print(f"{'':>8}   —— {ctx} 不稳定（{sum(verdicts)}/{ROUNDS}）")

    print("\n—— 每 token 的 KV ——")
    ks = sorted(sizes)
    for a, b in zip(ks, ks[1:]):
        if sizes[a] and sizes[b] and b > a:
            per = (sizes[b] - sizes[a]) / (b - a)
            print(f"  {a}→{b}: {(sizes[b]-sizes[a])/1e6:>7.1f} MB / {b-a} tok "
                  f"= {per/1024:>6.1f} KB/token")

    print("\n—— 收尾：卸载聊天模型 ——")
    try:
        post("/api/generate", {"model": CHAT, "prompt": "x", "keep_alive": 0,
                               "options": {"num_predict": 1}}, timeout=60)
        print("（已卸载）")
    except Exception as e:
        print(f"（卸载失败：{e}）")


if __name__ == "__main__":
    main()
