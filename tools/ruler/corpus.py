# -*- coding: utf-8 -*-
"""
**语料层** —— 所有尺子的唯一语料入口。psql 的坑、向量缓存的坑、按内容定位，全收在这里。

为什么要有这一层（2026-09-20 的教训，八条失败模式里有五条落在这）：
  · psql COPY 在 Windows 上给 **CRLF**，末尾字段带 `\\r` ⇒ 文档名比对永远为假、**全场 0**
  · 反转义必须**单遍**：顺序 replace 会把 LaTeX 的 `\\text{其中}` 变成「制表符 + ext{…}」
  · 向量缓存按**位置**对齐 ⇒ 语料一重建就**静默错位**（实测 661 条里 438 条错）
  · 没有**语料戳** ⇒ 一个数字脱离语料就没法引用，判死会随语料漂移
  · 靶子按 `seq` 定位 ⇒ 语料一重切就指向别的块

本层的对外承诺：
  · `stamp` ——**语料身份**（块数 + 每块 doc/seq/正文哈希），任何报告都带着它
  · `vecs(kind)` —— 取向量，**先验戳**；对不上自动重建（这是唯一会写缓存的地方）
  · `find(excerpt, doc=None)` —— **按内容**定位块，返回下标列表（可能多块）
"""
import hashlib
import io
import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)

PSQL = r"D:\vs\rag-kb\pgsql\bin\psql.exe"
PGPASS = r"D:\vs\rag-kb\data\pgapp.txt"
OLLAMA = "http://127.0.0.1:11434"
EMBED_MODEL = "bge-m3"

