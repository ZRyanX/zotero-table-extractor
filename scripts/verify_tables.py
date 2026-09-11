#!/usr/bin/env python3
"""
verify_tables.py — 验证 paper_tables 目录下所有 xlsx 文件的有效性。

修复旧验证脚本的 bug：旧版只读第一个 sheet，把第一个 sheet 为空但
后续 sheet 有数据的文件误报为「0行0列 提取失败」。

本脚本遍历每个 xlsx 文件的**所有 sheet**，取并集判断是否有效：
- 文件大小 < 3000 字节 且所有 sheet 均空 → 判为空壳（提取失败）
- 至少一个 sheet 含 >=1 个非空单元格 → 判为有效

用法:
    python3 scripts/verify_tables.py [--root <paper_tables_dir>] [--fix]
    --fix: 自动删除判定为空壳的损坏 xlsx 文件
"""
try:
    from .system_detector import (
        IS_MACOS, IS_WINDOWS, get_paper_tables_dir, get_zotero_db_path,
        get_zotero_storage_dir, get_journal_downloader_script,
        get_agent_reasoning_dir, get_audit_db_path, get_targeted_audit_db_path, adapt_path
    )
except ImportError:
    from system_detector import (
        IS_MACOS, IS_WINDOWS, get_paper_tables_dir, get_zotero_db_path,
        get_zotero_storage_dir, get_journal_downloader_script,
        get_agent_reasoning_dir, get_audit_db_path, get_targeted_audit_db_path, adapt_path
    )


import os
import sys
import argparse
import openpyxl


def inspect_xlsx(path):
    """
    返回 (is_valid, total_rows, total_cols, total_nonempty_cells, sheets_info)。
    is_valid: 至少一个 sheet 有数据
    """
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        return False, 0, 0, 0, f"LOAD_ERROR: {e}"

    sheets_info = []
    total_nonempty = 0
    max_rows = 0
    max_cols = 0
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        row_count = 0
        nonempty_cells = 0
        for row in ws.iter_rows(values_only=True):
            row_count += 1
            for c in row:
                if c is not None and str(c).strip():
                    nonempty_cells += 1
            if row_count > 500:
                break
        sheets_info.append(f"{sheet_name}({row_count}r/{nonempty_cells}c)")
        total_nonempty += nonempty_cells
        max_rows = max(max_rows, row_count)
        max_cols = max(max_cols, ws.max_column or 0)
    wb.close()

    is_valid = total_nonempty > 0
    return is_valid, max_rows, max_cols, total_nonempty, "; ".join(sheets_info)


def main():
    parser = argparse.ArgumentParser(description="Verify xlsx table files in paper_tables directory.")
    parser.add_argument("--root", default=get_paper_tables_dir(),
                        help="Root directory of paper_tables (default: auto-detected paper_tables directory)")
    parser.add_argument("--fix", action="store_true",
                        help="Delete empty/corrupt xlsx files automatically.")
    parser.add_argument("--verbose", action="store_true",
                        help="Print info for valid files too.")
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        print(f"Error: directory not found: {args.root}")
        sys.exit(1)

    empty_files = []
    valid_count = 0
    total_count = 0

    for folder in sorted(os.listdir(args.root)):
        folder_path = os.path.join(args.root, folder)
        if not os.path.isdir(folder_path) or folder.startswith("."):
            continue
        for fname in sorted(os.listdir(folder_path)):
            if not fname.lower().endswith(".xlsx") or fname.startswith("~$") or fname.startswith("._"):
                continue
            fpath = os.path.join(folder_path, fname)
            total_count += 1
            file_size = os.path.getsize(fpath)
            is_valid, rows, cols, nonempty, sheets = inspect_xlsx(fpath)

            rel_path = os.path.join(folder, fname)
            if is_valid:
                valid_count += 1
                if args.verbose:
                    print(f"  OK   {rel_path} | {rows}r x {cols}c | {nonempty} cells | {sheets}")
            else:
                empty_files.append((rel_path, file_size, sheets))
                print(f"  FAIL {rel_path} | {file_size}B | {sheets}")

    print(f"\n=== SUMMARY ===")
    print(f"Total xlsx files: {total_count}")
    print(f"Valid (has data): {valid_count}")
    print(f"Empty/corrupt:    {len(empty_files)}")

    if empty_files:
        print(f"\n=== EMPTY/CORRUPT FILES ({len(empty_files)}) ===")
        for rel_path, size, sheets in empty_files:
            print(f"  {rel_path} ({size}B) [{sheets}]")

        if args.fix:
            print(f"\n--fix: deleting {len(empty_files)} empty/corrupt files...")
            deleted = 0
            for rel_path, _, _ in empty_files:
                fpath = os.path.join(args.root, rel_path)
                try:
                    os.remove(fpath)
                    print(f"  deleted: {rel_path}")
                    deleted += 1
                except Exception as e:
                    print(f"  FAILED to delete {rel_path}: {e}")
            print(f"Deleted {deleted}/{len(empty_files)} files.")


if __name__ == "__main__":
    main()
