#!/usr/bin/env python3
"""
scripts/setup_paths.py — setup_paths.py 的入口包装，支持直接在 scripts 目录下调用。
"""
import os
import sys

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from setup_paths import main

if __name__ == "__main__":
    main()
