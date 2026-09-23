# -*- coding: utf-8 -*-
"""**已并入 `tools/think-structure.py`** —— 这个文件只是一层薄壳。

为什么保留文件名而不是删掉：**台账里引用过它**（"一个数字要说明白用哪把尺子量的"）。
删掉会让那些引用指向一个不存在的文件，事后回溯就断了。

新的用法（同一个工具，两个输入口）：
    python tools/think-structure.py --run tools/_runs/<探针目录>   # 有帧表 ⇒ 能报秒
    python tools/think-structure.py --eval <基准> <tag>            # 判分落盘 ⇒ 报字数
"""
import importlib.util
import os
import sys

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    sys.argv = [sys.argv[0], "--run"] + [a for a in sys.argv[1:] if not a.startswith("-")] \
        + [a for a in sys.argv[1:] if a.startswith("--")]
    spec = importlib.util.spec_from_file_location(
        "think_structure", os.path.join(here, "think-structure.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.main()
