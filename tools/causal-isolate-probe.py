# -*- coding: utf-8 -*-
"""
「因果」恒 0 的二次隔离：是五元框架挤掉了它，还是这个标签本身？

上一轮（causal-zero-probe.py）已定：环境正常（0 解析失败、首尾哨兵逐条一致、标签名送得到），
但塌陷是**标签选择性**的 —— 疑问/转折/举例/「都不是」四个哨兵全过，因果 3/3、条件 1/1 全 false，
连提示词里逐字写着的「因为…所以…」都判否。

六个臂，每次只改一个变量（都用同一批哨兵，答案已知）：
  A 五元原样        基线（与 sentence-type-probe 逐字相同）
  B 单标签          只剩一个定义、一个布尔（每个哨兵 × 5 标签）
  C 五元·顺序倒置    看是不是**位置**把它挤掉了（不是语义）
  D 五元·示例改省略号 prompt 里那个 {"…":false,…} 示例值不再给具体值
                     （「示例值被逐字抄」这个坑今天已栽过两次）
  E 自然语言无 JSON  「有没有表示因果的连词？只回答 有/没有」—— 绕开 JSON+布尔这条路
  F 回显            把句子原样抄回来 —— 管路检查（送进去的和我以为的一样吗）

判读：
  B 说 true            → 五元框架的锅，改成按需单问即可
  B 说 false，E 说 有  → JSON/布尔这条路的问题，不是「看不懂因果」
  B/C/D/E/F 全否       → 才轮到「这个标签它做不到」

用法：python tools/causal-isolate-probe.py
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
HERE = os.path.dirname(os.path.abspath(__file__))
CHAT = "qwen3:4b"
OPTS = {"temperature": 0.1, "num_ctx": 16384}

LABELS = ["疑问", "条件", "因果", "转折", "举例"]
DEFS = {
    "疑问": "在提问（问号、吗、呢、如何、是否）",
    "条件": "含条件从句（如果……则、一旦、当……时）",
    "因果": "讲因果（因为、所以、由于、因此、导致）",
    "转折": "含转折（但是、然而、不过、却）",
    "举例": "在举例（例如、比如、如下）",
}

SENTINELS = [
    ("因为缓存失效，所以请求全部落到了数据库上。", {"因果"}),
    ("这个异常是由于没有释放连接导致的。", {"因果"}),
    ("服务在高峰期崩溃过三次，根因是连接池没有回收。", {"因果"}),
    ("如果连接超时，就重试三次。", {"条件"}),
    ("但是这个方案的缺陷在于扩展性。", {"转折"}),
    ("例如，可以用哈希表把查找降到常数时间。", {"举例"}),
    ("内存为什么会一直涨？", {"疑问"}),
    ("这个函数返回一个整数。", set()),
]


def prompt5(labels, vals="false"):
    body = "\n".join(f"- {L}：{DEFS[L]}" for L in labels)
    ex = ",".join(f'"{L}":{vals}' for L in labels)
    return ("对这句话逐类判断，五类都要给出答案（是/否）：\n\n"
            + body + "\n\n只输出 JSON：{" + ex + "}")


def schema(labels):
    return {"type": "object",
            "properties": {k: {"type": "boolean"} for k in labels},
            "required": labels}


def post(messages, fmt=None, timeout=120, tries=3):
    body = {"model": CHAT, "stream": False, "think": False,
            "options": OPTS, "messages": messages}
    if fmt is not None:
        body["format"] = fmt
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r).get("message", {}).get("content", "")
        except Exception as e:
            last = e
            time.sleep(2)
    return f"<HTTP 失败 {last}>"


def five_way(sent, labels, vals="false"):
    """A / C / D 臂：五元。返回 (集合, 原样)。"""
    txt = post([{"role": "system", "content": prompt5(labels, vals)},
                {"role": "user", "content": sent}], fmt=schema(labels))
    try:
        o = json.loads(txt)
        return {L for L in labels if o.get(L) is True}, txt
    except Exception:
        return None, txt


def single(sent, L):
    """B 臂：只给一个定义、只收一个布尔。"""
    txt = post([{"role": "system", "content": prompt5([L])},
                {"role": "user", "content": sent}], fmt=schema([L]))
    try:
        return json.loads(txt).get(L) is True
    except Exception:
        return None


def natural(sent):
    """E 臂：绕开 JSON，自然语言二选一。"""
    txt = post([{"role": "system", "content": "你是中文语法标注器，只回答「有」或「没有」。"},
                {"role": "user", "content":
                 "下面这句话里有没有表示**因果**关系的连词（因为/所以/由于/因此/导致）？"
                 "只回答「有」或「没有」。\n\n句子：" + sent}])
    if "没有" in txt:
        return False, txt.strip()[:20]
    if "有" in txt:
        return True, txt.strip()[:20]
    return None, txt.strip()[:20]


def echo(sent):
    """F 臂：原样重复。"""
    txt = post([{"role": "user", "content": "请原样重复下面这句话，不要加任何其它内容：\n\n" + sent}])
    norm = lambda s: re.sub(r"\s+", "", s)
    return norm(sent) == norm(txt), txt.strip()[:40]


def fmt_set(s):
    return "全 false" if not s else " ".join(sorted(s))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"哨兵 {len(SENTINELS)} 条 × 6 臂（每臂只改一个变量）\n")
    t0 = time.time()
    ok = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0, "F": 0}

    for sent, exp in SENTINELS:
        print(f"哨兵：{sent}")
        print(f"  期望：{fmt_set(exp) or '五类都不是'}")

        got, _ = five_way(sent, LABELS)
        ok["A"] += (got == exp)
        print(f"  A 五元原样         {fmt_set(got) if got is not None else '解析失败'}")

        bres = {L: single(sent, L) for L in LABELS}      # 每标签只发一次
        bl = " ".join(f"{L} {'T' if bres[L] else 'F'}" for L in LABELS)
        bset = {L for L in LABELS if bres[L]}
        ok["B"] += (bset == exp)
        print(f"  B 单标签           {bl}")

        got, _ = five_way(sent, list(reversed(LABELS)))
        ok["C"] += (got == exp)
        print(f"  C 五元·顺序倒置     {fmt_set(got) if got is not None else '解析失败'}")

        got, _ = five_way(sent, LABELS, vals="...")
        ok["D"] += (got == exp)
        print(f"  D 示例改省略号      {fmt_set(got) if got is not None else '解析失败'}")

        nat, raw = natural(sent)
        e_ok = (nat is (not not exp & {"因果"})) if nat is not None else None
        ok["E"] += bool(e_ok)
        print(f"  E 自然语言(因果)    {'有' if nat else '没有' if nat is False else '?'}"
              f"　原样：{raw}")

        ec, raw = echo(sent)
        ok["F"] += ec
        print(f"  F 回显              {'一致' if ec else '不一致 → ' + raw}")
        print(flush=True)

    n = len(SENTINELS)
    print(f"耗时 {time.time()-t0:.0f}s")
    print(f"—— 各臂与期望一致的哨兵数（满分 {n}）——")
    for k, name in (("A", "五元原样"), ("B", "单标签"), ("C", "五元·顺序倒置"),
                    ("D", "五元·示例省略号"), ("E", "自然语言(只看因果那三句)"),
                    ("F", "回显")):
        print(f"  {k} {name:<22} {ok[k]}/{n}")


if __name__ == "__main__":
    main()
