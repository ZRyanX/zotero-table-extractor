#!/usr/bin/env python3
"""
zotero_sync.py — PyZotero 与 Zotero-Better-Notes 生态联动模块。

功能：
1. 本地数据库/路径感知：通过 zotero.sqlite 或附件存储目录自动溯源 PDF 对应的 Zotero 文献条目 Key (Parent Item Key)；
2. 附件回填联动：将提取生成的 .xlsx 表格文件作为子附件挂载至对应的 Zotero 顶层条目；
3. Better-Notes 联动：自动构建符合 Zotero-Better-Notes 规范的富文本 HTML 表格概览笔记卡片，
   内嵌表号、标题、行列数、提取引擎元数据与精美表格预览；
4. 离线/免 Key 兼容：除 PyZotero 云端 API 外，同步生成本地 _better_notes.html 预览快照，
   方便用户离线直接在 Zotero Better Notes 或浏览器中拖拽引入；
5. 可选开关保护：默认全局关闭 (ZOTERO_SYNC_ENABLED = false)，仅在用户明确配置或 CLI 参数触发时执行，
   且全程具备防崩保护，任何同步网络或配置异常绝不阻断提取流程。
"""

import os
import re
import sys
import uuid
import shutil
import sqlite3
import logging
import html as _html
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple, Union

logger = logging.getLogger(__name__)

try:
    import pandas as pd
except ImportError:
    pd = None

# 尝试引入 pyzotero
try:
    from pyzotero import zotero
    PYZOTERO_AVAILABLE = True
except ImportError:
    zotero = None
    PYZOTERO_AVAILABLE = False

try:
    from .common import load_config, copy_sqlite_snapshot, cleanup_sqlite_snapshot
    from .system_detector import get_zotero_db_path, get_zotero_storage_dir
except ImportError:
    try:
        from common import load_config, copy_sqlite_snapshot, cleanup_sqlite_snapshot
        from system_detector import get_zotero_db_path, get_zotero_storage_dir
    except ImportError:
        load_config = lambda: {}
        copy_sqlite_snapshot = None
        cleanup_sqlite_snapshot = None
        get_zotero_db_path = lambda: os.path.expanduser("~/Zotero/zotero.sqlite")
        get_zotero_storage_dir = lambda: os.path.expanduser("~/Zotero/storage")


