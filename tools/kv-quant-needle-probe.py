# -*- coding: utf-8 -*-
"""
q4_0 的 KV 把墙从 ~28K 推到 49152 —— 但**墙能装下不等于窗口能用**。
量化噪声随上下文变长累积，最直接的体检是「长上下文里还找不找得到事实」。

做法：拿真实语料块拼一段 ~24K 字的长上下文，在 5%/20%/35%/55%/75%/95% 六个深度
各埋一条**语料里不存在**的事实（代号 + 值），逐条提问，看答案里有没有那个值。

判据：命中数 / 6，外加 prefill 耗时与生成速度（q8_0 在同档位会掉 CPU，但**掉 CPU 只影响
速度、不影响数值** —— 所以同档位下 q4 与 q8 的命中数可以直接对比）。

用法：
  python tools/kv-quant-needle-probe.py q4     # 服务端 KV_CACHE_TYPE=q4_0
  python tools/kv-quant-needle-probe.py q8     # 服务端 KV_CACHE_TYPE=q8_0（慢，但可跑）
结果写进 tools/_needle-<档位>.json，便于两档对比。
"""
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OLLAMA = "http://127.0.0.1:11434"
CHAT = "qwen3:4b"
PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
BS = chr(92)
CTX = 32768
TARGET_CHARS = 24000     # 中文约 1 token/字，留出提示词与回答的余量

NEEDLES = [
    (0.05, "KX-318", "评审人", "蒲远舟"),
    (0.20, "KX-742", "额定压力", "4.7 MPa"),
    (0.35, "KX-905", "验收日期", "2027 年 3 月 11 日"),
    (0.55, "KX-127", "备用件编号", "BR-60221"),
    (0.75, "KX-463", "维护周期", "每 19 天"),
    (0.95, "KX-880", "负责部门", "第九计量室"),
]


def psql_rows(sql):
    f = os.path.join(HERE, "_needle.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    raw = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                         capture_output=True, env=env).stdout.decode("utf-8", "replace")
    out = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        for esc, real in ((BS + BS, BS), (BS + "n", "\n"), (BS + "r", "\r"), (BS + "t", "\t")):
            line = line.replace(esc, real)
        out.append(line)
    return out


def build_context():
    rows = psql_rows("SELECT content FROM chunks ORDER BY random() LIMIT 120")
    filler, total = [], 0
    for r in rows:
        filler.append(r)
        total += len(r)
        if total >= TARGET_CHARS:
            break
    text = "\n\n".join(filler)[:TARGET_CHARS]
    # 从后往前插，避免前面的插入挪动后面的深度
    for depth, code, field, value in sorted(NEEDLES, reverse=True):
        pos = min(int(len(text) * depth), len(text) - 1)
        end = text.find("\n", pos)
        end = len(text) if end < 0 else end
        line = f"\n【内部记录】代号 {code} 的{field}是 {value}。\n"
        text = text[:end] + line + text[end:]
    return text


SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


def ask(context, question, timeout=1800):
    # 必须 think:false **且** format：只有 format 才把第一个 token 钉在 `{` 上，
    # 思考/前言就没有地方可写。少了 format，24 token 全被「我需要在资料里找…」吃掉，
    # 命中数会假报成 0（第一版就栽在这里）。
    body = {"model": CHAT, "stream": False, "think": False, "format": SCHEMA,
            "options": {"temperature": 0, "num_ctx": CTX, "num_predict": 80},
            "messages": [{"role": "system", "content": "根据资料回答，只给答案本身，不要解释。"},
                         {"role": "user", "content": f"资料：\n{context}\n\n问题：{question}"}]}
    req = urllib.request.Request(OLLAMA + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    took = time.time() - t0
    raw = d.get("message", {}).get("content", "").strip()
    try:
        ans = str(json.loads(raw).get("a", "")).strip()
    except Exception:
        ans = raw
    ec, ed = d.get("eval_count") or 0, d.get("eval_duration") or 0
    pc, pd = d.get("prompt_eval_count") or 0, d.get("prompt_eval_duration") or 0
    return (ans, took, pc, (pd / 1e9 if pd else 0), (ec / (ed / 1e9) if ed else 0))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tag = (sys.argv[1] if len(sys.argv) > 1 else "q4").strip()
    ctx_text = build_context()
    print(f"档位 {tag}　上下文 {len(ctx_text)} 字　num_ctx={CTX}　埋了 {len(NEEDLES)} 条\n")
    print(f"{'深度':>6}{'代号':>9}{'命中':>6}{'耗时':>8}{'prompt token':>14}{'prefill':>10}{'tok/s':>8}   回答")
    hits, rows = 0, []
    for depth, code, field, value in NEEDLES:
        ans, took, pc, pds, rate = ask(ctx_text, f"代号 {code} 的{field}是什么？")
        ok = value.replace(" ", "") in ans.replace(" ", "") or value in ans
        hits += ok
        rows.append({"depth": depth, "code": code, "field": field, "value": value,
                     "answer": ans, "hit": bool(ok), "prompt_tokens": pc,
                     "prefill_s": round(pds, 2), "tok_s": round(rate, 1),
                     "wall_s": round(took, 1)})
        print(f"{depth*100:>5.0f}%{code:>9}{'✅' if ok else '❌':>5}{took:>7.1f}s{pc:>14}"
              f"{pds:>9.1f}s{rate:>8.1f}   {ans[:40]}")
        sys.stdout.flush()
    print(f"\n命中 {hits}/{len(NEEDLES)}")
    out = os.path.join(HERE, f"_needle-{tag}.json")
    io.open(out, "w", encoding="utf-8").write(
        json.dumps({"tag": tag, "ctx_chars": len(ctx_text), "hits": hits, "rows": rows},
                   ensure_ascii=False, indent=1))
    print(f"写入 {out}")


if __name__ == "__main__":
    main()
