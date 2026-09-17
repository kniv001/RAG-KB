# -*- coding: utf-8 -*-
"""
两个模型同时在场的真实窗口上限。

为什么要重测：docs/ollama-tuning.md 给的 10240 是**两个模型同时驻留**时测的；
上面那次逐档扫描测的是**聊天模型单独驻留**——到 28672 都还是全速（nvidia 7.22G/8G）。
两个数混在一起会得出「窗口已经到顶」。这里按文档的判定法重测联合上限：

  判定 = 两个模型都 100% 在显存，且**连续 3 轮**都成立
（文档明确写过：单次扫描会给出假阳性，12288 那一档就骗过一次）

症状判据用两个，互为交叉验证：
  · ollama ps 的 size_vram / size
  · 向量调用的实际延迟 —— 全驻留约 56ms，掉到 CPU 是 5474ms（项目里的既有实测）

用法：python tools/joint-window-ceiling-probe.py
"""
import json
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
EMBED = "bge-m3"
WINDOWS = [10240, 12288, 16384, 20480]
ROUNDS = 3
NONCE = str(int(time.time()))


def post(path, body, timeout=600):
    req = urllib.request.Request(OLLAMA + path,
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ps():
    with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=30) as r:
        return {m["model"]: m for m in json.load(r).get("models", [])}


def pct(m):
    if not m or not m.get("size"):
        return None
    return 100.0 * m.get("size_vram", 0) / m["size"]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print(f"{'num_ctx':>8}{'轮':>4}{'chat 显存%':>12}{'embed 显存%':>13}{'embed 延迟':>12}{'生成tok/s':>11}   判定")
    for ctx in WINDOWS:
        verdicts = []
        for r in range(1, ROUNDS + 1):
            try:
                resp = post("/api/generate", {
                    "model": CHAT,
                    "prompt": f"[{NONCE}-{ctx}-{r}] 用一句话说明什么是索引。",
                    "think": False, "stream": False,
                    "options": {"num_ctx": ctx, "num_predict": 24, "temperature": 0},
                })
            except Exception as e:
                print(f"{ctx:>8}{r:>4}   生成失败：{e}")
                continue

            t0 = time.time()
            try:
                post("/api/embed", {"model": EMBED, "input": f"检索延迟探测 {NONCE}-{ctx}-{r}"})
            except Exception as e:
                print(f"{ctx:>8}{r:>4}   向量失败：{e}")
                continue
            embed_ms = (time.time() - t0) * 1000

            ms_ = ps()
            cp, ep = pct(ms_.get(CHAT)), pct(ms_.get(EMBED))
            ec, ed = resp.get("eval_count") or 0, resp.get("eval_duration") or 0
            rate = (ec / (ed / 1e9)) if ed else 0
            ok = (cp is not None and cp >= 99 and ep is not None and ep >= 99 and embed_ms < 500)
            verdicts.append(ok)
            print(f"{ctx:>8}{r:>4}{(cp or 0):>11.0f}%{(ep or 0):>12.0f}%{embed_ms:>11.0f}m{rate:>11.1f}   "
                  f"{'✅ 两者全驻留' if ok else '❌ 有模型掉到 CPU'}")

        if verdicts:
            allok = all(verdicts)
            print(f"{'':>8}    → {ctx}：{'✅ 通过（3/3）' if allok else '❌ 不通过（%d/3）' % sum(verdicts)}")

    # 收尾：把聊天模型卸掉，别让它以 32K 的分配占着用户的显存
    try:
        post("/api/generate", {"model": CHAT, "prompt": "x", "keep_alive": 0,
                               "options": {"num_predict": 1}}, timeout=60)
    except Exception:
        pass
    print("\n（聊天模型已卸载，向量模型留驻）")


if __name__ == "__main__":
    main()