def resolve_zotero_item_key(
    pdf_path: str,
    db_path: Optional[str] = None
) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    """
    通过 PDF 物理路径和本地 zotero.sqlite 逆向溯源 Zotero 条目 Key。

    返回:
      (parent_item_key, attachment_item_key, metadata_dict)
      - parent_item_key: 顶层学术条目 Key (如 "ABCD2345")
      - attachment_item_key: PDF 附件自身 Key (如 "EFGH6789")
      - metadata_dict: 包含 title, DOI, url 等信息的字典
    """
    meta: Dict[str, Any] = {}
    if not pdf_path:
        return None, None, meta

    cfg = load_config() if callable(load_config) else {}
    if not db_path:
        db_path = cfg.get("ZOTERO_DB_PATH") or get_zotero_db_path()

    if not db_path or not os.path.exists(db_path):
        logger.debug(f"[ZoteroSync] 未找到 zotero.sqlite 数据库: {db_path}")
        return None, None, meta

    # 1. 优先从存储路径中解析附件 key (例如 storage/8XYZ1234/paper.pdf)
    norm_path = os.path.abspath(pdf_path).replace("\\", "/")
    parts = norm_path.split("/")
    attachment_key_cand = None
    if len(parts) >= 2:
        cand = parts[-2]
        if len(cand) == 8 and cand.isalnum():
            attachment_key_cand = cand.upper()

    # 安全使用 SQLite 临时副本（支持 WAL/SHM 快照），防止 Zotero 运行时锁库或丢失未刷盘写入
    import tempfile
    temp_dir = tempfile.gettempdir()
    temp_db = os.path.join(temp_dir, f"zotero_sync_{uuid.uuid4().hex[:8]}.sqlite")
    copied = False
    try:
        if callable(copy_sqlite_snapshot):
            copy_sqlite_snapshot(db_path, temp_db)
        else:
            shutil.copy2(db_path, temp_db)
            for _sfx in ("-wal", "-shm"):
                if os.path.exists(db_path + _sfx):
                    shutil.copy2(db_path + _sfx, temp_db + _sfx)
        target_db = temp_db
        copied = True
    except Exception:
        target_db = db_path

    parent_key = None
    attachment_key = attachment_key_cand

    try:
        conn = sqlite3.connect(target_db)
        cursor = conn.cursor()

        parent_id = None
        matched_attachment = False
        # 尝试按附件 key 定位
        if attachment_key_cand:
            cursor.execute("""
                SELECT ia.parentItemID, i.key
                FROM itemAttachments ia
                JOIN items i ON ia.itemID = i.itemID
                WHERE UPPER(i.key) = ?
            """, (attachment_key_cand,))
            row = cursor.fetchone()
            if row:
                matched_attachment = True
                parent_id = row[0]
                attachment_key = row[1]

        # 仅在未通过目录 Key 匹配到附件条目时，尝试按文件名模糊匹配
        if not matched_attachment:
            filename = os.path.basename(pdf_path)
            cursor.execute("""
                SELECT ia.parentItemID, i.key
                FROM itemAttachments ia
                JOIN items i ON ia.itemID = i.itemID
                WHERE ia.path LIKE ?
            """, (f"%{filename}",))
            row = cursor.fetchone()
            if row:
                parent_id = row[0]
                attachment_key = row[1]

        # 解析 parent item key 和基础元数据
        if parent_id:
            cursor.execute("SELECT key FROM items WHERE itemID = ?", (parent_id,))
            p_row = cursor.fetchone()
            if p_row:
                parent_key = p_row[0]

            cursor.execute("""
                SELECT f.fieldName, idv.value
                FROM itemData id
                JOIN fields f ON id.fieldID = f.fieldID
                JOIN itemDataValues idv ON id.valueID = idv.valueID
                WHERE id.itemID = ? AND f.fieldName IN ('title', 'DOI', 'url', 'date')
            """, (parent_id,))
            for f_name, f_val in cursor.fetchall():
                meta[f_name] = f_val

        conn.close()
    except Exception as e:
        logger.debug(f"[ZoteroSync] 查询 zotero.sqlite 出错: {e}")
    finally:
        if copied:
            if callable(cleanup_sqlite_snapshot):
                cleanup_sqlite_snapshot(temp_db)
            elif os.path.exists(temp_db):
                for _sfx in ("", "-wal", "-shm"):
                    if os.path.exists(temp_db + _sfx):
                        try:
                            os.remove(temp_db + _sfx)
                        except Exception:
                            pass

    return parent_key, attachment_key, meta


