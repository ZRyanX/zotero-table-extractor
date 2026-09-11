#!/usr/bin/env python3
"""
[兼容薄壳] 出版社登录与 Cookie 配置向导

实现已合并至 login.py（子命令 publishers），本文件仅为保持原有调用方式不变：
  python3 scripts/compat/login_publishers.py   等价于   python3 scripts/login.py publishers

同时继续导出原有的模块级名称（供 scratch 脚本等直接 import 使用）。
"""

import sys
import os

# compat/ 位于 scripts/ 下一层
scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if scripts_dir not in sys.path:
    sys.path.append(scripts_dir)

from login import (  # noqa: F401
    publishers_main as main,
    launch_chrome_debug_mode,
    load_skill_config,
    open_tabs_via_cdp,
    get_all_cookies_via_cdp,
    get_crawl4ai_cookies_path,
    get_cnki_cookies_path,
    PUBLISHERS,
)

if __name__ == "__main__":
    main()
