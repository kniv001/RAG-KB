# -*- coding: utf-8 -*-
"""
q4 KV 有没有把「8b 与向量模型同驻」这条路打开。

背景：`docs/ollama-tuning.md` 判定 qwen3:8b 不能用，理由是**装进来就把 bge-m3 挤出去**
（切换重载 1.8~4.3 s，省下的时间全被重载吃回去）。但那条判定是在
「KV = 144~149 KB/token」的显存算法下做的 —— 现在是 41~48 KB/token，
**同样的窗口只花三分之一的 KV**。而 8b 恰好是唯一在**数字糊化**上干净的模型
（4b 在 18 个值里糊 3 个：600 / 3000 / 24576）。

两段：
  ① 同驻体检：8b 按 8192 / 16384 装载后，embed 延迟还在不在 30~100ms（全驻留）
  ② 数字体检：8b 跑线上摘要提示词，600 / 3000 / 24576 会不会糊

用法：python tools/q4-8b-coexist-probe.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
BIG = "qwen3:8b"
EMBED = "bge-m3"
NONCE = str(int(time.time()))
NVSMI = r"C:\Windows\System32\nvidia-smi.exe"


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ps():
    with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=30) as r:
        return json.load(r).get("models", [])


def gpu_used():
    try:
        out = subprocess.run([NVSMI, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return int(out) / 1024
    except Exception:
        return None


def fit(ctx):
    print(f"—— 8b @ num_ctx={ctx} ——")
    post("/api/generate", {"model": BIG, "prompt": f"[{NONCE}-{ctx}] 用一句话说明什么是索引。",
                           "think": False, "stream": False,
                           "options": {"num_ctx": ctx, "num_predict": 24, "temperature": 0}})
    for r in range(1, 4):
        t0 = time.time()
        post("/api/embed", {"model": EMBED, "input": f"同驻体检 {NONCE}-{ctx}-{r}"})
        ms = (time.time() - t0) * 1000
        resp = post("/api/generate", {"model": BIG, "prompt": f"[{NONCE}-{ctx}-{r}] 索引是什么？",
                                      "think": False, "stream": False,
                                      "options": {"num_ctx": ctx, "num_predict": 24,
                                                  "temperature": 0}})
        ec, ed = resp.get("eval_count") or 0, resp.get("eval_duration") or 0
        rate = (ec / (ed / 1e9)) if ed else 0
        sizes = "  ".join(f"{m['model'].split(':')[0]}={m.get('size_vram', 0)/1e9:.2f}G"
                          for m in ps())
        print(f"  轮{r}  embed {ms:>7.0f}ms   8b 生成 {rate:>6.1f} tok/s   显存 {gpu_used():.2f}G   {sizes}"
              f"   {'✅ 同驻' if ms < 800 else '❌ 被挤出'}", flush=True)


def numbers():
    spec = importlib.util.spec_from_file_location("ssp", os.path.join(HERE, "summary-shape-probe.py"))
    ssp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ssp)
    dgp_spec = importlib.util.spec_from_file_location("dgp", os.path.join(HERE, "digit-garble-probe.py"))
    dgp = importlib.util.module_from_spec(dgp_spec)
    try:
        dgp_spec.loader.exec_module(dgp)
    except SystemExit:
        pass
    old, fresh = dgp.OLD, dgp.FRESH_COLON
    user = f"已有条目：\n{old}\n\n新增对话：\n{fresh}"
    print(f"\n—— 8b 的数字保真（同一个触发例，4b 在 q8 下会糊） ——")
    bad = 0
    for i in range(1, 6):
        resp = post("/api/chat", {"model": BIG, "stream": False, "think": False,
                                  "format": ssp.DELTA_SCHEMA,
                                  "options": {"temperature": 0.2, "num_ctx": 8192},
                                  "messages": [{"role": "system", "content": ssp.DELTA_PROMPT},
                                               {"role": "user", "content": user}]})
        raw = resp.get("message", {}).get("content", "")
        try:
            items = json.loads(raw).get("items") or []
        except Exception:
            items = [raw]
        joined = " ".join(str(x) for x in items)
        hit = dgp.GARBLE.search(joined)
        bad += bool(hit)
        print(f"  #{i} {'❌ 糊了' if hit else '✅ 干净'}　{' | '.join(str(x) for x in items)[:110]}")
        sys.stdout.flush()
    print(f"  → 5 次里糊 {bad} 次")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for ctx in ([int(x) for x in sys.argv[1:]] or [8192, 16384]):
        fit(ctx)
    numbers()
    try:
        post("/api/generate", {"model": BIG, "prompt": "x", "keep_alive": 0,
                               "options": {"num_predict": 1}}, timeout=60)
    except Exception:
        pass


if __name__ == "__main__":
    main()