def format_better_notes_html(
    dfs: List[Any],
    pdf_path: str,
    excel_path: Union[str, List[str]],
    metadata: Optional[Dict[str, Any]] = None,
    max_preview_rows: int = 15,
) -> str:
    """
    根据 Zotero-Better-Notes (https://github.com/windingwind/zotero-better-notes)
    规范生成精美结构化的富文本 HTML 预览卡片。
    """
    meta = metadata or {}
    paper_title = meta.get("title") or os.path.splitext(os.path.basename(pdf_path))[0]
    doi = str(meta.get("DOI", "") or "")
    excel_paths = [excel_path] if isinstance(excel_path, str) else [p for p in (excel_path or []) if p]
    excel_name = ", ".join(os.path.basename(p) for p in excel_paths) if excel_paths else ""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    safe_paper_title = _html.escape(str(paper_title), quote=True)
    safe_doi = _html.escape(doi, quote=True)
    safe_excel_name = _html.escape(excel_name, quote=True)
    doi_href = _html.escape(f"https://doi.org/{doi}", quote=True)

    # 构建 HTML 表头卡片（全局唯一 <style> 定义）
    cards_html = []
    cards_html.append(f"""
<div class="better-notes-table-summary" style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.5; color: #24292e;">
  <style>
    .better-notes-table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }}
    .better-notes-table th {{ background-color: #f1f3f5; color: #333; font-weight: 600; padding: 6px 10px; border: 1px solid #d0d7de; text-align: left; }}
    .better-notes-table td {{ padding: 5px 10px; border: 1px solid #d0d7de; }}
    .better-notes-table tr:nth-child(even) {{ background-color: #fafbfc; }}
  </style>
  <div style="background: linear-gradient(135deg, #f6f8fa 0%, #eef2f7 100%); border-left: 5px solid #0366d6; padding: 14px 18px; border-radius: 6px; margin-bottom: 20px;">
    <h2 style="margin: 0 0 8px 0; font-size: 18px; color: #0366d6;">📊 提取表格概览 (Extracted Tables Preview)</h2>
    <div style="font-size: 13px; color: #586069;">
      <div><strong>📄 来源文献:</strong> {safe_paper_title}</div>
      {f"<div><strong>🔗 DOI:</strong> <a href='{doi_href}'>{safe_doi}</a></div>" if doi else ""}
      <div><strong>📁 导出工作簿:</strong> <code>{safe_excel_name}</code></div>
      <div><strong>🔢 表格总数:</strong> {len(dfs)} 个 | <strong>⏱ 提取时间:</strong> {now_str}</div>
    </div>
  </div>
""")

    for idx, df_item in enumerate(dfs):
        # 统一获取 DataFrame
        df = None
        if hasattr(df_item, "to_dataframe"):
            df = df_item.to_dataframe()
        elif hasattr(df_item, "df") and isinstance(df_item.df, pd.DataFrame):
            df = df_item.df
        elif isinstance(df_item, pd.DataFrame):
            df = df_item
        elif isinstance(df_item, dict) and "df" in df_item:
            df = df_item["df"]

        if df is None or (hasattr(df, "empty") and df.empty):
            continue

        attrs = getattr(df, "attrs", {})
        label = str(attrs.get("label") or f"Table {idx + 1}")
        title = str(attrs.get("table_title") or "")
        page_idx = attrs.get("page_idx", 0)
        page_str = f"Page {page_idx + 1}" if isinstance(page_idx, int) else f"Page {page_idx}"
        extractor = str(attrs.get("vote_extractor") or attrs.get("extractor") or "auto")
        rows_cnt, cols_cnt = df.shape

        safe_label = _html.escape(label, quote=True)
        safe_title = _html.escape(title, quote=True)
        safe_page_str = _html.escape(page_str, quote=True)
        safe_extractor = _html.escape(extractor, quote=True)

        # 截取预览行
        preview_df = df.iloc[:max_preview_rows]
        is_truncated = rows_cnt > max_preview_rows

        # 转换为 HTML 表格
        table_html = preview_df.to_html(
            classes="better-notes-table",
            index=False,
            escape=True,
            border=0
        )

        card_html = f"""
  <div class="better-notes-table-card" style="margin-bottom: 24px; border: 1px solid #e1e4e8; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.04);">
    <div style="background-color: #f6f8fa; padding: 10px 16px; border-bottom: 1px solid #e1e4e8; display: flex; justify-content: space-between; align-items: center;">
      <span style="font-weight: 600; font-size: 15px; color: #24292e;">🏷 {safe_label}{': ' + safe_title if title else ''}</span>
      <span style="font-size: 12px; color: #586069; background: #e1e4e8; padding: 2px 8px; border-radius: 12px;">{safe_page_str} | {rows_cnt}行 × {cols_cnt}列</span>
    </div>
    <div style="padding: 12px 16px; font-size: 12px; color: #586069; background: #ffffff;">
      <span><strong>解析引擎:</strong> {safe_extractor}</span>
    </div>
    <div style="overflow-x: auto; max-height: 420px; padding: 0 16px 12px 16px; background: #ffffff;">
      {table_html}
      {f"<div style='font-size: 11px; color: #6a737d; margin-top: 6px; font-style: italic;'>... 仅展示前 {max_preview_rows} 行，完整数据请查阅附件 {safe_excel_name}</div>" if is_truncated else ""}
    </div>
  </div>
"""
        cards_html.append(card_html)

    cards_html.append("</div>")
    return "\n".join(cards_html)


