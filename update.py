#!/usr/bin/env python3
"""
update.py — Zotero Table Extractor 技能在线更新主入口。

功能：
- 自动检测远端代码与模型更新（支持 Git 与 Release 归档双模式）；
- 在线更新前自动对现有本地环境与 config.json 建立时间戳快照；
- 智能合并 config.example.json 中新增的字段，100% 绝对保护用户原有的 API 密钥、
  自定义存储路径、Zotero 数据库位置与浏览器 Profile 等个人私有信息；
- 详尽打印敏感凭据防护核验单，提供一键式 --rollback 回滚保障。

用法：
  python update.py                 # 一键在线安全更新并平滑迁移配置
  python update.py --check         # 仅检查远端更新与本地缺失配置项
  python update.py --dry-run       # 演练模式模拟更新
  python update.py --rollback      # 回滚至最近一次备份的配置状态
  python update.py --list-backups  # 查看历史配置快照列表
  python update.py --merge-only    # 仅安全同步最新模板参数至 config.json
"""

import os
import sys

# 确保项目根目录与 scripts/ 子目录在 sys.path 中
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from updater import main

if __name__ == "__main__":
    main()
