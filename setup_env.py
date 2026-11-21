#!/usr/bin/env python3
"""
setup_env.py — Cross-platform one-command environment & dependency installer/checker
for Zotero Table Extractor (Windows / macOS / Linux).

Usage:
  python setup_env.py                  # Install core + online + local PDF engines, Playwright Chromium & ONNX models
  python setup_env.py --all            # Also install heavy optional engines (docling, camelot-py[cv])
  python setup_env.py --check          # Read-only readiness audit (imports, Chromium, models, config keys)
  python setup_env.py --check --json   # Machine-readable JSON readiness report
  python setup_env.py --system-deps    # Also attempt OS package install (macOS Homebrew ghostscript/tesseract)
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

SYSTEM = platform.system()
IS_WINDOWS = SYSTEM == "Windows"
IS_MACOS = SYSTEM == "Darwin"
IS_LINUX = SYSTEM == "Linux"

if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CORE_PACKAGES = [
    "pymupdf>=1.23.0",
    "pandas>=2.0.0",
    "openpyxl>=3.1.0",
    "requests>=2.28.0",
    "pillow>=10.0.0",
    "beautifulsoup4>=4.12.0",
    "lxml>=4.9.0",
    "numpy>=1.24.0",
    "onnxruntime>=1.16.0",
    "playwright>=1.40.0",
    "curl_cffi>=0.6.0",
    "nest_asyncio>=1.5.0",
    "websockets>=11.0",
]

PDF_ENGINE_PACKAGES = [
    "pdfplumber>=0.10.0",
    "img2table>=2.0.0",
    "gmft>=0.4.0",
    "pymupdf4llm>=0.0.17",
    "rapid-table>=0.3.0",
    "rapidocr>=1.4.0",
]

ONLINE_EXT_PACKAGES = [
    "scrapling>=0.2.0",
    "crawl4ai>=0.3.0",
    "pyzotero>=1.5.0",
]

BEST_EFFORT_OPTIONAL = [
    "pdf-inspector",
]

HEAVY_OPTIONAL_PACKAGES = [
    "docling>=2.0.0",
    "camelot-py[cv]>=0.11.0",
]

# Preload onnxruntime before torch/gmft in check_readiness to avoid Windows DLL init 0xC0000005
CHECK_MODULES: List[Tuple[str, str, str]] = [
    ("onnxruntime", "onnxruntime", "core"),
    ("fitz", "pymupdf", "core"),
    ("pandas", "pandas", "core"),
    ("openpyxl", "openpyxl", "core"),
    ("requests", "requests", "core"),
    ("PIL", "pillow", "core"),
    ("bs4", "beautifulsoup4", "core"),
    ("lxml", "lxml", "core"),
    ("playwright", "playwright", "online"),
    ("curl_cffi", "curl_cffi", "online"),
    ("scrapling", "scrapling", "online-ext"),
    ("crawl4ai", "crawl4ai", "online-ext"),
    ("pdfplumber", "pdfplumber", "pdf-engine"),
    ("img2table", "img2table", "pdf-engine"),
    ("pymupdf4llm", "pymupdf4llm", "pdf-engine"),
    ("rapid_table", "rapid-table", "pdf-engine"),
    ("rapidocr", "rapidocr", "pdf-engine"),
    ("gmft", "gmft", "pdf-engine"),
    ("pyzotero", "pyzotero", "ecosystem"),
    ("pdf_inspector", "pdf-inspector", "optional"),
    ("docling", "docling", "heavy-optional"),
    ("camelot", "camelot-py[cv]", "heavy-optional"),
]


def _get_index_url(explicit_mirror: Optional[str] = None) -> str:
    if explicit_mirror:
        return explicit_mirror.strip()
    env_url = os.environ.get("UV_INDEX_URL") or os.environ.get("PIP_INDEX_URL")
    if env_url:
        return env_url.strip()
    cfg_path = ROOT_DIR / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and cfg.get("UV_INDEX_URL"):
                return str(cfg["UV_INDEX_URL"]).strip()
        except Exception:
            pass
    return ""


def _pip_install(
    packages: List[str],
    use_uv: bool = True,
    index_url: str = "",
    upgrade: bool = False,
    dry_run: bool = False,
) -> Tuple[bool, str]:
    uv_bin = shutil.which("uv") if use_uv else None
    if uv_bin:
        cmd = [uv_bin, "pip", "install", "--python", sys.executable]
    else:
        cmd = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    if index_url:
        cmd.extend(["-i", index_url])
    cmd.extend(packages)

    if dry_run:
        return True, "DRY-RUN: " + " ".join(cmd)

    proc = subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def _detect_streamlit_pillow_conflict() -> Optional[str]:
    try:
        st_ver = importlib.metadata.version("streamlit")
        major_minor = tuple(int(x) for x in st_ver.split(".")[:2])
        if major_minor < (1, 45):
            return st_ver
    except Exception:
        pass
    return None


def install_python_deps(
    with_all: bool = False,
    with_docling: bool = False,
    with_camelot: bool = False,
    with_online_ext: bool = True,
    fix_streamlit: bool = False,
    use_uv: bool = True,
    index_url: str = "",
    dry_run: bool = False,
) -> Dict[str, List[str]]:
    failed: List[str] = []
    installed: List[str] = []

    batches: List[Tuple[str, List[str], bool]] = [
        ("Core dependencies", CORE_PACKAGES, True),
        ("Local PDF voting & OCR engines", PDF_ENGINE_PACKAGES, True),
    ]
    if with_online_ext:
        batches.append(("Online scrapers & Zotero sync", ONLINE_EXT_PACKAGES, False))
    batches.append(("Optional Rust pre-scanner", BEST_EFFORT_OPTIONAL, False))

    heavy: List[str] = []
    if with_all or with_docling:
        heavy.append("docling>=2.0.0")
    if with_all or with_camelot:
        heavy.append("camelot-py[cv]>=0.11.0")
    if heavy:
        batches.append(("Heavy optional engines", heavy, False))

    for label, pkgs, required in batches:
        print(f"\n[setup_env] Installing {label}: {', '.join(pkgs)}")
        ok, out = _pip_install(pkgs, use_uv=use_uv, index_url=index_url, dry_run=dry_run)
        if ok:
            installed.extend(pkgs)
            print(f"  [OK] {label}")
            continue

        # Fallback per-package so one optional/incompatible package does not block others
        print(f"  [WARN] Batch install returned non-zero for {label}; retrying per package...")
        for pkg in pkgs:
            ok_single, out_single = _pip_install([pkg], use_uv=use_uv, index_url=index_url, dry_run=dry_run)
            if ok_single:
                installed.append(pkg)
                print(f"    [OK] {pkg}")
            else:
                tag = "REQUIRED" if required else "OPTIONAL"
                print(f"    [{tag} FAIL] {pkg}: {out_single.splitlines()[-1] if out_single else 'unknown error'}")
                failed.append(pkg)

    st_ver = _detect_streamlit_pillow_conflict()
    if st_ver and fix_streamlit:
        print(f"\n[setup_env] Upgrading streamlit ({st_ver}) & pillow for compatibility...")
        _pip_install(["streamlit", "pillow"], use_uv=use_uv, index_url=index_url, upgrade=True, dry_run=dry_run)
    elif st_ver:
        print(
            f"\n[NOTE] Detected streamlit {st_ver} (prefers pillow<12) alongside pdfplumber (prefers pillow>=12.2)."
            " Imports work fine; run `python setup_env.py --fix-streamlit` if you want `pip check` clean."
        )

    return {"installed": installed, "failed": failed}


def install_playwright_chromium(dry_run: bool = False) -> bool:
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    if dry_run:
        print("DRY-RUN:", " ".join(cmd))
        return True
    print("\n[setup_env] Ensuring Playwright Chromium runtime is installed...")
    proc = subprocess.run(cmd, cwd=str(ROOT_DIR))
    return proc.returncode == 0


def install_onnx_models(dry_run: bool = False) -> bool:
    cmd = [sys.executable, str(ROOT_DIR / "setup_paths.py"), "--download-models"]
    if dry_run:
        print("DRY-RUN:", " ".join(cmd))
        return True
    print("\n[setup_env] Verifying/downloading local ONNX visual & orientation models...")
    proc = subprocess.run(cmd, cwd=str(ROOT_DIR))
    return proc.returncode == 0


def check_or_install_system_deps(install: bool = False, dry_run: bool = False) -> Dict[str, object]:
    gs = shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c")
    tess = shutil.which("tesseract")
    mineru_cli = shutil.which("mineru-open-api")
    brew = shutil.which("brew") if IS_MACOS else None

    if install and IS_MACOS:
        if not brew:
            print("[WARN] Homebrew (`brew`) not found on macOS; install from https://brew.sh then run `brew install ghostscript tesseract`.")
        else:
            missing_brew = [pkg for pkg, bin_path in [("ghostscript", gs), ("tesseract", tess)] if not bin_path]
            if missing_brew:
                cmd = [brew, "install", *missing_brew]
                print(f"[setup_env] Installing macOS system dependencies: {' '.join(cmd)}")
                if not dry_run:
                    subprocess.run(cmd)
                    gs = shutil.which("gs")
                    tess = shutil.which("tesseract")
    elif install and IS_WINDOWS:
        winget = shutil.which("winget")
        if not gs and winget:
            print("[INFO] Ghostscript not found. Optional for Camelot (img2table works without it). You can install via: winget install ArtifexSoftware.Ghostscript")

    return {
        "os": SYSTEM,
        "ghostscript": bool(gs),
        "tesseract": bool(tess),
        "mineru_open_api_cli": bool(mineru_cli),
    }


def check_readiness() -> Dict[str, object]:
    modules: Dict[str, Dict[str, object]] = {}
    for mod_name, pkg_label, tier in CHECK_MODULES:
        try:
            mod = importlib.import_module(mod_name)
            try:
                ver = importlib.metadata.version(pkg_label.split("[")[0])
            except Exception:
                v_attr = getattr(mod, "__version__", None)
                ver = v_attr if isinstance(v_attr, str) else "installed"
            modules[mod_name] = {"ok": True, "version": str(ver), "tier": tier, "package": pkg_label}
        except Exception as exc:
            modules[mod_name] = {"ok": False, "error": str(exc), "tier": tier, "package": pkg_label}

    # Check Playwright Chromium binary via repo helper
    chromium_path = None
    try:
        from scripts.playwright_utils import find_playwright_chromium
        chromium_path = find_playwright_chromium()
    except Exception:
        pass

    # Check local ONNX models
    models_dir = ROOT_DIR / "models"
    models = {
        "doc_ori.onnx": (models_dir / "doc_ori.onnx").is_file(),
        "doclayout_yolo_docstructbench_imgsz1280_2501.onnx": (
            models_dir / "doclayout_yolo_docstructbench_imgsz1280_2501.onnx"
        ).is_file(),
    }

    # Check config.json & credential presence (boolean only, never expose secret values)
    cfg_path = ROOT_DIR / "config.json"
    creds: Dict[str, bool] = {}
    if cfg_path.exists():
        try:
            from scripts.common import load_config
            cfg = load_config()
            for key in (
                "MINERU_TOKEN",
                "PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN",
                "ELSEVIER_API_KEY",
                "FIRECRAWL_API_KEY",
                "LLM_API_KEY",
            ):
                creds[key] = bool(str(cfg.get(key) or "").strip())
        except Exception:
            pass

    sys_tools = check_or_install_system_deps(install=False)
    core_ok = all(v["ok"] for v in modules.values() if v["tier"] == "core")
    pdf_ok = all(v["ok"] for v in modules.values() if v["tier"] == "pdf-engine")

    return {
        "os": SYSTEM,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "core_ready": core_ok,
        "pdf_engines_ready": pdf_ok,
        "playwright_chromium": bool(chromium_path),
        "playwright_chromium_path": chromium_path,
        "models": models,
        "config_exists": cfg_path.exists(),
        "credentials_configured": creds,
        "system_tools": sys_tools,
        "modules": modules,
    }


def _print_readiness(report: Dict[str, object]) -> None:
    print(f"\n=== Zotero Table Extractor Environment Readiness ({report['os']}, Python {report['python']}) ===")
    print(f"Executable : {report['executable']}")
    print(f"Core Ready : {report['core_ready']} | PDF Engines Ready: {report['pdf_engines_ready']}")
    print(f"Chromium   : {report['playwright_chromium']} ({report.get('playwright_chromium_path') or 'not found'})")
    print(f"Models     : {report['models']}")
    print(f"config.json: {report['config_exists']} | Credentials: {report['credentials_configured']}")
    print(f"System     : {report['system_tools']}")
    print("\nModules:")
    for name, info in report["modules"].items():
        status = "OK" if info["ok"] else "MISSING"
        detail = info.get("version") if info["ok"] else info.get("error", "")
        print(f"  - {name:16s} [{info['tier']:14s}] {status:7s} {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cross-platform dependency, Playwright, ONNX model & readiness setup for Zotero Table Extractor"
    )
    parser.add_argument("--check", action="store_true", help="Run read-only environment readiness check")
    parser.add_argument("--json", action="store_true", help="Output readiness report as JSON (with --check)")
    parser.add_argument("--all", action="store_true", help="Install heavy optional engines too (docling, camelot-py[cv])")
    parser.add_argument("--with-docling", action="store_true", help="Also install IBM Docling")
    parser.add_argument("--with-camelot", action="store_true", help="Also install Camelot")
    parser.add_argument("--no-online-ext", action="store_true", help="Skip optional online scrapers (scrapling/crawl4ai/pyzotero)")
    parser.add_argument("--fix-streamlit", action="store_true", help="Upgrade streamlit & pillow together to resolve pillow<12 conflict")
    parser.add_argument("--system-deps", action="store_true", help="Install OS-level helpers where supported (e.g. macOS brew ghostscript/tesseract)")
    parser.add_argument("--skip-browser", action="store_true", help="Skip `playwright install chromium`")
    parser.add_argument("--skip-models", action="store_true", help="Skip ONNX model verification/download")
    parser.add_argument("--init-config", action="store_true", help="Run `setup_paths.py --auto` if config.json does not exist")
    parser.add_argument("--mirror", default="", help="Custom PyPI index URL (overrides UV_INDEX_URL / config.json)")
    parser.add_argument("--no-uv", action="store_true", help="Force `python -m pip` instead of `uv`")
    parser.add_argument("--dry-run", action="store_true", help="Print install commands without executing them")
    args = parser.parse_args()

    if args.check:
        report = check_readiness()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_readiness(report)
        return 0 if report["core_ready"] else 1

    index_url = _get_index_url(args.mirror)
    res = install_python_deps(
        with_all=args.all,
        with_docling=args.with_docling,
        with_camelot=args.with_camelot,
        with_online_ext=not args.no_online_ext,
        fix_streamlit=args.fix_streamlit,
        use_uv=not args.no_uv,
        index_url=index_url,
        dry_run=args.dry_run,
    )

    if not args.skip_browser:
        install_playwright_chromium(dry_run=args.dry_run)

    if not args.skip_models:
        install_onnx_models(dry_run=args.dry_run)

    if args.system_deps:
        check_or_install_system_deps(install=True, dry_run=args.dry_run)

    if args.init_config and not (ROOT_DIR / "config.json").exists() and not args.dry_run:
        subprocess.run([sys.executable, str(ROOT_DIR / "setup_paths.py"), "--auto"], cwd=str(ROOT_DIR))

    if not args.dry_run:
        report = check_readiness()
        _print_readiness(report)
        if res["failed"]:
            print(f"\n[WARN] Some packages did not install cleanly: {res['failed']}")
        return 0 if report["core_ready"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