def sync_extracted_tables_to_zotero(
    pdf_path: str,
    excel_path: Union[str, List[str]],
    dfs: List[Any],
    config: Optional[Dict[str, Any]] = None,
    force_sync: bool = False
) -> bool:
    """
    统一 Zotero & Zotero-Better-Notes 生态联动入口。
    
    规则：
    1. 默认关闭：需 config["ZOTERO_SYNC_ENABLED"] == True 或 force_sync == True 触发；
    2. 生成本地 _better_notes.html 预览文档；
    3. 若配置了 Zotero API (PyZotero)，通过 Web API 将 Excel 挂载为附件，并创建 Better-Notes 概览笔记；
    4. 全程异常捕获，绝不影响主流程。
    """
    if config is None:
        config = load_config() if callable(load_config) else {}

    # 严格校验联动开关，默认关闭 (FALSE)
    sync_enabled = force_sync or bool(config.get("ZOTERO_SYNC_ENABLED", False))
    if not sync_enabled:
        logger.debug("[ZoteroSync] ZOTERO_SYNC_ENABLED 为 False，跳过生态联动")
        return False

    excel_paths = [excel_path] if isinstance(excel_path, str) else [p for p in (excel_path or []) if p]
    valid_excel_paths = [p for p in excel_paths if os.path.exists(p)]
    if not valid_excel_paths:
        logger.debug("[ZoteroSync] 无有效 Excel 文件，跳过联动")
        return False

    if not dfs and pd is not None:
        loaded_dfs = []
        for x_path in valid_excel_paths:
            try:
                excel_sheets = pd.read_excel(x_path, sheet_name=None, dtype=str, keep_default_na=False)
                base_lbl = os.path.splitext(os.path.basename(x_path))[0]
                for sheet_name, sheet_df in excel_sheets.items():
                    if sheet_df is not None and not sheet_df.empty:
                        sheet_df.attrs["label"] = base_lbl if len(excel_sheets) == 1 else str(sheet_name)
                        loaded_dfs.append(sheet_df)
            except Exception as e_rx:
                logger.debug(f"[ZoteroSync] 从 Excel 读取表格列表失败 ({x_path}): {e_rx}")
        dfs = loaded_dfs

    if not dfs:
        logger.debug("[ZoteroSync] 无有效表格数据，跳过联动")
        return False

    target_desc = ", ".join(os.path.basename(p) for p in valid_excel_paths)
    print(f"\n[Zotero-Sync] 正在启动 Zotero 与 Better-Notes 生态联动 (Target: {target_desc})...")

    # 1. 溯源 Zotero Item
    parent_key, attach_key, metadata = resolve_zotero_item_key(pdf_path, config.get("ZOTERO_DB_PATH"))
    if parent_key:
        print(f"[Zotero-Sync] 成功锚定 Zotero 条目: Key=[{parent_key}] (附件 Key=[{attach_key or 'None'}])")
    else:
        print("[Zotero-Sync] 未能在本地 Zotero 库中匹配到对应 Parent Item Key (可能为独立 PDF 或未入库文件)")

    # 2. 生成本地 Better-Notes HTML 预览卡片快照
    note_html = format_better_notes_html(dfs, pdf_path, valid_excel_paths, metadata=metadata)
    base_no_ext = os.path.splitext(valid_excel_paths[0])[0]
    local_note_path = f"{base_no_ext}_better_notes.html"
    try:
        with open(local_note_path, "w", encoding="utf-8") as f:
            f.write(note_html)
        print(f"[Zotero-Sync] 本地 Better-Notes 预览卡片已生成: {local_note_path}")
    except Exception as e_w:
        logger.warning(f"[Zotero-Sync] 写入本地 Better-Notes 预览失败: {e_w}")

    # 3. 若配置了 PyZotero API，执行在线附件挂载与笔记写入
    api_key = config.get("ZOTERO_API_KEY", "").strip()
    user_id = str(config.get("ZOTERO_USER_ID", "")).strip()
    library_type = config.get("ZOTERO_LIBRARY_TYPE", "user").strip()
    attach_xlsx = config.get("ZOTERO_AUTO_ATTACH_XLSX", True)
    better_notes_enabled = config.get("ZOTERO_BETTER_NOTES_ENABLED", True)

    if not PYZOTERO_AVAILABLE:
        print("[Zotero-Sync] pyzotero 库未安装，已仅生成本地 Better-Notes 预览文件")
        return True

    if not api_key or not user_id:
        print("[Zotero-Sync] 未配置 ZOTERO_API_KEY 或 ZOTERO_USER_ID，已仅生成本地 Better-Notes 预览文件")
        return True

    if not parent_key:
        print("[Zotero-Sync] 未获得 Parent Item Key，跳过云端附件挂载与笔记写入")
        return True

    try:
        zot = zotero.Zotero(user_id, library_type, api_key)

        existing_children = []
        try:
            if hasattr(zot, "children"):
                existing_children = zot.children(parent_key) or []
        except Exception:
            existing_children = []

        existing_attachment_names = set()
        existing_table_note = None
        for child in existing_children:
            cdata = child.get("data", {}) if isinstance(child, dict) else {}
            itype = cdata.get("itemType")
            if itype == "attachment":
                fname = cdata.get("filename") or cdata.get("title") or ""
                if fname:
                    existing_attachment_names.add(os.path.basename(fname))
            elif itype == "note":
                tags = {t.get("tag") for t in cdata.get("tags", []) if isinstance(t, dict)}
                if {"table-extractor", "better-notes"}.issubset(tags) or "better-notes-table-summary" in cdata.get("note", ""):
                    existing_table_note = child

        # 3.1 挂载 Excel 文件为子附件（去重，避免重复运行多次挂同一附件）
        if attach_xlsx:
            try:
                to_attach = [p for p in valid_excel_paths if os.path.basename(p) not in existing_attachment_names]
                if to_attach:
                    print(f"[Zotero-Sync] 正在挂载 {len(to_attach)} 个 Excel 表格附件至条目 {parent_key}...")
                    zot.attachment_simple(to_attach, parent_key)
                    print(f"[Zotero-Sync] ✓ Excel 表格已成功作为附件挂载至 Zotero 条目 [{parent_key}]！")
                else:
                    print(f"[Zotero-Sync] Excel 附件已存在于条目 [{parent_key}]，跳过重复上传")
            except Exception as e_att:
                print(f"[Zotero-Sync Warning] 挂载 Excel 附件失败: {e_att}")

        # 3.2 创建或更新 Better-Notes 预览笔记条目（避免重复写入多条笔记）
        if better_notes_enabled:
            try:
                if existing_table_note and hasattr(zot, "update_item") and "data" in existing_table_note:
                    print(f"[Zotero-Sync] 正在更新条目 {parent_key} 既有 Better-Notes 表格概览笔记...")
                    existing_table_note["data"]["note"] = note_html
                    zot.update_item(existing_table_note)
                    print(f"[Zotero-Sync] ✓ Better-Notes 概览笔记已更新 (Key: {existing_table_note.get('key')})！")
                else:
                    print(f"[Zotero-Sync] 正在为条目 {parent_key} 创建 Better-Notes 表格概览笔记...")
                    note_payload = {
                        "itemType": "note",
                        "parentItem": parent_key,
                        "note": note_html,
                        "tags": [
                            {"tag": "table-extractor"},
                            {"tag": "better-notes"}
                        ]
                    }
                    res = zot.create_items([note_payload])
                    if res and res.get("successful"):
                        print(f"[Zotero-Sync] ✓ Better-Notes 概览笔记已成功写入 Zotero (Key: {list(res['successful'].keys())})！")
                    else:
                        print(f"[Zotero-Sync] 创建笔记完成: {res}")
            except Exception as e_note:
                print(f"[Zotero-Sync Warning] 创建 Better-Notes 笔记失败: {e_note}")

        return True
    except Exception as e_all:
        print(f"[Zotero-Sync Warning] PyZotero 同步执行异常: {e_all}")
        return False
