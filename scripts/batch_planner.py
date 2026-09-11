#!/usr/bin/env python3
"""
batch_planner.py — 批量提取前的预分析（Pre-analysis）阶段（原 planner.py）

在真正开始昂贵的在线抓取 / PaddleOCR-VL 解析之前，先对每篇 PDF 做一次廉价的
"体检"，产出一份提取计划（plan）：

  - 元数据解析：DOI / 标题 / 最终落地 URL（复用 online.graph.ResolveMetadataNode）
  - 渠道分类：is_cnki / publisher（cnki, elsevier, springer, wiley, general, none）
  - PDF 类型探测：是否扫描版（无文本层）、页数
  - 提取策略建议：online_viable（有 URL/DOI 可在线抓取）

预分析本身也是并行的（analyze_batch 使用线程池），这样批处理时可以
"先分析、后提取"：不同出版社的 HTML 抓取与 PaddleOCR 解析随后即可
按分组并发派发，避免串行 DAG 中逐个渠道试探的时间浪费。
"""

import os
import re
import urllib.parse
import concurrent.futures

try:
    import pymupdf as fitz
except ImportError:
    import fitz

# 复用在线提取图中的元数据解析节点（保持单一实现）
try:
    from .online.graph import ResolveMetadataNode
except ImportError:
    try:
        from online.graph import ResolveMetadataNode
    except ImportError:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from online.graph import ResolveMetadataNode


def _classify_publisher(url, is_cnki):
    """根据落地 URL 域名对出版社渠道分组。"""
    if is_cnki:
        return "cnki"
    if not url:
        return "none"
    domain = urllib.parse.urlparse(url).netloc.lower()
    if "cnki.net" in domain or "cnki.com.cn" in domain:
        return "cnki"
    if "sciencedirect.com" in domain or "elsevier.com" in domain:
        return "elsevier"
    if "springer.com" in domain or "springerlink" in domain or "nature.com" in domain:
        return "springer"
    if "wiley.com" in domain:
        return "wiley"
    if domain:
        return "general"
    return "none"


def analyze_pdf(pdf_path, db_path=None, api_key=None):
    """
    对单个 PDF 做预分析，返回 plan 字典：
      pdf_path, doi, title, url, is_cnki, publisher,
      is_scanned, n_pages, online_viable
    """
    plan = {
        "pdf_path": pdf_path,
        "doi": None,
        "title": None,
        "year": None,
        "url": None,
        "is_cnki": False,
        "publisher": "none",
        "is_scanned": False,
        "n_pages": 0,
        "online_viable": False,
    }

    # 1. 本地 PDF 快速探测（纯本地操作，毫秒级）
    if isinstance(pdf_path, str) and not pdf_path.startswith(("http://", "https://")):
        try:
            with fitz.open(pdf_path) as doc:
                plan["n_pages"] = len(doc)
                total_chars = 0
                for p in doc:
                    total_chars += len(p.get_text().strip())
            plan["is_scanned"] = total_chars < 200
        except Exception as e:
            print(f"[Planner] PDF 探测失败 {pdf_path}: {e}")

    # 2. 元数据解析（复用 ResolveMetadataNode，含 Zotero DB / PDF 内容 / 标题搜索）
    try:
        node = ResolveMetadataNode()
        state = {"pdf_path": pdf_path, "db_path": db_path, "api_key": api_key}
        state, _next = node.execute(state)
        for k in ("doi", "title", "url", "is_cnki", "year"):
            if k in state and state.get(k) is not None:
                plan[k] = state.get(k)
    except Exception as e:
        print(f"[Planner] 元数据解析失败 {pdf_path}: {e}")

    # 3. 若元数据未获取到年份，从文件名或标题自适应提取
    if not plan.get("year"):
        search_targets = [os.path.basename(str(pdf_path)), str(plan.get("title") or "")]
        for tgt in search_targets:
            m_yr = re.search(r'(?:^|[\D_])(19\d{2}|20\d{2})(?:[\D_]|$)', tgt)
            if m_yr:
                plan["year"] = int(m_yr.group(1))
                break

    # 4. 渠道分组与策略建议
    plan["publisher"] = _classify_publisher(plan.get("url"), plan.get("is_cnki"))
    plan["online_viable"] = bool(plan.get("url") or plan.get("doi"))
    return plan


def analyze_batch(pdf_files, db_path=None, api_key=None, max_workers=8):
    """
    并行预分析一批 PDF。返回 {pdf_path: plan}（保持输入顺序的插入序）。
    """
    if not pdf_files:
        return {}

    max_workers = max(1, min(max_workers, len(pdf_files)))
    plans = {}
    print(f"[Planner] 开始并行预分析 {len(pdf_files)} 个 PDF (workers={max_workers})...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut_map = {
            ex.submit(analyze_pdf, p, db_path, api_key): p
            for p in pdf_files
        }
        for fut in concurrent.futures.as_completed(fut_map):
            p = fut_map[fut]
            try:
                plans[p] = fut.result()
            except Exception as e:
                print(f"[Planner] 预分析失败 {p}: {e}")
                plans[p] = {
                    "pdf_path": p, "doi": None, "title": None, "url": None,
                    "is_cnki": False, "publisher": "none", "is_scanned": False,
                    "n_pages": 0, "online_viable": False,
                }

    # 汇总打印渠道分布，便于判断并行收益
    dist = {}
    for plan in plans.values():
        dist[plan["publisher"]] = dist.get(plan["publisher"], 0) + 1
    online_cnt = sum(1 for plan in plans.values() if plan["online_viable"])
    scanned_cnt = sum(1 for plan in plans.values() if plan["is_scanned"])
    print(f"[Planner] 预分析完成: {len(plans)} 篇 | 可在线抓取 {online_cnt} 篇 | "
          f"扫描版 {scanned_cnt} 篇 | 渠道分布: {dist}")
    return plans


def group_by_publisher(pdf_files, plans):
    """
    按出版社渠道对任务分组（同组共享相似抓取策略/速率特征）。
    返回 {publisher: [pdf_path, ...]}，按输入顺序稳定排列。
    """
    groups = {}
    for p in pdf_files:
        pub = (plans.get(p) or {}).get("publisher", "none")
        groups.setdefault(pub, []).append(p)
    return groups
