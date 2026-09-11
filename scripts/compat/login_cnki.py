#!/usr/bin/env python3
"""
[兼容薄壳] CNKI Cookie 配置向导

实现已合并至 login.py（子命令 cnki），本文件仅为保持原有调用方式不变：
  python3 scripts/compat/login_cnki.py   等价于   python3 scripts/login.py cnki
"""

import sys
import os

# Resolve scripts path to import login (compat/ 位于 scripts/ 下一层)
scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if scripts_dir not in sys.path:
    sys.path.append(scripts_dir)

try:
    import login
except ImportError:
    print("Error: Could not import login.py. Please verify scripts directory structure.", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    sys.exit(0 if login.cnki_main() else 1)
