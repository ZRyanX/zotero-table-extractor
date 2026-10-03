#!/usr/bin/env python3
"""
scripts/setup_paths.py — setup_paths.py 的入口包装，支持直接在 scripts 目录下调用。
"""
import os
import sys

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir in sys.path:
    sys.path.remove(root_dir)
sys.path.insert(0, root_dir)

import importlib.util
_spec = importlib.util.spec_from_file_location("setup_paths_root_entry", os.path.join(root_dir, "setup_paths.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
main = _mod.main

if __name__ == "__main__":
    main()
