# -*- coding: utf-8 -*-
"""
前台/后台分槽的可行性：**换载到底几秒**，能不能摊进后台窗口。

设想（用户提的）：前台保持 4b 不变，后台（入库语境行 / 摘要合并 / 旧轮次笔记 / 簇命名）
换一个更大的模型 —— 反正那些活本来就在空闲窗口里跑，换载那几秒没人等。

要注意的是规划与评估**也在前台**、也走 utility-model（`AgenticRagService.utilityRef`），
所以不能直接把 utility-model 换成 8b —— 那样每次提问都要换两次载。要分槽。

本探针只量代价，不判收益：
  · 来回切一次各花多久（Ollama 的 load_duration 是它自己报的装载耗时）
  · 切过去之后 bge-m3 还在不在（8b 必须带 num_gpu 钉层数，否则向量模型被挤）
  · 切回来时用户要等多久

用法：python tools/model-switch-cost-probe.py [后台模型，默认 qwen3:8b] [num_gpu，默认 26]
"""
import json
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
FRONT = "qwen3:4b"
EMBED = "bge-m3"
BACK = sys.argv[1] if len(sys.argv) > 1 else "qwen3:8b"
NUM_GPU = int(sys.argv[2]) if len(sys.argv) > 2 else 26
NONCE = str(int(time.time()))
NVSMI = r"C:\Windows\System32\nvidia-smi.exe"


def post(path, body, timeout=900):
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
        return 0.0


def ps():
    """用**完整**模型名做键 —— 截断到 ':' 之前会让 qwen3:4b 与 qwen3:8b 互相覆盖，
    两个模型的占位就分不清了（第一版栽在这里）。"""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=30) as r:
            ms = json.load(r).get("models", [])
        return {m["model"]: round(m.get("size_vram", 0) / 1e9, 2) for m in ms}
    except Exception:
        return {}


def call(model, tag, num_gpu=None, num_ctx=8192):
    opts = {"num_ctx": num_ctx, "num_predict": 24, "temperature": 0}
    if num_gpu is not None:
        opts["num_gpu"] = num_gpu
    t0 = time.time()
    d = post("/api/generate", {"model": model, "prompt": f"[{NONCE}-{tag}] 索引是什么？",
                               "think": False, "stream": False, "options": opts})
    wall = time.time() - t0
    load = (d.get("load_duration") or 0) / 1e9
    ec, ed = d.get("eval_count") or 0, d.get("eval_duration") or 0
    rate = (ec / (ed / 1e9)) if ed else 0
    t0 = time.time()
    post("/api/embed", {"model": EMBED, "input": f"换载体检 {NONCE}-{tag}"})
    embed_ms = (time.time() - t0) * 1000
    print(f"{tag:<28} 总 {wall:>5.1f}s（其中装载 {load:>4.1f}s）  生成 {rate:>5.1f} tok/s  "
          f"embed {embed_ms:>6.0f}ms  显存 {gpu_used():.2f}G  {ps()}", flush=True)
    return load, wall


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"前台 {FRONT}　后台 {BACK}（num_gpu={NUM_GPU}）　向量 {EMBED}\n")
    print("—— ① 先把前台与向量模型都装上（模拟用户刚用完） ——")
    call(FRONT, "前台首次（冷）")
    call(FRONT, "前台第二次（热）")

    print(f"\n—— ② 切到后台模型（等价于后台任务开工）——")
    call(BACK, f"后台 {BACK} 首次", num_gpu=NUM_GPU)
    call(BACK, f"后台 {BACK} 第二次（热）")

    print(f"\n—— ③ 切回前台（等价于用户这时候提问）——")
    call(FRONT, "前台回来（冷）")
    call(FRONT, "前台回来（热）")

    print("\n判据：②③ 的『装载』那一列就是换载代价；"
          "embed 列看 bge-m3 有没有被挤（<800ms 为驻留）。")


if __name__ == "__main__":
    main()
