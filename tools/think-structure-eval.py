# -*- coding: utf-8 -*-
"""**已并入 `tools/think-structure.py`** —— 这个文件只是一层薄壳（理由同上）。

新的用法：
    python tools/think-structure.py --eval <基准> <tag>     # 例如 answer-quality __sw3
    python tools/think-structure.py --eval <基准> <tag> --json
"""
import importlib.util
import os
import sys

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    a = [x for x in sys.argv[1:] if not x.startswith("-")]
    sys.argv = [sys.argv[0], "--eval"] + a[:2] + [x for x in sys.argv[1:] if x.startswith("-")]
    spec = importlib.util.spec_from_file_location(
        "think_structure", os.path.join(here, "think-structure.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.main()
