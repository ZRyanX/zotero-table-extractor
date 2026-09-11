#!/usr/bin/env python3
"""
online_utils.py — 兼容门面（facade）

实现已拆分为 online/ 包：
  online/doi_resolver.py            DOI 解析与知网 URL 定位
  online/strategies.py              各渠道抓取策略 + HTML/Markdown 表格解析
  online/graph.py                   提取图（DAG）编排与各渠道节点实现
  online/general_html_extractor.py  出版社网页 Playwright 表格提取
  online/cnki_html_extractor.py     知网 HTML 阅读器表格提取

本模块重新导出全部原有公共名称，原有调用方式不变：
  import online_utils
  online_utils.extract_tables_online(...)
"""

import os
import sys

# compat/ 位于 scripts/ 下一层，需先将 scripts/ 加入 sys.path
scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if scripts_dir not in sys.path:
    sys.path.append(scripts_dir)

from online import *  # noqa: F401,F403
