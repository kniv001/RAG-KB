# -*- coding: utf-8 -*-
"""
KV 越窗的「断崖」探针：num_ctx 从窗口内推到远超显存，看两件事——

  1. 模型还有多少层留在显存（ollama ps 的 size_vram / size）
  2. 生成速度掉多少（eval_duration / eval_count）

为什么要单独量：项目里用过的「掉到 CPU」实测只有向量模型那一次
（bge-m3 掉 CPU，retrieve 56ms → 5474ms）。聊天模型越窗后会怎样，
一直只有「会变慢」这个说法，没有数。

注意：num_ctx 决定的是 **KV 分配量**，在加载时就按满额分配，
所以哪怕提示词很短，装不下时照样会削层 —— 短提示词正好能把
「削层」这一项单独隔离出来。

用法：python tools/kv-vram-cliff-probe.py
"""
import json
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen3:4b"
WINDOWS = [8192, 10240, 12288, 16384, 20480, 32768]
NONCE = str(int(time.time()))


def post(path, body, timeout=600):
    req = urllib.request.Request(OLLAMA + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ps():
    with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=30) as r:
        return json.load(r).get("models", [])


def ms(v):
    return (v or 0) / 1e6


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print(f"{'num_ctx':>8}{'驻留显存':>12}{'占比':>8}{'加载':>9}{'前算':>9}{'生成tok/s':>11}   说明")
    rows = []
    for ctx in WINDOWS:
        # 前缀唯一：否则 Ollama 会复用上一次的 KV，量到的不是冷启动
        prompt = f"[{NONCE}-{ctx}] 用一句话说明什么是索引。"
        try:
            t0 = time.time()
            resp = post("/api/generate", {
                "model": MODEL,
                "prompt": prompt,
                "think": False,
                "stream": False,
                "options": {"num_ctx": ctx, "num_predict": 32, "temperature": 0},
            })
            wall = time.time() - t0
        except Exception as e:
            print(f"{ctx:>8}   失败：{e}")
            continue

        info = None
        for m in ps():
            if m.get("name", "").startswith(MODEL) or m.get("model", "").startswith(MODEL):
                info = m
                break
        vram = (info or {}).get("size_vram", 0)
        size = (info or {}).get("size", 0)
        pct = (100.0 * vram / size) if size else 0

        ec = resp.get("eval_count") or 0
        ed = resp.get("eval_duration") or 0
        rate = (ec / (ed / 1e9)) if ed else 0
        load_ms = ms(resp.get("load_duration"))
        pe_ms = ms(resp.get("prompt_eval_duration"))
        note = "全驻留" if pct >= 99 else f"**{100 - pct:.0f}% 在 CPU**"
        print(f"{ctx:>8}{vram/1e9:>10.2f}G{pct:>7.0f}%{load_ms:>8.0f}m{pe_ms:>8.0f}m{rate:>11.1f}   {note}")
        rows.append((ctx, pct, rate, wall))

    print()
    if rows:
        base = rows[0]
        for ctx, pct, rate, wall in rows:
            drop = (base[2] / rate) if rate else float("inf")
            sec_per_1k = (1000 / rate) if rate else 0
            print(f"num_ctx={ctx:>6}  驻留 {pct:>3.0f}%  生成 {rate:>5.1f} tok/s"
                  f"  （相对 {base[0]} 慢 {drop:.1f}×，每千 token 生成 {sec_per_1k:.0f} 秒）")


if __name__ == "__main__":
    main()
