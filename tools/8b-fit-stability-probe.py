# -*- coding: utf-8 -*-
"""
8b 在 q4 下到底是"放得下"还是"贴着墙"：同一档位跑两遍给出不同答案，得先把这个钉死。

先行观察（同一天、同一档位 8192）：
  第一次 8b 占 5.32G，bge-m3 同驻（embed 44~70ms）
  第二次 8b 占 6.19G，**bge-m3 被挤出**（embed 3520ms）
差别不在配置，在**装载那一刻有多少空闲显存** —— Ollama 据此决定往 GPU 上放几层。

所以这里做两件事：
  ① 稳定性：反复装载 3 次（每次先卸载），看 8b 的 size_vram 与 bge-m3 是否同驻
  ② 找可控点：用请求级 `num_gpu` 限制上 GPU 的层数，看能不能换来"两者都稳"

判据：embed 延迟 30~100ms 才算 bge-m3 驻留（掉 CPU 是数千毫秒）；8b 生成 tok/s 参考值。

用法：python tools/8b-fit-stability-probe.py [num_ctx，默认 8192]
"""
import json
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
BIG = "qwen3:8b"
EMBED = "bge-m3"
CTX = int(sys.argv[1]) if len(sys.argv) > 1 else 8192
NONCE = str(int(time.time()))
NVSMI = r"C:\Windows\System32\nvidia-smi.exe"


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ps():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=30) as r:
            return json.load(r).get("models", [])
    except Exception:
        return []


def gpu_used():
    try:
        out = subprocess.run([NVSMI, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return int(out) / 1024
    except Exception:
        return 0.0


def unload(model):
    try:
        post("/api/generate", {"model": model, "prompt": "x", "keep_alive": 0,
                               "options": {"num_predict": 1}}, timeout=60)
    except Exception:
        pass


def trial(label, num_gpu=None, rounds=3):
    unload(BIG)
    unload(EMBED)
    opts = {"num_ctx": CTX, "num_predict": 24, "temperature": 0}
    if num_gpu is not None:
        opts["num_gpu"] = num_gpu
    post("/api/generate", {"model": BIG, "prompt": f"[{NONCE}-{label}] 索引是什么？",
                           "think": False, "stream": False, "options": opts})
    sizes = {m["model"].split(":")[0]: m.get("size_vram", 0) / 1e9 for m in ps()}
    ms_list, rates = [], []
    for r in range(rounds):
        t0 = time.time()
        post("/api/embed", {"model": EMBED, "input": f"同驻 {NONCE}-{label}-{r}"})
        ms_list.append((time.time() - t0) * 1000)
        resp = post("/api/generate", {"model": BIG, "prompt": f"[{NONCE}-{label}-{r}] 索引是什么？",
                                      "think": False, "stream": False, "options": opts})
        ec, ed = resp.get("eval_count") or 0, resp.get("eval_duration") or 0
        rates.append((ec / (ed / 1e9)) if ed else 0)
    # 第 1 轮含 bge-m3 的冷加载，**不算**；判定要看第 2 轮起每一轮（全都要 <800ms 才叫稳）
    warm = ms_list[1:] or ms_list
    ok = all(m < 800 for m in warm)
    print(f"{label:<14} 8b={sizes.get('qwen3', 0):.2f}G  显存 {gpu_used():.2f}G  "
          f"embed [{' '.join(f'{m:.0f}' for m in ms_list)}]ms  "
          f"生成 [{' '.join(f'{r:.0f}' for r in rates)}] tok/s  "
          f"{'✅ 每轮都驻留' if ok else '❌ 有轮次被挤出'}", flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"num_ctx={CTX}　判据：**第 2 轮起每一轮** embed 都 <800ms 才算 bge-m3 驻留"
          f"（第 1 轮含冷加载，不计）\n")
    if len(sys.argv) > 2:      # 单档深测：python tools/8b-fit-stability-probe.py 8192 26
        g = int(sys.argv[2])
        rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        print(f"—— 单档深测 num_gpu={g}，{rounds} 轮 ——")
        trial(f"num_gpu={g}", num_gpu=g, rounds=rounds)
        unload(BIG)
        return
    print("—— ① 稳定性：同样配置连装 3 次 ——")
    for i in range(1, 4):
        trial(f"默认 #{i}")
    print("\n—— ② 可控点：请求级 num_gpu 限制上 GPU 的层数（qwen3:8b 共 36 层）——")
    for g in (30, 26, 22, 18):
        trial(f"num_gpu={g}", num_gpu=g)
    print("\n（num_gpu 越小 → 8b 越慢，但给 bge-m3 留的位置越多；要的是两者都稳的那一档）")
    unload(BIG)


if __name__ == "__main__":
    main()
