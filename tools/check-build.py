# -*- coding: utf-8 -*-
"""**构建纪律的第三步**：在**产物**里核对"只有新代码才有的串"。

## 为什么必须有这一步

本项目吃过**三次**"构建成功而产物没更新"，三次的症状都是**数字看着正常**：

  1. `mvn … | tail -6; echo BUILD=$?` —— **`$?` 是管道最后一个命令（`tail`）的退出码**，
     mvn 失败也打印 0；
  2. **增量编译跳过模块** —— 根构建里那个模块只有 `Building jar` 没有 `Compiling`，
     jar 用**旧的 `target/classes`** 打出来（"构建成功"，内容没变）；
  3. **应用在跑时 maven 换不掉 jar** —— `Unable to rename … .jar .original`，
     而增量编译又让它看起来没报错。

⇒ **"构建成功"必须有独立判据，而且判据必须是产物本身**。
`$?` / `BUILD SUCCESS` / 文件时间戳都会骗你（时间戳更新而内容不必）；
**只有"在产物里找一句只有新代码才会出现的字符串"骗不了**。

## 用法

    python tools/check-build.py "相邻块补全" listNeighbors neighbor
    python tools/check-build.py --jar-only "材料地板"

不给串时只报**产物的时间戳**（并明确说"这不能证明什么"）—— 免得被当成"检查过了"。

⚠️ CJK 串要按 **UTF-8 逐字节**找（class 常量池是 modified UTF-8）：
按 latin-1 解码找过一次，白找。所以这里一律 `decode("utf-8", errors="replace")`。
⚠️ **扫描面为 0 要报错**：`tools/eval/_runs/` 那次"三张表全 0"的教训 ——
"没找到"与"没采到"长得一模一样。
"""
import io
import os
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAR = os.path.join(ROOT, "rag-kb-web", "target", "rag-kb.jar")
MODULES = ["rag-kb-common", "rag-kb-domain", "rag-kb-dao", "rag-kb-security",
           "rag-kb-provider", "rag-kb-service", "rag-kb-web"]


def class_files():
    for m in MODULES:
        base = os.path.join(ROOT, m, "target", "classes")
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                # ⚠️ **只管 class 会漏掉资源** —— `application.yml` 也打进产物，
                # 而"jar 里是旧 yml"与"jar 里是旧 class"一样致命（2026-09-29 实测：
                # 新加的 `mat-floor-mode` 只在 yml 里，check-build 报"缺失"，
                # 差点被当成构建没生效）。资源只认这几类文本文件。
                if f.endswith(".class") or f.endswith((".yml", ".yaml", ".xml", ".properties")):
                    yield os.path.join(dirpath, f)


def jar_entries():
    """把 fat jar 里**每个嵌套模块 jar 的每个 class** 也当条目吐出来。

    这是**最终产物** —— 应用真正加载的就是它。`target/classes` 对了不等于 jar 对了
    （第 2 条那个坑正是"classes 是新的、jar 是旧的"）。
    """
    if not os.path.exists(JAR):
        return
    with zipfile.ZipFile(JAR) as z:
        for n in z.namelist():
            if not n.endswith(".jar"):
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(z.read(n))) as inner:
                    for m in inner.namelist():
                        if m.endswith((".class", ".yml", ".yaml", ".xml", ".properties")):
                            yield f"{JAR}!{n}!{m}", inner.read(m)
            except zipfile.BadZipFile:
                continue


def scan(name, data, needles):
    txt = data.decode("utf-8", errors="replace")
    return [n for n in needles if n in txt]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    jar_only = "--jar-only" in sys.argv

    hits = {n: [] for n in args}
    n_files = 0
    if not jar_only:
        for p in class_files():
            n_files += 1
            data = open(p, "rb").read()
            for n in scan(p, data, args):
                hits[n].append(os.path.relpath(p, ROOT))
    n_jar = 0
    for name, data in jar_entries() or []:
        n_jar += 1
        for n in scan(name, data, args):
            hits[n].append(name.replace(ROOT + os.sep, ""))

    print(f"扫了 target/classes {n_files} 个 class/资源、fat jar 内 {n_jar} 个")
    if n_files == 0 and n_jar == 0:
        raise SystemExit("！一个产物都没扫到 —— 先构建（`mvn -DskipTests package`）")

    if not args:
        print("\n（没给要核对的串 ⇒ **这只报了产物存在，什么也没证明**。）")
        print("用法：python tools/check-build.py \"只有新代码才有的串\" …")
        for p in (JAR,):
            if os.path.exists(p):
                import time
                print(f"  {os.path.relpath(p, ROOT)}  {time.ctime(os.path.getmtime(p))}")
        return 0

    bad = 0
    for n in args:
        where = hits[n]
        bad += 0 if where else 1
        head = f"  {'命中' if where else '缺失'}  {n}"
        print(head + (f"（{len(where)} 个文件，例：{where[0]}）" if where else ""))
    print("\n" + ("产物里确实是新代码" if not bad else f"有 {bad} 处没找到 —— 产物是旧的"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
