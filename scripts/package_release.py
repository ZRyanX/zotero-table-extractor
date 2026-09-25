#!/usr/bin/env python3
"""
scripts/package_release.py — Zotero Table Extractor Release 资产自动化打包与上传工具。

功能：
1. 提取指定 Tag 或当前最新 Release 标签的代码与权重；
2. 基于 git archive 机制构建纯净的 .zip 发行包（自动带独立根目录前缀）；
3. 严格安全审计：坚决排除 config.json、.backups、profiles、.git、scratch、__pycache__ 等敏感信息；
4. 确保包含必要核心模型权重 (models/doclayout-yolo-*.onnx) 与源码；
5. 生成 SHA256 校验和文件 (.sha256)；
6. 支持通过 GitHub CLI (gh) 一键将打包资产直传上传至对应 Release。
"""

import os
import sys
import argparse
import subprocess
import hashlib
import zipfile
from pathlib import Path
from typing import Optional, Tuple, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 严禁打包泄漏的黑名单关键词
FORBIDDEN_KEYWORDS = [
    "config.json",
    "config.backup.json",
    ".backups",
    "profiles",
    ".zotero_playwright_profile",
    ".zotero_chrome_debug_profile",
    "scratch",
    ".git/",
    ".env",
    ".DS_Store",
    "__pycache__",
]

# 必须包含的核心资产
MANDATORY_ENTRIES = [
    "scripts/pdf_table_extractor.py",
    "scripts/ocr_client.py",
    "scripts/table_postprocess.py",
    "config.example.json",
    "requirements.txt",
    "setup_paths.py",
    "update.py",
    "models/doclayout-yolo-docstructbench-q8-6c25a56c.onnx",
]


def get_latest_tag() -> str:
    """获取本地或远程最新的 git tag。"""
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=str(PROJECT_ROOT),
            text=True
        ).strip()
        return tag
    except Exception:
        tags = subprocess.check_output(
            ["git", "tag", "-l", "--sort=-v:refname"],
            cwd=str(PROJECT_ROOT),
            text=True
        ).strip().splitlines()
        if tags:
            return tags[0].strip()
        return "v1.4.0"


def compute_sha256(filepath: str) -> str:
    """计算文件的 SHA256 散列值。"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def audit_zip_contents(zip_path: str, prefix: str) -> Tuple[bool, List[str]]:
    """对生成的 zip 包进行严格安全审计。"""
    violations = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()

        # 1. 检查黑名单
        for name in names:
            rel_name = name[len(prefix):] if name.startswith(prefix) else name
            for kw in FORBIDDEN_KEYWORDS:
                if kw in rel_name.split("/"):
                    violations.append(f"[安全违规] 发现敏感文件/目录: {name}")
                elif kw == "config.json" and rel_name.endswith("config.json") and "example" not in rel_name:
                    violations.append(f"[安全违规] 发现敏感私有配置文件: {name}")

        # 2. 检查必须项
        for mand in MANDATORY_ENTRIES:
            expected = f"{prefix}{mand}"
            if expected not in names and mand not in names:
                violations.append(f"[完整性缺失] 缺少核心必要文件: {mand}")

    return len(violations) == 0, violations


def package_release(tag: str, output_dir: str, upload: bool = False) -> Tuple[str, str]:
    """执行打包与上传主流程。"""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pkg_name = f"zotero-table-extractor-{tag}.zip"
    zip_path = out_dir / pkg_name
    sha_path = out_dir / f"{pkg_name}.sha256"
    prefix = f"zotero-table-extractor-{tag}/"

    print("=" * 70)
    print(f"📦 正在打包 Release 发行代码包: {tag}")
    print(f"📁 目标文件: {zip_path}")
    print("=" * 70)

    # 1. 使用 git archive 构建纯净发行包
    cmd_archive = [
        "git", "archive",
        "--format=zip",
        f"--prefix={prefix}",
        tag,
        "-o", str(zip_path)
    ]
    subprocess.check_call(cmd_archive, cwd=str(PROJECT_ROOT))
    size_bytes = os.path.getsize(zip_path)
    size_mb = size_bytes / (1024 * 1024)
    print(f"[✓] 打包完成！大小: {size_mb:.2f} MB ({size_bytes} 字节)")

    # 2. 严格安全与完整性审计
    passed, violations = audit_zip_contents(str(zip_path), prefix)
    if not passed:
        print("\n❌ 发现安全或完整性违规项，打包流程强制终止:")
        for v in violations:
            print(f"  • {v}")
        if zip_path.exists():
            zip_path.unlink()
        sys.exit(1)
    print("[✓] 安全与完整性审计通过：无任何敏感配置文件泄露，已完整包含核心 ONNX 权重。")

    # 3. 计算 SHA256 校验和
    sha256_val = compute_sha256(str(zip_path))
    with open(sha_path, "w", encoding="utf-8") as f:
        f.write(f"{sha256_val}  {pkg_name}\n")
    print(f"[✓] SHA256 校验和: {sha256_val}")
    print(f"[✓] 校验和文件已生成: {sha_path}")

    # 4. 上传至 GitHub Release (若指定或可用)
    if upload:
        print("\n🚀 正在通过 GitHub CLI (gh) 上传资产至 Release...")
        cmd_upload = [
            "gh", "release", "upload", tag,
            str(zip_path), str(sha_path),
            "--clobber"
        ]
        try:
            subprocess.check_call(cmd_upload, cwd=str(PROJECT_ROOT))
            print(f"🎉 资产上传成功！已附加至 Release: {tag}")
        except Exception as e:
            print(f"⚠️ 上传失败 ({e})，请手动执行: gh release upload {tag} {zip_path} {sha_path}")

    return str(zip_path), str(sha_path)


def main():
    parser = argparse.ArgumentParser(description="Zotero Table Extractor Release 资产打包与上传工具")
    parser.add_argument("--tag", type=str, default="", help="指定 Release Tag (默认提取最新 tag)")
    parser.add_argument("--output-dir", type=str, default=str(PROJECT_ROOT / "dist"), help="包输出目录 (默认 dist/)")
    parser.add_argument("--upload", action="store_true", help="打包完成后自动通过 gh 上传至 GitHub Release")
    args = parser.parse_args()

    tag = args.tag.strip() or get_latest_tag()
    package_release(tag, args.output_dir, upload=args.upload)


if __name__ == "__main__":
    main()