_BS = chr(92)
_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def unescape(s):
    """**单遍**反转义。顺序 replace 会把 `\\text{其中}` 变成「制表符 + ext{…}」，字段切歪。"""
    out, i = [], 0
    while i < len(s):
        if s[i] == _BS and i + 1 < len(s):
            out.append(_ESC.get(s[i + 1], _BS + s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def norm(s):
    """**比对用的规范形**：反转义 + 去掉所有空白。全库统一用这一个，别再各写各的。"""
    return re.sub(r"\s+", "", unescape(s))


def psql_rows(sql, tag="corpus"):
    # **结尾分号要剥掉**：本函数把查询包进 `COPY (...)`，而 `COPY (SELECT ...;)` 是语法错误。
    # 2026-09-22 踩到：调用方带了个分号 ⇒ 每条查询都报错，而**报错信息只进 stderr**，
    # 函数返回空列表 ⇒ 被读成"表里没有"。（那次我以为"非零退出"就是判据，见下。）
    sql = sql.strip().rstrip(";")
    f = os.path.join(TOOLS, f"_ruler_{tag}.sql")
    io.open(f, "w", encoding="utf-8").write(f"COPY ({sql}) TO STDOUT;")
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    r = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb", "-f", f],
                       capture_output=True, env=env)
    # **判据是 stderr，不是退出码** —— 2026-09-22 实测：SQL 语法错误时
    # **psql 的退出码仍然是 0**，所以"非零才喊"那一版完全抓不住。
    # 这正是「尺子坏掉时不报错，只让结果悄悄变空」的又一例 —— 而且是我自己修了一次
    # 还没修对的那种。
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0 or "ERROR" in err.upper():
        raise RuntimeError(f"psql 失败（退出码 {r.returncode}）：{err[:400]}")
    raw = r.stdout.decode("utf-8", "replace")
    # `\r` 不去掉的话，每行最后一个字段永远比不中（2026-09-20 踩过：全场 0 命中）
    return [[unescape(x) for x in ln.rstrip("\r").split("\t")]
            for ln in raw.split("\n") if ln.strip()]


def psql_script(sql_text, tag="exec"):
    """**写路径**：执行一段 SQL 脚本（脚本里自带 BEGIN/COMMIT）。

    与 {@link psql_rows} 用同一套连接参数与**同一个静默失败的判据** ——
    这里尤其要紧：写操作如果静默失败，读回来会是"表是空的"，
    而那是本项目最贵的一类误读（`psql` 在 SQL 出错时**退出码仍然是 0**）。
    """
    f = os.path.join(TOOLS, f"_ruler_{tag}.sql")
    io.open(f, "w", encoding="utf-8").write(sql_text)
    env = dict(os.environ)
    env["PGPASSWORD"] = io.open(PGPASS, encoding="utf-8").read().strip()
    env["PGCLIENTENCODING"] = "UTF8"
    r = subprocess.run([PSQL, "-h", "127.0.0.1", "-U", "ragkb", "-d", "ragkb",
                        "-v", "ON_ERROR_STOP=1", "-f", f],
                       capture_output=True, env=env)
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0 or "ERROR" in err.upper():
        raise RuntimeError(f"psql 写失败（退出码 {r.returncode}）：{err[:600]}")
    return r.stdout.decode("utf-8", "replace")


def copy_escape(s):
    """COPY 文本格式的转义（`psql_rows` 那边是反转义，这里是它的逆）。"""
    return (s.replace("\\", "\\\\").replace("\t", "\\t")
             .replace("\n", "\\n").replace("\r", "\\r"))


def embed(texts, batch=8, timeout=1800):
    import urllib.request
    out = []
    for i in range(0, len(texts), batch):
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": EMBED_MODEL, "input": texts[i:i + batch]}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out += json.load(r)["embeddings"]
    return out


def cos(a, b):
    s = sum(x * y for x, y in zip(a, b))
    return s / ((sum(x * x for x in a) ** .5) * (sum(x * x for x in b) ** .5) + 1e-9)


# 索引文本的三个变体：与 multihop-probe 的历史约定一致，**别改名**（旧缓存文件按这个命名）
KINDS = {
    "ctx+body": "_corpus_vecs.json",
    "body": "_corpus_vecs-noctx.json",
    "ctx": "_corpus_vecs-ctxtonly.json",
}


class Corpus:
    """一次加载，全程复用。所有尺子都从这里拿语料。"""

    def __init__(self):
        rows = psql_rows(
            "SELECT c.id, coalesce(c.ctx,''), c.content, c.seq, d.name "
            "FROM chunks c JOIN documents d ON d.id=c.doc_id ORDER BY c.id")
        self.ids = [r[0] for r in rows]
        self.ctx = [r[1] for r in rows]
        self.body = [r[2] for r in rows]
        self.seq = [r[3] for r in rows]
        self.doc = [r[4] for r in rows]
        self.n = len(rows)
        self.text = [(c + "\n" + b) if c else b
                     for c, b in zip(self.ctx, self.body)]
        self._nbody = [norm(b) for b in self.body]
        # **语料戳**：块数 + 每块的 (doc, seq, 正文哈希)。正文一模一样但 id 重排也算不同 ——
        # 因为一切按位置对齐的缓存都会因此错位。
        h = hashlib.sha1()
        h.update(str(self.n).encode())
        for d, s, b in zip(self.doc, self.seq, self.body):
            h.update(b"\x00")
            h.update(f"{d}\t{s}\t{hashlib.sha1(b.encode('utf-8')).hexdigest()[:12]}".encode("utf-8"))
        self.stamp = h.hexdigest()[:12]
        self._vecs = {}

    # ── 定位 ────────────────────────────────────────────────────────────
    # 摘录长度：40 字。**别改** —— 旧尺子（multihop-probe）用的是 40 字包含匹配，
    # 换了长度 = 换了靶子组 = 数字与历史不可比（2026-09-20 实测：24 字前缀比 40 字包含
    # 严得多，组大小 61 vs 109，全中率直接差一档）。
    EXCERPT = 40

    def find(self, excerpt, doc=None):
        """**按内容**定位块：**任意位置包含**（不是前缀）。

        为什么是包含而不是前缀：同一段文字可能落在一块的开头，也可能落在块中间
        （块把上一段并进来时就变中间了）—— 只要这块**含有**那段内容，它就是靶子。
        另加反向包含：极短的块整个落在这段文字里时也算（那是重复块）。
        """
        key = norm(excerpt)
        if not key:
            return []
        out = []
        for i in range(self.n):
            if doc is not None and self.doc[i] != doc:
                continue
            b = self._nbody[i]
            if key in b or (b and b in key):
                out.append(i)
        return out

    def doc_index(self):
        m = {}
        for i, d in enumerate(self.doc):
            m.setdefault(d, []).append(i)
        return m

    def head(self, i, n=None):
        """第 i 块的**正文**头部（反转义 + 去空白）—— 存靶子摘录用它，保证与 `norm` 一致。"""
        return self._nbody[i][:n or self.EXCERPT]

    # ── 向量 ────────────────────────────────────────────────────────────
    def vecs(self, kind="ctx+body", rebuild=True):
        """取向量：**先验语料戳**。对不上就重建（唯一会写缓存的地方）。"""
        if kind in self._vecs:
            return self._vecs[kind]
        texts = {"ctx+body": self.text, "body": self.body, "ctx": self.ctx}[kind]
        p = os.path.join(TOOLS, KINDS[kind])
        d = None
        if os.path.exists(p):
            try:
                d = json.load(io.open(p, encoding="utf-8"))
            except Exception as e:
                print(f"！{os.path.basename(p)} 读不动（{e}）")
        if d and d.get("stamp") == self.stamp:
            self._vecs[kind] = d["vecs"]
            return d["vecs"]
        if d and d.get("stamp") is None and d.get("ids") == self.ids:
            # 旧格式（只有 ids 没有戳）：这一份可以信，补上戳
            io.open(p, "w", encoding="utf-8").write(json.dumps(
                {"stamp": self.stamp, "ids": self.ids, "vecs": d["vecs"]}))
            print(f"  给 {os.path.basename(p)} 补上语料戳 {self.stamp}")
            self._vecs[kind] = d["vecs"]
            return d["vecs"]
        if not rebuild:
            raise SystemExit(f"{os.path.basename(p)} 与当前语料不符（缓存戳 "
                             f"{(d or {}).get('stamp')} / 现在 {self.stamp}）—— "
                             f"先跑：python tools/ruler.py rebuild")
        print(f"  重建 {os.path.basename(p)}（语料戳 {self.stamp}，{self.n} 块）…", flush=True)
        v = embed(texts)
        io.open(p, "w", encoding="utf-8").write(json.dumps(
            {"stamp": self.stamp, "ids": self.ids, "vecs": v}))
        self._vecs[kind] = v
        return v

    def qvec(self, questions):
        """批量嵌入查询，一次算完复用（尺度小的缓存不做，查询每次都重算，保证新鲜）。"""
        uniq = sorted(set(questions))
        out = {}
        for i in range(0, len(uniq), 8):
            for q, v in zip(uniq[i:i + 8], embed(uniq[i:i + 8])):
                out[q] = v
        return out

    def line(self):
        return f"语料 {self.n} 块 · 戳 {self.stamp}"


_C = None


def load():
    global _C
    if _C is None:
        _C = Corpus()
    return _C
