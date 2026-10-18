#!/usr/bin/env python3
"""
ocr_client.py — PaddleOCR 在线 API 客户端（支持 PP-StructureV3 与 PaddleOCR-VL-1.6）。

通过 AIStudio 在线 API (https://paddleocr.aistudio-app.com/api/v2/ocr/jobs) 调度：
1. PP-StructureV3: 用于前置版面分析、表格定位与切图提取；
2. PaddleOCR-VL-1.6: 用于后置高精度多模态 VLM 表格结构化解析；
3. 两阶段串联流水线 (run_ppstructure_vlm_pipeline): PP-StructureV3 先定位表格 -> PaddleOCR-VL-1.6 精细解析。
"""

import io
import json
import os
import re
import sys
import time
import tempfile
import zipfile
import collections
from typing import List, Dict, Any, Optional, Union, Tuple, Set

import logging
import requests
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import pymupdf as fitz
except ImportError:
    import fitz
sys.modules['fitz'] = fitz

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from .common import load_config, clean_text, clean_latex_and_ocr, TABLE_LABEL_RE, format_table_label
except ImportError:
    from common import load_config, clean_text, clean_latex_and_ocr, TABLE_LABEL_RE, format_table_label

try:
    from .table_postprocess import parse_structured_vlm_content
except ImportError:
    try:
        from table_postprocess import parse_structured_vlm_content
    except ImportError:
        parse_structured_vlm_content = None

try:
    from .pdf_page_filter import page_may_contain_tables
except ImportError:
    try:
        from pdf_page_filter import page_may_contain_tables
    except ImportError:
        page_may_contain_tables = None


# ===========================================================================
# 在线 API 常量
# ===========================================================================

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"

OPTIONAL_PAYLOAD = {
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useChartRecognition": False,
}


def get_optional_payload(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    根据本地文档方向检测器可用性动态生成 optionalPayload。
    - 当本地 Tier 1/2 doc_orientation_detector 可用时，保持 useDocOrientationClassify=False，
      避免与本地无损校准发生二次重复翻转；
    - 当本地模型缺失或 ONNX 运行时不可用 (优雅降级) 时，动态将 useDocOrientationClassify 置为 True，
      确保旋转扫描页在云端仍有方向纠偏防线。
    """
    payload = dict(OPTIONAL_PAYLOAD)
    if config:
        if "USE_DOC_ORIENTATION_CLASSIFY" in config:
            payload["useDocOrientationClassify"] = bool(config["USE_DOC_ORIENTATION_CLASSIFY"])
            return payload
        if "useDocOrientationClassify" in config:
            payload["useDocOrientationClassify"] = bool(config["useDocOrientationClassify"])
            return payload

    local_available = False
    try:
        try:
            from .doc_orientation_detector import get_orientation_detector
        except ImportError:
            from doc_orientation_detector import get_orientation_detector
        detector = get_orientation_detector()
        if detector is not None and detector.is_available():
            local_available = True
    except Exception:
        local_available = False

    if not local_available:
        payload["useDocOrientationClassify"] = True
    return payload


def _sleep_cancel_aware(seconds: float, cancel_event=None) -> bool:
    """可响应取消事件的休眠函数，若收到取消信号返回 True。"""
    steps = max(1, int(seconds * 2))
    for _ in range(steps):
        if cancel_event is not None and cancel_event.is_set():
            return True
        time.sleep(seconds / steps)
    return False


# ===========================================================================
# MinerU 官方 API 客户端与多云视觉容灾集成
# ===========================================================================

def format_page_ranges(page_ranges: Optional[Union[str, List[int]]]) -> Optional[str]:
    """格式化页码范围参数，兼容整数列表 (0-based) 与字符串范围 (如 '1-3,5')。"""
    if not page_ranges:
        return None
    if isinstance(page_ranges, str):
        return page_ranges.strip()
    if isinstance(page_ranges, (list, tuple, set)):
        if not page_ranges:
            return None
        # 内部页码集合统一为 0-based 索引，无条件转换为 MinerU 官方要求的 1-based 页码
        sorted_p = [p + 1 for p in sorted(set(int(p) for p in page_ranges))]
        ranges = []
        start = sorted_p[0]
        end = sorted_p[0]
        for p in sorted_p[1:]:
            if p == end + 1:
                end = p
            else:
                ranges.append(f"{start}-{end}" if start != end else f"{start}")
                start = end = p
        ranges.append(f"{start}-{end}" if start != end else f"{start}")
        return ",".join(ranges)
    return str(page_ranges)


def _extract_polymorphic_text(val: Any) -> str:
    """多态文本提取辅助函数：统一兼容字符串、字典、字典列表或多层嵌套。"""
    if not val:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, dict):
        txt = (
            val.get("text") or
            val.get("content") or
            val.get("title") or
            val.get("caption") or
            val.get("raw") or
            val.get("value") or
            ""
        )
        if txt:
            return str(txt).strip()
        nested = val.get("children") or val.get("blocks") or val.get("items") or val.get("paragraphs") or val.get("lines") or val.get("spans")
        if nested:
            return _extract_polymorphic_text(nested)
        parts = [str(v).strip() for v in val.values() if isinstance(v, (str, int, float)) and str(v).strip()]
        return " ".join(parts).strip()
    if isinstance(val, (list, tuple, set)):
        parts = []
        for sub in val:
            txt = _extract_polymorphic_text(sub)
            if txt:
                parts.append(txt)
        return " ".join(parts).strip()
    return str(val).strip()


def _make_columns_unique(cols: List[str]) -> List[str]:
    """生成绝对唯一的列名列表，杜绝重名冲突。"""
    seen = set()
    unique_cols = []
    for idx, c in enumerate(cols):
        candidate = c if (c and str(c).strip()) else f"Col_{idx+1}"
        if candidate not in seen:
            seen.add(candidate)
            unique_cols.append(candidate)
        else:
            k = 1
            new_name = f"{candidate}_{k}"
            while new_name in seen:
                k += 1
                new_name = f"{candidate}_{k}"
            seen.add(new_name)
            unique_cols.append(new_name)
    return unique_cols


SUB_MAP = str.maketrans('0123456789+-=()aehijklmnoprstuvx', '₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ')
SUP_MAP = str.maketrans('0123456789+-−=()ni', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿⁱ')

def _convert_sub_sup(text: str) -> str:
    """将 HTML 中的 <sub>/<sup> 标签转换为高保真 Unicode 上下标，确保 pd.read_html 及导出时学术符号完整。"""
    text = re.sub(r'<sub>(.*?)</sub>', lambda m: m.group(1).translate(SUB_MAP), text, flags=re.IGNORECASE)
    text = re.sub(r'<sup>(.*?)</sup>', lambda m: m.group(1).translate(SUP_MAP), text, flags=re.IGNORECASE)
    return text


def _parse_mineru_table_html(table_html_or_md: str) -> List[pd.DataFrame]:
    """将 MinerU 返回的 table_body (HTML 或 Markdown) 安全解析为 DataFrame 列表。"""
    if not isinstance(table_html_or_md, str):
        if isinstance(table_html_or_md, dict):
            table_html_or_md = (
                table_html_or_md.get("html") or
                table_html_or_md.get("body") or
                table_html_or_md.get("markdown") or
                table_html_or_md.get("text") or
                ""
            )
        table_html_or_md = str(table_html_or_md) if table_html_or_md is not None else ""
    if not table_html_or_md or not table_html_or_md.strip():
        return []
    content = table_html_or_md.strip()

    if "<table" in content.lower():
        try:
            converted_content = _convert_sub_sup(content)
            # 锁定单元格为 str，避免自动将样号 001、范围 10-20、编码等误转为数值/日期
            conv = collections.defaultdict(lambda: str)
            dfs = pd.read_html(io.StringIO(converted_content), keep_default_na=False, converters=conv)
            cleaned_dfs = []
            for df in dfs:
                if df is None or df.empty:
                    continue
                # 规范化并展开 MultiIndex 复合表头
                if isinstance(df.columns, pd.MultiIndex):
                    flat_cols = []
                    for col_tuple in df.columns:
                        parts = []
                        for p in col_tuple:
                            if p is None:
                                continue
                            p_str = clean_latex_and_ocr(clean_text(str(p))).strip()
                            # 仅过滤系统生成的 unnamed 或纯数字列标签如 col_1，保护真实词汇如 Color, Cold, Column
                            if p_str and not re.match(r'^(?:unnamed\b|col[_\s]*\d+$)', p_str, re.IGNORECASE) and p_str.lower() not in ('nan', 'none'):
                                if not parts or p_str != parts[-1]:
                                    parts.append(p_str)
                        flat_cols.append('_'.join(parts) if parts else '')
                    df.columns = _make_columns_unique(flat_cols)
                else:
                    cols = [
                        clean_latex_and_ocr(clean_text(str(c))).strip()
                        if pd.notna(c) and not re.match(r'^(?:unnamed\b|col[_\s]*\d+$|none$|nan$)', str(c).strip(), re.IGNORECASE)
                        else f"Col_{i+1}"
                        for i, c in enumerate(df.columns)
                    ]
                    df.columns = _make_columns_unique(cols)
                cleaned_dfs.append(df)
            if cleaned_dfs:
                return cleaned_dfs
        except Exception:
            pass

    # Markdown 表格降级解析
    if parse_structured_vlm_content is not None:
        return parse_structured_vlm_content(content)
    return []


def extract_mineru_structured_archive(archive_input: Any) -> List[pd.DataFrame]:
    """
    从 MinerU 解析结果的 ZIP 包中深度提取结构化表格：
    1. 动态匹配 JSON 索引文件：优先匹配 *content_list*.json, *middle*.json, *structured_content*.json 等；
    2. 提取结构化字段：table_body (HTML/Markdown), bbox, polymorphic table_caption, table_footnote；
    3. 安全解析 HTML / 表格至 DataFrame，干净展平 MultiIndex 多级复合表头，并锁定原始数据类型；
    4. 完整绑定元数据至 df.attrs ('extractor': 'mineru_api', 'page_idx', 'bbox', 'table_title', 'table_footnote')。
    """
    zf = None
    should_close = False
    try:
        if isinstance(archive_input, zipfile.ZipFile):
            zf = archive_input
        elif isinstance(archive_input, (bytes, bytearray)):
            zf = zipfile.ZipFile(io.BytesIO(archive_input))
            should_close = True
        elif isinstance(archive_input, io.BytesIO):
            zf = zipfile.ZipFile(archive_input)
            should_close = True
        elif isinstance(archive_input, str):
            if archive_input.startswith(("http://", "https://")):
                resp = requests.get(archive_input, timeout=120)
                if resp.status_code != 200:
                    print(f"[MinerU] 下载 ZIP 产物包失败 status={resp.status_code}")
                    return []
                zf = zipfile.ZipFile(io.BytesIO(resp.content))
                should_close = True
            elif os.path.exists(archive_input):
                zf = zipfile.ZipFile(archive_input)
                should_close = True
            else:
                return []
        else:
            return []

        names = zf.namelist()

        def _find_table_body_and_box(obj: Any) -> Tuple[Optional[str], Optional[list]]:
            if not obj:
                return None, None
            if isinstance(obj, str):
                if "<table" in obj.lower() or "\n|" in obj or obj.strip().startswith("|"):
                    return obj.strip(), None
                return None, None
            if isinstance(obj, dict):
                bbox = obj.get("bbox") or obj.get("box") or obj.get("rect")
                # Direct body keys
                for k in ("table_body", "html", "table_html", "body"):
                    val = obj.get(k)
                    if isinstance(val, str) and val.strip():
                        return val.strip(), bbox
                    elif isinstance(val, dict):
                        sub_b, sub_box = _find_table_body_and_box(val)
                        if sub_b:
                            return sub_b, bbox or sub_box

                # Content key
                c = obj.get("content")
                if isinstance(c, str) and c.strip():
                    if "<table" in c.lower() or "\n|" in c or c.strip().startswith("|"):
                        return c.strip(), bbox
                elif isinstance(c, dict):
                    sub_b, sub_box = _find_table_body_and_box(c)
                    if sub_b:
                        return sub_b, bbox or sub_box

                # Table block check
                obj_type = str(obj.get("type", "")).lower()
                if obj_type in ("table", "table_body"):
                    for k in ("markdown", "text", "raw"):
                        val = obj.get(k)
                        if isinstance(val, str) and val.strip() and ("<table" in val.lower() or "\n|" in val or val.strip().startswith("|")):
                            return val.strip(), bbox

                # Deep search in blocks / lines / spans / elements
                for child_k in ("blocks", "lines", "spans", "elements"):
                    children = obj.get(child_k)
                    if isinstance(children, list):
                        for child in children:
                            sub_b, sub_box = _find_table_body_and_box(child)
                            if sub_b:
                                return sub_b, bbox or sub_box

                return None, bbox
            return None, None

        def _collect_table_candidates(data: Any, inherited_page: Optional[int] = None) -> List[Tuple[dict, Optional[int]]]:
            results = []
            if not data:
                return results

            if isinstance(data, list):
                is_nested_pages = any(isinstance(elem, list) for elem in data) if data else False
                is_page_dict_list = all(isinstance(elem, dict) and any(k in elem for k in ("blocks", "preproc_blocks", "elements", "tables")) for elem in data) if data else False
                for idx, elem in enumerate(data):
                    page = idx if ((is_nested_pages or is_page_dict_list) and inherited_page is None) else inherited_page
                    results.extend(_collect_table_candidates(elem, page))
                return results

            if isinstance(data, dict):
                curr_page = inherited_page
                raw_p = data.get("page_idx")
                if raw_p is None:
                    p_no = data.get("page_no")
                    if isinstance(p_no, int) and p_no > 0:
                        curr_page = p_no - 1
                    elif isinstance(p_no, str):
                        try:
                            curr_page = max(0, int(p_no) - 1)
                        except Exception:
                            pass
                else:
                    try:
                        curr_page = int(raw_p)
                    except Exception:
                        pass

                # 1. pages tree (docvortex / middle v2)
                if "pages" in data and isinstance(data["pages"], list):
                    for pg in data["pages"]:
                        p_idx = pg.get("page_idx")
                        if p_idx is None:
                            p_no = pg.get("page_no")
                            p_idx = (p_no - 1) if (isinstance(p_no, int) and p_no > 0) else curr_page
                        blocks = pg.get("blocks", []) or pg.get("content", [])
                        results.extend(_collect_table_candidates(blocks, p_idx))
                    return results

                # 2. pdf_info tree
                if "pdf_info" in data and isinstance(data["pdf_info"], list):
                    for p_info in data["pdf_info"]:
                        p_idx = p_info.get("page_idx")
                        if p_idx is None:
                            p_no = p_info.get("page_no")
                            p_idx = (p_no - 1) if (isinstance(p_no, int) and p_no > 0) else curr_page
                        blocks = p_info.get("preproc_blocks", []) + p_info.get("blocks", []) + p_info.get("tables", [])
                        results.extend(_collect_table_candidates(blocks, p_idx))
                    return results

                # 3. dict itself as table item (evaluated before generic container keys to protect block-level bbox & metadata)
                item_type = str(data.get("type", "")).lower()
                has_table_attr = any(k in data for k in ("table_body", "table_html"))
                if item_type in ("table", "table_body") or has_table_attr:
                    results.append((data, curr_page))
                    return results

                # 4. generic container keys
                for container_k in ("content_list", "elements", "blocks", "tables", "data"):
                    val = data.get(container_k)
                    if isinstance(val, (list, dict)):
                        sub = _collect_table_candidates(val, curr_page)
                        if sub:
                            results.extend(sub)
                            return results

            return results

        def _extract_page_size_from_dict(d):
            if not isinstance(d, dict):
                return None
            for k in ("page_size", "size", "pagesize"):
                val = d.get(k)
                if isinstance(val, (list, tuple)) and len(val) >= 2:
                    try:
                        w, h = float(val[0]), float(val[1])
                        if w > 0 and h > 0:
                            return (w, h)
                    except Exception:
                        pass
            w = d.get("page_w") or d.get("width") or d.get("w")
            h = d.get("page_h") or d.get("height") or d.get("h")
            if w is not None and h is not None:
                try:
                    w, h = float(w), float(h)
                    if w > 0 and h > 0:
                        return (w, h)
                except Exception:
                    pass
            return None

        # 收集所有 JSON 文件并按优先级排序
        json_candidates = []
        for name in names:
            base = os.path.basename(name)
            if base.startswith('.') or '__MACOSX' in name:
                continue
            if not base.lower().endswith('.json'):
                continue
            if re.search(r'content_list', base, re.IGNORECASE):
                priority = 1
            elif re.search(r'middle', base, re.IGNORECASE):
                priority = 2
            elif re.search(r'structured_content', base, re.IGNORECASE):
                priority = 3
            else:
                priority = 4
            json_candidates.append((priority, name))

        json_candidates.sort(key=lambda x: (x[0], len(x[1])))

        # 预先扫描各 JSON (特别是 middle.json / pdf_info / pages) 提取页面物理尺寸
        known_page_sizes = {}
        for _, jname in json_candidates:
            try:
                raw_scan = zf.read(jname)
                scan_data = json.loads(raw_scan.decode('utf-8', errors='replace'))
                if isinstance(scan_data, dict):
                    for p_info in scan_data.get("pdf_info", []):
                        if isinstance(p_info, dict):
                            pidx = p_info.get("page_idx")
                            if pidx is None:
                                p_no = p_info.get("page_no")
                                if isinstance(p_no, int) and p_no > 0:
                                    pidx = p_no - 1
                            ps = _extract_page_size_from_dict(p_info)
                            if pidx is not None and ps:
                                known_page_sizes[int(pidx)] = ps
                    for pg in scan_data.get("pages", []):
                        if isinstance(pg, dict):
                            pidx = pg.get("page_idx")
                            if pidx is None:
                                p_no = pg.get("page_no")
                                if isinstance(p_no, int) and p_no > 0:
                                    pidx = p_no - 1
                            ps = _extract_page_size_from_dict(pg)
                            if pidx is not None and ps:
                                known_page_sizes[int(pidx)] = ps
            except Exception:
                pass

        # 逐个尝试候选 JSON，具有优雅降级回退
        all_candidate_tables = []
        for _, json_name in json_candidates:
            try:
                raw_bytes = zf.read(json_name)
                data = json.loads(raw_bytes.decode('utf-8', errors='replace'))
                raw_items = _collect_table_candidates(data)
                if not raw_items:
                    continue

                is_content_list = bool(re.search(r'content_list', os.path.basename(json_name), re.IGNORECASE))
                extracted_dfs = []
                for item, default_page in raw_items:
                    try:
                        table_body, box = _find_table_body_and_box(item)
                        if not table_body:
                            continue

                        raw_p = item.get("page_idx")
                        if raw_p is None:
                            p_no = item.get("page_no")
                            if isinstance(p_no, int) and p_no > 0:
                                page_idx = p_no - 1
                            elif isinstance(p_no, str):
                                try:
                                    page_idx = max(0, int(p_no) - 1)
                                except Exception:
                                    page_idx = default_page if default_page is not None else 0
                            else:
                                page_idx = default_page if default_page is not None else 0
                        else:
                            try:
                                page_idx = int(raw_p)
                            except Exception:
                                page_idx = default_page if default_page is not None else 0

                        content_obj = {}
                        container_children = item.get("content") or item.get("blocks") or []
                        if isinstance(container_children, dict):
                            content_obj = container_children
                        elif isinstance(container_children, list):
                            for c_elem in container_children:
                                if isinstance(c_elem, dict):
                                    elem_type = str(c_elem.get("type", "")).lower()
                                    if "caption" in elem_type or "title" in elem_type:
                                        content_obj.setdefault("table_caption", c_elem)
                                    elif "footnote" in elem_type or "note" in elem_type:
                                        content_obj.setdefault("table_footnote", c_elem)
                                    for k in ("table_caption", "caption", "title", "table_footnote", "footnote", "footnotes", "label", "is_continuation"):
                                        if k in c_elem:
                                            content_obj.setdefault(k, c_elem[k])

                        raw_caption = (
                            item.get("table_caption") or item.get("caption") or item.get("title") or
                            content_obj.get("table_caption") or content_obj.get("caption") or content_obj.get("title")
                        )
                        caption = _extract_polymorphic_text(raw_caption)

                        raw_footnote = (
                            item.get("table_footnote") or item.get("footnote") or item.get("footnotes") or
                            content_obj.get("table_footnote") or content_obj.get("footnote") or content_obj.get("footnotes")
                        )
                        footnote = _extract_polymorphic_text(raw_footnote)

                        raw_lbl = item.get("label") or item.get("table_label") or content_obj.get("label") or content_obj.get("table_label")
                        label = format_table_label(str(raw_lbl)) if raw_lbl else ""
                        if not label and caption:
                            m_lbl = TABLE_LABEL_RE.search(caption)
                            if m_lbl:
                                label = format_table_label(m_lbl.group(0))

                        is_continuation = bool(
                            item.get("is_continuation") or
                            content_obj.get("is_continuation") or
                            re.search(r'(?:续表|接上表|续上表|（续）|\(续\)|continued|cont\'?d|\(cont\b|cont\.)', caption, re.IGNORECASE)
                        )

                        dfs = _parse_mineru_table_html(table_body)
                        for df in dfs:
                            if df is not None and not df.empty:
                                df.attrs['extractor'] = 'mineru_api'
                                df.attrs['page_idx'] = page_idx
                                df.attrs['bbox'] = box
                                if page_idx in known_page_sizes:
                                    df.attrs['page_size'] = known_page_sizes[page_idx]
                                if is_content_list:
                                    df.attrs['bbox_format'] = 'norm_1000'
                                    df.attrs['coord_format'] = 'norm_1000'
                                else:
                                    df.attrs['bbox_format'] = 'points'
                                    df.attrs['coord_format'] = 'points'
                                df.attrs['table_title'] = caption
                                df.attrs['table_footnote'] = footnote
                                df.attrs['footnotes'] = [footnote] if footnote else []
                                if label:
                                    df.attrs['label'] = label
                                if is_continuation:
                                    df.attrs['is_continuation'] = True
                                extracted_dfs.append(df)
                    except Exception as e_item:
                        logger.debug(f"[MinerU] 解析单个 item 异常已跳过: {e_item}")
                        continue

                if extracted_dfs:
                    all_candidate_tables.append((json_name, extracted_dfs))
            except Exception as e_json:
                logger.debug(f"[MinerU] 解析候选 JSON {json_name} 失败，尝试下一候选: {e_json}")
                continue

        if all_candidate_tables:
            if len(all_candidate_tables) == 1:
                return all_candidate_tables[0][1]

            # 跨表示融合多文件结果：取最大表示或融合非重复表格，补齐单一 JSON 遗漏的表格
            def _calc_valid_cells(df):
                if df is None or df.empty:
                    return 0
                return sum(1 for v in df.values.flatten() if pd.notna(v) and str(v).strip() not in ('', 'nan', 'none'))

            def _normalize_mineru_bbox_to_1000(bbox, bbox_format=None, page_size=None):
                if not bbox or len(bbox) < 4:
                    return None
                try:
                    x0, y0, x1, y1 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
                except Exception:
                    return None

                # 确保坐标顺序正常: min, min, max, max
                x0, x1 = min(x0, x1), max(x0, x1)
                y0, y1 = min(y0, y1), max(y0, y1)

                if bbox_format == 'norm_1000':
                    return [x0, y0, x1, y1]

                # Check if coordinates are in 0..1 range
                if max(x0, y0, x1, y1) <= 1.0 and max(x0, y0, x1, y1) > 0:
                    return [x0 * 1000.0, y0 * 1000.0, x1 * 1000.0, y1 * 1000.0]

                # Points format: normalize using page_size
                pw, ph = 595.28, 841.89
                if page_size and len(page_size) >= 2:
                    try:
                        w, h = float(page_size[0]), float(page_size[1])
                        if w > 0 and h > 0:
                            pw, ph = w, h
                    except Exception:
                        pass

                # Detect rotated/transposed page orientation
                if x1 > pw and x1 <= ph and y1 <= pw:
                    pw, ph = ph, pw

                return [x0 / pw * 1000.0, y0 / ph * 1000.0, x1 / pw * 1000.0, y1 / ph * 1000.0]

            def _is_same_mineru_table(t1, t2):
                l1 = t1.attrs.get('label')
                l2 = t2.attrs.get('label')
                p1 = t1.attrs.get('page_idx')
                p2 = t2.attrs.get('page_idx')

                fmt_l1 = format_table_label(l1) if l1 else None
                fmt_l2 = format_table_label(l2) if l2 else None

                # 0. 明确表号冲突：若两者均具有明确且不同的表号（如 Table 1 vs Table 2），绝非同一张表
                if fmt_l1 and fmt_l2 and fmt_l1 != fmt_l2:
                    return False

                # 若两表处于明确不同的页面，绝非同一张表
                if p1 is not None and p2 is not None and p1 != p2:
                    return False

                # 1. 明确表号相同
                if fmt_l1 and fmt_l2 and fmt_l1 == fmt_l2:
                    return True

                # 2. 同页且空间 IoU 高 (统一归一化为 0-1000 坐标系后再计算 IoU，杜绝 points 与 norm_1000 混淆误删)
                if (p1 is None or p2 is None or p1 == p2):
                    b1 = t1.attrs.get('bbox')
                    b2 = t2.attrs.get('bbox')
                    ps1 = t1.attrs.get('page_size') or known_page_sizes.get(p1)
                    ps2 = t2.attrs.get('page_size') or known_page_sizes.get(p2)
                    nb1 = _normalize_mineru_bbox_to_1000(b1, t1.attrs.get('bbox_format'), ps1)
                    nb2 = _normalize_mineru_bbox_to_1000(b2, t2.attrs.get('bbox_format'), ps2)
                    if nb1 and nb2 and len(nb1) >= 4 and len(nb2) >= 4:
                        inter = max(0.0, min(nb1[2], nb2[2]) - max(nb1[0], nb2[0])) * max(0.0, min(nb1[3], nb2[3]) - max(nb1[1], nb2[1]))
                        a1 = max(0.0, (nb1[2] - nb1[0]) * (nb1[3] - nb1[1]))
                        a2 = max(0.0, (nb2[2] - nb2[0]) * (nb2[3] - nb2[1]))
                        union = a1 + a2 - inter
                        if union > 0 and (inter / union) >= 0.3:
                            return True

                # 3. 同页 (或页码未知) 且数据单元格高重合度 (仅限表号无冲突时)
                if (p1 is None or p2 is None or p1 == p2) and (not fmt_l1 or not fmt_l2):
                    c1 = set(str(v).strip().lower() for v in t1.values.flatten() if pd.notna(v) and str(v).strip() not in ('', 'nan', 'none'))
                    c2 = set(str(v).strip().lower() for v in t2.values.flatten() if pd.notna(v) and str(v).strip() not in ('', 'nan', 'none'))
                    if c1 and c2:
                        overlap = len(c1 & c2) / max(1, min(len(c1), len(c2)))
                        if overlap >= 0.5:
                            return True

                return False

            master_dfs = list(all_candidate_tables[0][1])
            for _, dfs in all_candidate_tables[1:]:
                for new_t in dfs:
                    dup_idx = None
                    for idx, existing_t in enumerate(master_dfs):
                        if _is_same_mineru_table(existing_t, new_t):
                            dup_idx = idx
                            break
                    if dup_idx is not None:
                        existing_t = master_dfs[dup_idx]
                        if _calc_valid_cells(new_t) > _calc_valid_cells(existing_t):
                            for k, v in existing_t.attrs.items():
                                if k not in new_t.attrs or not new_t.attrs[k]:
                                    new_t.attrs[k] = v
                            master_dfs[dup_idx] = new_t
                        else:
                            for k, v in new_t.attrs.items():
                                if k not in existing_t.attrs or not existing_t.attrs[k]:
                                    existing_t.attrs[k] = v
                    else:
                        master_dfs.append(new_t)

            if master_dfs:
                def _table_sort_key(d):
                    p = d.attrs.get('page_idx', 0)
                    if p is None:
                        p = 0
                    box = d.attrs.get('bbox')
                    top = box[1] if (box and len(box) >= 2) else 0
                    return (p, top)
                master_dfs.sort(key=_table_sort_key)
                return master_dfs

        # 若未从 JSON 解析出表格，优雅回退至从 .md 文件解析
        md_files = [n for n in names if n.lower().endswith('.md') and not os.path.basename(n).startswith('.') and '__MACOSX' not in n]
        if md_files:
            chosen_md = md_files[0]
            for n in md_files:
                if os.path.basename(n).lower() in ("output.md", "auto.md", "full.md"):
                    chosen_md = n
                    break
            md_text = zf.read(chosen_md).decode('utf-8', errors='replace')
            if parse_structured_vlm_content is not None:
                dfs = parse_structured_vlm_content(md_text)
                for df in dfs:
                    if df is not None and not df.empty:
                        df.attrs['extractor'] = 'mineru_api'
                return dfs

        return []
    except Exception as e:
        print(f"[MinerU] 解析结构化 ZIP 产物包失败: {e}")
        return []
    finally:
        if should_close and zf is not None:
            try:
                zf.close()
            except Exception:
                pass


class MinerUClient:
    """
    MinerU 官方云端 API 客户端 (mineru.net)。
    官方提供每日 1,000 页免费提取额度，用于多云双活与百度 AIStudio 队列熔断兜底。
    集成零 Token Agent 备用 API 与结构化高精表格抽取。
    """
    def __init__(self, api_key: str = "", api_base: str = "https://mineru.net"):
        self.api_key = api_key or os.getenv("MINERU_API_KEY", "")
        self.api_base = (api_base or os.getenv("MINERU_API_BASE", "https://mineru.net") or "https://mineru.net").rstrip("/")
        self._agent_tasks: Set[str] = set()
        self._single_tasks: Set[str] = set()

    def submit_task(
        self,
        file_path: str,
        is_ocr: bool = True,
        enable_table: bool = True,
        enable_formula: bool = True,
        page_ranges: Optional[Union[str, List[int]]] = None,
        pages: Optional[Union[str, List[int]]] = None,
    ) -> Optional[str]:
        """
        提交解析任务至 MinerU API，返回 batch_id 或 task_id。
        支持本地 PDF / 图片文件及远程 URL，支持 page_ranges/pages 指定页码范围。
        未配置 MINERU_API_KEY 时，自动对 <=10MB 且 <=20 页的文档走 Agent API 零 Token 备用提取。
        """
        if page_ranges is None and pages is not None:
            page_ranges = pages
        page_ranges_str = format_page_ranges(page_ranges)

        if not self.api_key:
            # 零 Token Agent 备用 API 触发检查 (文件大小 <= 10MB 且页数 <= 20)
            is_eligible = False
            file_size_bytes = 0
            page_count = 1
            if isinstance(file_path, str) and os.path.exists(file_path):
                file_size_bytes = os.path.getsize(file_path)
                if file_path.lower().endswith(".pdf"):
                    try:
                        with fitz.open(file_path) as doc:
                            page_count = len(doc)
                    except Exception:
                        page_count = 1
                if file_size_bytes <= 10 * 1024 * 1024 and page_count <= 20:
                    is_eligible = True

            if is_eligible:
                print(f"[MinerU Agent API] 未配置 MINERU_API_KEY，且文件满足免费额度限制 (大小: {file_size_bytes / (1024*1024):.2f}MB <= 10MB, 页数: {page_count} <= 20)，触发零 Token Agent 备用 API 提交...")
                return self._submit_agent_task(file_path, page_ranges=page_ranges_str)
            else:
                print("[MinerU] 提示: 未配置 MINERU_API_KEY，无法提交任务")
                return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        # 远程 URL 方式
        if isinstance(file_path, str) and file_path.startswith(("http://", "https://")):
            url = f"{self.api_base}/api/v4/extract/task"
            payload = {
                "url": file_path,
                "is_ocr": is_ocr,
                "enable_table": enable_table,
                "enable_formula": enable_formula,
                "model": "vlm",
                "model_version": "vlm"
            }
            if page_ranges_str:
                payload["page_ranges"] = page_ranges_str
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=60)
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    task_id = data.get("task_id") or data.get("batch_id")
                    if task_id:
                        self._single_tasks.add(str(task_id))
                    return task_id
                else:
                    print(f"[MinerU] URL 任务提交失败 status={resp.status_code}: {resp.text[:200]}")
                    return None
            except Exception as e:
                print(f"[MinerU] URL 任务提交异常: {e}")
                return None

        # 本地文件方式：注册批量上传并 PUT 到 OSS
        if not os.path.exists(file_path):
            print(f"[MinerU] 文件不存在: {file_path}")
            return None

        batch_url = f"{self.api_base}/api/v4/file-urls/batch"
        filename = os.path.basename(file_path)
        file_item = {"name": filename}
        if page_ranges_str:
            file_item["page_ranges"] = page_ranges_str
        payload = {
            "files": [file_item],
            "model_version": "vlm",
            "enable_formula": enable_formula,
            "enable_table": enable_table,
            "is_ocr": is_ocr
        }
        if page_ranges_str:
            payload["page_ranges"] = page_ranges_str

        try:
            resp = requests.post(batch_url, json=payload, headers=headers, timeout=60)
            if resp.status_code != 200:
                print(f"[MinerU] 批量上传注册失败 status={resp.status_code}: {resp.text[:200]}")
                return None
            res_json = resp.json()
            data = res_json.get("data", {}) if "data" in res_json else res_json
            batch_id = data.get("batch_id") or data.get("id") or res_json.get("batch_id")
            
            # 获取上传链接
            upload_url = None
            if "file_urls" in data and isinstance(data["file_urls"], list) and data["file_urls"]:
                upload_url = data["file_urls"][0]
            elif "files" in data and isinstance(data["files"], list) and data["files"]:
                upload_url = data["files"][0].get("upload_url")
            elif "files" in res_json and isinstance(res_json["files"], list) and res_json["files"]:
                upload_url = res_json["files"][0].get("upload_url")

            if not upload_url:
                print(f"[MinerU] 未从响应中获得上传 URL: {res_json}")
                return None

            # 上传文件内容 (注意: Content-Type 置空以防止签名冲突)
            with open(file_path, "rb") as f:
                put_headers = {"Content-Type": ""}
                put_resp = requests.put(upload_url, data=f, headers=put_headers, timeout=120)
                if put_resp.status_code not in (200, 201):
                    print(f"[MinerU] 文件上传 OSS 失败 status={put_resp.status_code}")
                    return None

            return batch_id
        except Exception as e:
            print(f"[MinerU] 提交任务异常: {e}")
            return None

    def _submit_agent_task(self, file_path: str, page_ranges: Optional[str] = None) -> Optional[str]:
        """向 MinerU 零 Token Agent 备用 API (/api/v1/agent/parse/file) 提交轻量任务。
        按照官方协议：POST JSON 获取预签名上传 URL -> PUT 二进制上传 -> 返回 task_id。
        """
        agent_url = f"{self.api_base}/api/v1/agent/parse/file"
        try:
            filename = os.path.basename(file_path)
            payload = {
                "filename": filename,
                "file_name": filename,
                "backend": "pipeline",
                "return_md": True,
            }
            if page_ranges:
                payload["page_range"] = page_ranges
            headers = {"Content-Type": "application/json"}
            resp = requests.post(agent_url, json=payload, headers=headers, timeout=120)
            if resp.status_code == 200:
                res_json = resp.json()
                data = res_json.get("data")
                task_id = None
                upload_url = None
                if isinstance(data, dict):
                    task_id = data.get("task_id") or data.get("batch_id") or res_json.get("task_id") or res_json.get("batch_id")
                    upload_url = data.get("file_url") or data.get("upload_url") or data.get("url") or res_json.get("file_url") or res_json.get("upload_url") or res_json.get("url")
                elif isinstance(data, str):
                    task_id = data
                elif data is None:
                    task_id = res_json.get("task_id") or res_json.get("batch_id")
                    upload_url = res_json.get("file_url") or res_json.get("upload_url") or res_json.get("url")

                if upload_url:
                    with open(file_path, "rb") as f:
                        put_headers = {"Content-Type": ""}
                        put_resp = requests.put(upload_url, data=f, headers=put_headers, timeout=120)
                        if isinstance(getattr(put_resp, 'status_code', None), int) and put_resp.status_code not in (200, 201):
                            print(f"[MinerU Agent API] 文件上传失败 status={put_resp.status_code}")
                            return None

                if task_id:
                    self._agent_tasks.add(str(task_id))
                    return str(task_id)
            print(f"[MinerU Agent API] 提交失败 status={resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[MinerU Agent API] 提交异常: {e}")
        return None

    def poll_task(self, task_id: str, timeout: int = 600, cancel_event=None) -> Dict[str, Any]:
        """
        轮询 MinerU 任务状态，直至完成或超时。
        """
        if not task_id:
            return {"success": False, "error": "invalid_args"}

        timeout_env = os.getenv("MINERU_POLL_TIMEOUT")
        if timeout_env:
            try:
                timeout = int(timeout_env)
            except Exception:
                pass
        else:
            try:
                from common import load_config
                _cfg = load_config()
                if _cfg.get("MINERU_POLL_TIMEOUT"):
                    timeout = int(_cfg.get("MINERU_POLL_TIMEOUT"))
            except Exception:
                pass

        is_agent_task = (str(task_id) in self._agent_tasks) or (not self.api_key)
        is_single_task = (str(task_id) in getattr(self, '_single_tasks', set()))
        headers = {
            "Content-Type": "application/json"
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        task_query_url = f"{self.api_base}/api/v4/extract/task/{task_id}"
        if is_agent_task:
            query_url = f"{self.api_base}/api/v1/agent/parse/{task_id}"
        elif is_single_task:
            query_url = task_query_url
        else:
            query_url = f"{self.api_base}/api/v4/extract-results/batch/{task_id}"

        start_time = time.time()
        print(f"[MinerU] 任务已提交 (ID: {task_id})，开始轮询结果...")

        while True:
            if cancel_event is not None and cancel_event.is_set():
                print("[MinerU] 收到取消信号，中止轮询")
                return {"success": False, "error": "cancelled"}

            if time.time() - start_time > timeout:
                print(f"[MinerU] 任务轮询超时 ({timeout}s)")
                return {"success": False, "error": "timeout"}

            try:
                resp = requests.get(query_url, headers=headers, timeout=30)
                if not is_agent_task and query_url != task_query_url:
                    switch_to_single = False
                    if resp.status_code == 404:
                        switch_to_single = True
                    elif resp.status_code == 200:
                        try:
                            rj = resp.json()
                            code = rj.get("code")
                            data_field = rj.get("data")
                            if code not in (0, 200, None) or (not data_field and rj.get("state") is None):
                                switch_to_single = True
                        except Exception:
                            pass
                    if switch_to_single:
                        resp_single = requests.get(task_query_url, headers=headers, timeout=30)
                        resp = resp_single
                        query_url = task_query_url
                        self._single_tasks.add(str(task_id))
            except Exception as e:
                print(f"[MinerU] 轮询网络异常: {e}")
                if _sleep_cancel_aware(5, cancel_event):
                    return {"success": False, "error": "cancelled"}
                continue

            if resp.status_code != 200:
                print(f"[MinerU] 轮询状态码非 200: {resp.status_code}")
                if _sleep_cancel_aware(5, cancel_event):
                    return {"success": False, "error": "cancelled"}
                continue

            res_json = resp.json()
            code = res_json.get("code")
            msg = res_json.get("msg") or res_json.get("message")
            if code not in (0, 200, None):
                print(f"[MinerU] 任务执行失败: code={code}, msg={msg}")
                return {"success": False, "state": "failed", "error": msg or f"API error code {code}"}

            data = res_json.get("data")
            if data is None:
                data = {}
            elif isinstance(data, str):
                data = {"markdown": data, "state": "done"}
            state = data.get("state") or data.get("status") or res_json.get("state")
            error_msg = data.get("message") or data.get("errorMsg") or data.get("err_msg")

            extract_results = data.get("extract_result") or data.get("extract_results") or []
            if isinstance(extract_results, list) and extract_results:
                first_res = extract_results[0]
                if isinstance(first_res, dict):
                    state = first_res.get("state") or first_res.get("status") or state
                    if not error_msg:
                        error_msg = first_res.get("err_msg") or first_res.get("message") or first_res.get("errorMsg")

            if state in ("done", "success") or (is_agent_task and (
                data.get("markdown") or data.get("full_zip_url") or data.get("markdown_url") or
                (isinstance(extract_results, list) and extract_results and isinstance(extract_results[0], dict) and (
                    extract_results[0].get("markdown") or extract_results[0].get("markdown_url") or extract_results[0].get("full_zip_url")
                ))
            )):
                print("[MinerU] 任务解析成功！")
                return {"success": True, "state": "done", "data": data}
            elif state in ("failed", "error"):
                error_msg = error_msg or "Unknown failure"
                print(f"[MinerU] 任务执行失败: {error_msg}")
                return {"success": False, "state": "failed", "error": error_msg}
            else:
                if _sleep_cancel_aware(5, cancel_event):
                    return {"success": False, "error": "cancelled"}

    def parse_mineru_table_result(self, result_data: Any) -> str:
        """
        解析 MinerU 返回的产物，提取 Markdown 字符串。
        """
        if not result_data:
            return ""

        if isinstance(result_data, str):
            if "\n|" in result_data or result_data.strip().startswith("#"):
                return result_data
            if result_data.startswith(("http://", "https://")) and ".zip" in result_data:
                return self._download_and_extract_md(result_data)

        if isinstance(result_data, dict):
            if "markdown" in result_data and result_data["markdown"]:
                return result_data["markdown"]

            md_url = result_data.get("markdown_url")
            if md_url:
                try:
                    r = requests.get(md_url, timeout=60)
                    if r.status_code == 200:
                        return r.text
                except Exception:
                    pass

            extract_results = result_data.get("extract_result") or result_data.get("extract_results") or []
            if isinstance(extract_results, list) and extract_results:
                first_res = extract_results[0]
                if isinstance(first_res, dict):
                    if "markdown" in first_res and first_res["markdown"]:
                        return first_res["markdown"]
                    first_md_url = first_res.get("markdown_url")
                    if first_md_url:
                        try:
                            r = requests.get(first_md_url, timeout=60)
                            if r.status_code == 200:
                                return r.text
                        except Exception:
                            pass
                    if "full_zip_url" in first_res and first_res["full_zip_url"]:
                        return self._download_and_extract_md(first_res["full_zip_url"])

            zip_url = result_data.get("result_url") or result_data.get("full_zip_url")
            if zip_url:
                return self._download_and_extract_md(zip_url)

        return ""

    def parse_mineru_structured_result(self, result_data: Any) -> List[pd.DataFrame]:
        """
        从 MinerU 任务返回数据中解析结构化表格 DataFrames：
        支持 ZIP URL 下载解压，或直接解析 JSON 数据中的 content_list。
        """
        if not result_data:
            return []

        if isinstance(result_data, (bytes, bytearray, io.BytesIO)):
            return extract_mineru_structured_archive(result_data)

        if isinstance(result_data, str):
            if result_data.startswith(("http://", "https://")):
                dfs = extract_mineru_structured_archive(result_data)
                if dfs:
                    return dfs
            if os.path.exists(result_data) and result_data.lower().endswith(".zip"):
                return extract_mineru_structured_archive(result_data)
            if ("\n|" in result_data or "<table" in result_data.lower()) and parse_structured_vlm_content is not None:
                dfs = parse_structured_vlm_content(result_data)
                for df in dfs:
                    if df is not None and not df.empty:
                        df.attrs['extractor'] = 'mineru_api'
                return dfs

        if isinstance(result_data, dict):
            md_url = result_data.get("markdown_url")
            if md_url:
                try:
                    r = requests.get(md_url, timeout=60)
                    if r.status_code == 200 and r.text:
                        dfs = parse_structured_vlm_content(r.text) if parse_structured_vlm_content is not None else []
                        for df in dfs:
                            if df is not None and not df.empty:
                                df.attrs['extractor'] = 'mineru_api'
                        if dfs:
                            return dfs
                except Exception:
                    pass

            extract_results = result_data.get("extract_result") or result_data.get("extract_results") or []
            if isinstance(extract_results, list) and extract_results:
                first_res = extract_results[0]
                if isinstance(first_res, dict):
                    first_md_url = first_res.get("markdown_url")
                    if first_md_url:
                        try:
                            r = requests.get(first_md_url, timeout=60)
                            if r.status_code == 200 and r.text:
                                dfs = parse_structured_vlm_content(r.text) if parse_structured_vlm_content is not None else []
                                for df in dfs:
                                    if df is not None and not df.empty:
                                        df.attrs['extractor'] = 'mineru_api'
                                if dfs:
                                    return dfs
                        except Exception:
                            pass
                    if "markdown" in first_res and first_res["markdown"] and parse_structured_vlm_content is not None:
                        dfs = parse_structured_vlm_content(first_res["markdown"])
                        for df in dfs:
                            if df is not None and not df.empty:
                                df.attrs['extractor'] = 'mineru_api'
                        if dfs:
                            return dfs
                    zip_url = first_res.get("full_zip_url") or first_res.get("result_url") or first_res.get("zip_url")
                    if zip_url:
                        dfs = extract_mineru_structured_archive(zip_url)
                        if dfs:
                            return dfs
                    if "content_list" in first_res or "pdf_info" in first_res or "elements" in first_res:
                        bio = io.BytesIO()
                        with zipfile.ZipFile(bio, 'w') as zf:
                            zf.writestr("content_list.json", json.dumps(first_res))
                        bio.seek(0)
                        return extract_mineru_structured_archive(bio)

            zip_url = result_data.get("result_url") or result_data.get("full_zip_url")
            if zip_url:
                dfs = extract_mineru_structured_archive(zip_url)
                if dfs:
                    return dfs

            if "content_list" in result_data or "pdf_info" in result_data or "elements" in result_data:
                bio = io.BytesIO()
                with zipfile.ZipFile(bio, 'w') as zf:
                    zf.writestr("content_list.json", json.dumps(result_data))
                bio.seek(0)
                return extract_mineru_structured_archive(bio)

            if "markdown" in result_data and result_data["markdown"] and parse_structured_vlm_content is not None:
                dfs = parse_structured_vlm_content(result_data["markdown"])
                for df in dfs:
                    if df is not None and not df.empty:
                        df.attrs['extractor'] = 'mineru_api'
                return dfs

        return []

    def _download_and_extract_md(self, zip_url: str) -> str:
        try:
            resp = requests.get(zip_url, timeout=120)
            if resp.status_code != 200:
                print(f"[MinerU] 下载 ZIP 结果包失败 status={resp.status_code}")
                return ""
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                md_files = [n for n in zf.namelist() if n.lower().endswith(".md")]
                if md_files:
                    chosen = md_files[0]
                    for name in md_files:
                        base = os.path.basename(name).lower()
                        if base in ("output.md", "auto.md", "full.md"):
                            chosen = name
                            break
                    md_bytes = zf.read(chosen)
                    return md_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[MinerU] 解压提取 Markdown 失败: {e}")
        return ""


def mineru_submit_task(
    file_path: str,
    config: Optional[dict] = None,
    page_ranges: Optional[Union[str, List[int]]] = None,
) -> Optional[str]:
    """快捷提交 MinerU 任务。"""
    if config is None:
        config = load_config()
    client = MinerUClient(
        api_key=config.get("MINERU_API_KEY", ""),
        api_base=config.get("MINERU_API_BASE", "https://mineru.net")
    )
    return client.submit_task(file_path, page_ranges=page_ranges)


def mineru_poll_task(task_id: str, config: Optional[dict] = None, timeout: int = 600, cancel_event=None) -> Dict[str, Any]:
    """快捷轮询 MinerU 任务。"""
    if config is None:
        config = load_config()
    client = MinerUClient(
        api_key=config.get("MINERU_API_KEY", ""),
        api_base=config.get("MINERU_API_BASE", "https://mineru.net")
    )
    return client.poll_task(task_id, timeout=timeout, cancel_event=cancel_event)


def parse_mineru_table_result(result_data: Any) -> str:
    """解析 MinerU 产物并返回 Markdown 字符串。"""
    client = MinerUClient()
    return client.parse_mineru_table_result(result_data)


def parse_mineru_structured_result(result_data: Any) -> List[pd.DataFrame]:
    """解析 MinerU 产物并返回结构化 DataFrame 列表。"""
    client = MinerUClient()
    return client.parse_mineru_structured_result(result_data)


def call_mineru_api(
    file_path: str,
    config: Optional[dict] = None,
    timeout: int = 600,
    cancel_event=None,
    page_ranges: Optional[Union[str, List[int]]] = None,
    pages: Optional[Union[str, List[int]]] = None,
) -> str:
    """
    通过 MinerU 官方云端 API 进行高保真多模态解析并返回 Markdown 表格字符串。
    """
    if page_ranges is None and pages is not None:
        page_ranges = pages
    if config is None:
        config = load_config()
    client = MinerUClient(
        api_key=config.get("MINERU_API_KEY", ""),
        api_base=config.get("MINERU_API_BASE", "https://mineru.net")
    )
    task_id = client.submit_task(file_path, page_ranges=page_ranges)
    if not task_id:
        return ""
    poll_res = client.poll_task(task_id, timeout=timeout, cancel_event=cancel_event)
    if not poll_res.get("success"):
        return ""
    return client.parse_mineru_table_result(poll_res.get("data", {}))


def call_mineru_structured_api(
    file_path: str,
    config: Optional[dict] = None,
    timeout: int = 600,
    cancel_event=None,
    page_ranges: Optional[Union[str, List[int]]] = None,
    pages: Optional[Union[str, List[int]]] = None,
) -> List[pd.DataFrame]:
    """
    通过 MinerU 官方云端 API 进行高保真多模态解析，直接返回高质量结构化 DataFrame 列表。
    """
    if page_ranges is None and pages is not None:
        page_ranges = pages
    if config is None:
        config = load_config()
    client = MinerUClient(
        api_key=config.get("MINERU_API_KEY", ""),
        api_base=config.get("MINERU_API_BASE", "https://mineru.net")
    )
    task_id = client.submit_task(file_path, page_ranges=page_ranges)
    if not task_id:
        return []
    poll_res = client.poll_task(task_id, timeout=timeout, cancel_event=cancel_event)
    if not poll_res.get("success"):
        return []
    return client.parse_mineru_structured_result(poll_res.get("data", {}))


# 全局默认 MinerU 客户端实例
mineru_client = MinerUClient()


def is_mineru_available(config: Optional[dict] = None, file_path: Optional[str] = None) -> bool:
    """
    检查 MinerU 是否可用：
    1. 配置了 MINERU_API_KEY；
    2. 或文件符合零 Token 备用 Agent API 条件 (文件存在、<=10MB 且 <=20 页)。
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            config = {}
    key = config.get("MINERU_API_KEY", "") or os.getenv("MINERU_API_KEY", "")
    if key:
        return True
    if file_path and isinstance(file_path, str) and os.path.exists(file_path):
        try:
            if os.path.getsize(file_path) <= 10 * 1024 * 1024:
                if file_path.lower().endswith(".pdf"):
                    with fitz.open(file_path) as doc:
                        if len(doc) <= 20:
                            return True
                else:
                    return True
        except Exception:
            pass
    return False


# ===========================================================================
# 核心：通用 AIStudio 异步作业调用与轮询
# ===========================================================================

def call_paddleocr_job(
    file_path: str,
    model: str,
    config: Optional[dict] = None,
    timeout: int = 600,
    cancel_event=None
) -> Dict[str, Any]:
    """
    通用 AIStudio 作业调用底层函数。支持 PP-StructureV3 / PaddleOCR-VL-1.6 等任意模型。
    支持本地文件路径或公开可访问的 HTTP(S) URL。
    当 AIStudio 发生队列已满 (status=400 队列已满) 或超时时，无缝自动故障转移至 MinerU API。

    返回字典：
    {
        "success": bool,
        "model": str,
        "jsonl_url": str,
        "combined_markdown": str,
        "pages": List[Dict[str, Any]], # 每页 layoutParsingResults: markdown text, images dict, outputImages dict
        "error": Optional[str]
    }
    """
    if cancel_event is not None and cancel_event.is_set():
        return {"success": False, "model": model, "error": "cancelled", "combined_markdown": "", "pages": []}

    if config is None:
        config = load_config()

    token = (
        os.getenv("PADDLEOCR_ACCESS_TOKEN")
        or config.get("PADDLEOCR_ACCESS_TOKEN")
        or config.get("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN")
        or config.get("PADDLEOCR_VL_TOKEN")
        or ""
    )
    def _make_failover_result(mineru_md: str) -> Dict[str, Any]:
        return {
            "success": True,
            "model": "mineru",
            "jsonl_url": "",
            "combined_markdown": mineru_md,
            "pages": [{"page_idx": 0, "markdown": mineru_md, "images": {}}],
            "error": None,
            "failover": True
        }

    if not token:
        # 若未配置百度 Token 但 MinerU 可用（配置了 key 或符合 Agent 免费额度），直接平滑切换
        if is_mineru_available(config, file_path):
            print(f"[{model}] 提示: 未配置百度 AIStudio Token，自动切换至 MinerU API 解析...")
            mineru_md = call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)
            if mineru_md:
                return _make_failover_result(mineru_md)
        print(f"[{model}] 错误: 未配置 Token (PADDLEOCR_ACCESS_TOKEN 环境变量或 config.json)")
        return {"success": False, "model": model, "error": "no_token", "combined_markdown": "", "pages": []}

    headers = {
        "Authorization": f"bearer {token}",
    }

    is_url = isinstance(file_path, str) and file_path.startswith(("http://", "https://"))
    file_display = file_path if is_url else os.path.basename(file_path)
    print(f"[{model}] 正在通过在线 API 提交: {file_display}...")

    if not is_url and not os.path.exists(file_path):
        print(f"[{model}] 文件不存在: {file_path}")
        return {"success": False, "model": model, "error": "file_not_found", "combined_markdown": "", "pages": []}

    engine_pref = (config.get("OCR_ENGINE") or os.environ.get("OCR_ENGINE") or "auto").lower()
    can_mineru_failover = is_mineru_available(config, file_path) and engine_pref in ("auto", "mineru")

    # 提交作业（支持指数退避与满队列重试，并在队列爆满或超时时无缝熔断至 MinerU）
    retry_waits = [5, 15, 30, 60, 90]
    job_id = None
    for attempt, wait_time in enumerate(retry_waits):
        if cancel_event is not None and cancel_event.is_set():
            return {"success": False, "model": model, "error": "cancelled", "combined_markdown": "", "pages": []}
        try:
            opt_payload = get_optional_payload(config)
            if is_url:
                h = dict(headers)
                h["Content-Type"] = "application/json"
                payload = {
                    "fileUrl": file_path,
                    "model": model,
                    "optionalPayload": opt_payload
                }
                job_response = requests.post(JOB_URL, json=payload, headers=h, timeout=120)
            else:
                data = {
                    "model": model,
                    "optionalPayload": json.dumps(opt_payload)
                }
                with open(file_path, "rb") as f:
                    files = {"file": f}
                    job_response = requests.post(JOB_URL, headers=headers, data=data, files=files, timeout=120)
        except Exception as e:
            print(f"[{model}] 提交作业失败: {e}")
            is_net_err = isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)) or "timeout" in str(e).lower() or "connection" in str(e).lower()
            if is_net_err and can_mineru_failover:
                print(f"[{model}] 百度 AIStudio 提交网络异常/超时，触发多云熔断，自动切换至 MinerU API...")
                mineru_md = call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)
                if mineru_md:
                    return _make_failover_result(mineru_md)
            return {"success": False, "model": model, "error": str(e), "combined_markdown": "", "pages": []}

        if job_response.status_code == 200:
            resp_data = job_response.json().get("data", {})
            job_id = resp_data.get("jobId")
            break
        elif job_response.status_code == 400 and "队列已满" in job_response.text:
            if can_mineru_failover:
                print(f"[{model}] 遇到百度 AIStudio 队列已满 (status=400 队列已满)，立即触发多云双活熔断，无缝切换至 MinerU API...")
                mineru_md = call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)
                if mineru_md:
                    return _make_failover_result(mineru_md)
            print(f"[{model}] 队列已满, 等待 {wait_time}s 后重试 ({attempt+1}/{len(retry_waits)})...")
            if _sleep_cancel_aware(wait_time, cancel_event):
                return {"success": False, "model": model, "error": "cancelled", "combined_markdown": "", "pages": []}
            continue
        else:
            print(f"[{model}] 提交作业失败, status={job_response.status_code}: {job_response.text[:200]}")
            if job_response.status_code in (500, 502, 503, 504) and can_mineru_failover:
                print(f"[{model}] 百度 AIStudio 服务端异常 (status={job_response.status_code})，触发多云熔断，自动切换至 MinerU API...")
                mineru_md = call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)
                if mineru_md:
                    return _make_failover_result(mineru_md)
            return {"success": False, "model": model, "error": f"status_{job_response.status_code}", "combined_markdown": "", "pages": []}
    else:
        print(f"[{model}] 提交作业重试 {len(retry_waits)} 次后仍失败")
        if can_mineru_failover:
            print(f"[{model}] 百度 AIStudio 多次重试未成功，无缝切换至 MinerU API...")
            mineru_md = call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)
            if mineru_md:
                return _make_failover_result(mineru_md)
        return {"success": False, "model": model, "error": "max_retries_exceeded", "combined_markdown": "", "pages": []}

    if not job_id:
        return {"success": False, "model": model, "error": "no_job_id", "combined_markdown": "", "pages": []}
    print(f"[{model}] 作业已提交, jobId={job_id}, 开始轮询结果...")

    # 轮询结果
    start_time = time.time()
    while True:
        if cancel_event is not None and cancel_event.is_set():
            print(f"[{model}] 收到取消信号，中止轮询")
            return {"success": False, "model": model, "error": "cancelled", "combined_markdown": "", "pages": []}

        if time.time() - start_time > timeout:
            print(f"[{model}] 轮询超时 ({timeout}s)，跳过")
            if can_mineru_failover:
                print(f"[{model}] 百度 AIStudio 轮询超时，无缝切换至 MinerU API...")
                mineru_md = call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)
                if mineru_md:
                    return _make_failover_result(mineru_md)
            return {"success": False, "model": model, "error": "timeout", "combined_markdown": "", "pages": []}

        try:
            result_response = requests.get(f"{JOB_URL}/{job_id}", headers=headers, timeout=30)
        except Exception as e:
            print(f"[{model}] 轮询请求异常: {e}")
            if _sleep_cancel_aware(5, cancel_event):
                return {"success": False, "model": model, "error": "cancelled", "combined_markdown": "", "pages": []}
            continue

        if result_response.status_code != 200:
            print(f"[{model}] 轮询失败, status={result_response.status_code}")
            if _sleep_cancel_aware(5, cancel_event):
                return {"success": False, "model": model, "error": "cancelled", "combined_markdown": "", "pages": []}
            continue

        resp_json = result_response.json()
        data = resp_json.get("data", {})
        state = data.get("state")

        if state == "pending":
            if _sleep_cancel_aware(3, cancel_event):
                return {"success": False, "model": model, "error": "cancelled", "combined_markdown": "", "pages": []}
        elif state == "running":
            try:
                total_pages = data.get("extractProgress", {}).get("totalPages", 0)
                extracted_pages = data.get("extractProgress", {}).get("extractedPages", 0)
                print(f"[{model}] 运行中: {extracted_pages}/{total_pages} 页")
            except Exception:
                pass
            if _sleep_cancel_aware(5, cancel_event):
                return {"success": False, "model": model, "error": "cancelled", "combined_markdown": "", "pages": []}
        elif state == "done":
            jsonl_url = data.get("resultUrl", {}).get("jsonUrl", "")
            print(f"[{model}] 作业完成, 获取结果: {jsonl_url[:60]}...")
            return _download_and_parse_jsonl(jsonl_url, model)
        elif state == "failed":
            error_msg = data.get("errorMsg", "unknown")
            print(f"[{model}] 作业失败: {error_msg}")
            if can_mineru_failover:
                print(f"[{model}] 百度 AIStudio 任务失败 ({error_msg})，无缝切换至 MinerU API...")
                mineru_md = call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)
                if mineru_md:
                    return _make_failover_result(mineru_md)
            return {"success": False, "model": model, "error": error_msg, "combined_markdown": "", "pages": []}
        else:
            if _sleep_cancel_aware(5, cancel_event):
                return {"success": False, "model": model, "error": "cancelled", "combined_markdown": "", "pages": []}


def _download_and_parse_jsonl(jsonl_url: str, model: str = "") -> Dict[str, Any]:
    """
    下载 JSONL 结果并解析每页 Markdown、切图字典 (res['markdown']['images']) 与整体布局。
    """
    try:
        resp = requests.get(jsonl_url, timeout=120)
        resp.raise_for_status()
    except Exception as e:
        print(f"[{model}] 下载结果失败: {e}")
        return {"success": False, "model": model, "error": str(e), "combined_markdown": "", "pages": []}

    lines = resp.text.strip().split('\n')
    all_markdown = []
    pages_data = []

    for line_idx, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            result = json.loads(line).get("result", {})
            layout_results = result.get("layoutParsingResults", [])
            for res_idx, res in enumerate(layout_results):
                md_text = res.get("markdown", {}).get("text", "")
                images_dict = res.get("markdown", {}).get("images", {})
                output_images = res.get("outputImages", {})
                if md_text:
                    all_markdown.append(md_text)

                # 优先提取真实物理页码，避免多 layout 块导致按循环自增错位
                p_val = (
                    res.get("page_index") or res.get("page_idx") or res.get("page_num") or
                    result.get("page_index") or result.get("page_num") or result.get("page_idx")
                )
                if p_val is not None:
                    try:
                        resolved_page_idx = int(p_val)
                    except (ValueError, TypeError):
                        resolved_page_idx = line_idx
                else:
                    resolved_page_idx = line_idx

                pages_data.append({
                    "page_idx": resolved_page_idx,
                    "markdown": md_text,
                    "images": images_dict,
                    "output_images": output_images,
                    "res": res,
                })
        except Exception as e:
            print(f"[{model}] 解析 JSONL 行失败: {e}")
            continue

    combined = "\n\n".join(all_markdown)
    print(f"[{model}] 合并 {len(all_markdown)} 页 Markdown, 总长度 {len(combined)} 字符")
    return {
        "success": True,
        "model": model,
        "jsonl_url": jsonl_url,
        "combined_markdown": combined,
        "pages": pages_data,
        "error": None
    }


# ===========================================================================
# PP-StructureV3 专用接口与切图定位
# ===========================================================================

def call_pp_structure_v3_api(file_path: str, config: Optional[dict] = None, timeout: int = 600, cancel_event=None) -> Dict[str, Any]:
    """
    调用 PP-StructureV3 在线 API 进行版面分析与表格定位。
    """
    if config is None:
        config = load_config()
    model = config.get("PP_STRUCTURE_MODEL", "PP-StructureV3")
    return call_paddleocr_job(file_path, model=model, config=config, timeout=timeout, cancel_event=cancel_event)


def extract_pp_structure_table_crops(pdf_path: str, config: Optional[dict] = None, cancel_event=None, pages: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    """
    通过 PP-StructureV3 提取 PDF 中所有表格的图像切图 Crop 和页面上下文。
    返回结构与原 extract_table_crops_from_pdf 兼容：
    [
        {
            "page_index": int,
            "table_index": int,
            "crop_bbox": None,
            "score": 0.95,
            "has_caption": bool,
            "has_footnote": bool,
            "image": PIL.Image or None,
            "image_url": str,
            "markdown": str,
            "page_text": str,
        },
        ...
    ]
    """
    if config is None:
        config = load_config()

    res = call_pp_structure_v3_api(pdf_path, config=config, timeout=600, cancel_event=cancel_event)
    if not res.get("success") or not res.get("pages"):
        return []

    crops = []
    table_re = re.compile(r'(?:<table|\|[^\n]+\|[^\n]+\||\|[\s:\-|]+\|)', re.IGNORECASE)
    caption_re = re.compile(r'(?:表|Table)\s*\d+', re.IGNORECASE)

    for p_info in res["pages"]:
        if cancel_event is not None and cancel_event.is_set():
            break

        p_idx = p_info["page_idx"]
        if pages is not None and p_idx not in pages:
            continue
        md = p_info.get("markdown", "")
        images = p_info.get("images", {})  # dict { "table_1.png": "http..." }

        # 从 images 字典中找出表格切图
        tbl_img_urls = []
        for img_name, img_url in images.items():
            if "table" in img_name.lower() or "tab" in img_name.lower() or len(images) == 1:
                tbl_img_urls.append((img_name, img_url))

        # 若未从文件名匹配到 table，但该页 markdown 包含表格特征且有切图
        if not tbl_img_urls and (table_re.search(md) or caption_re.search(md)):
            for img_name, img_url in images.items():
                tbl_img_urls.append((img_name, img_url))

        # 下载切图
        for t_idx, (img_name, img_url) in enumerate(tbl_img_urls):
            pil_img = None
            if Image is not None:
                try:
                    img_resp = requests.get(img_url, timeout=30)
                    if img_resp.status_code == 200:
                        pil_img = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
                except Exception as e:
                    print(f"[PP-StructureV3] 下载切图 {img_name} 失败: {e}")

            has_cap = bool(caption_re.search(md))
            crops.append({
                "page_index": p_idx,
                "table_index": t_idx,
                "crop_bbox": None,
                "score": 0.95,
                "has_caption": has_cap,
                "has_footnote": False,
                "image": pil_img,
                "image_url": img_url,
                "markdown": md,
                "page_text": md,
            })

    print(f"[PP-StructureV3] 成功从 PDF 中定位并提取出 {len(crops)} 个表格切图区域。")
    return crops


# ===========================================================================
# PaddleOCR-VL-1.6 专用接口与双阶段融合管线
# ===========================================================================

def call_paddleocr_vl_online_api(
    file_path: str,
    config: Optional[dict] = None,
    timeout: int = 600,
    cancel_event=None,
    pages: Optional[Union[str, List[int]]] = None,
    page_ranges: Optional[Union[str, List[int]]] = None,
) -> str:
    """
    通过 AIStudio 在线 API 调用 PaddleOCR-VL-1.6。
    支持多云双活与容灾回退：
    - 若配置 OCR_ENGINE="mineru"，直接调用 MinerU API；
    - 若配置 OCR_ENGINE="auto"，优先调用 PaddleOCR-VL，在队列已满或超时时无缝自动切换至 MinerU API。
    支持传整个 PDF 文件或单页图片文件 / 图像 URL。
    返回合并后的 Markdown 字符串。
    """
    if pages is None and page_ranges is not None:
        pages = page_ranges
    if page_ranges is None and pages is not None:
        page_ranges = pages
    if config is None:
        config = load_config()

    engine = (config.get("OCR_ENGINE") or os.environ.get("OCR_ENGINE") or "auto").lower()
    if engine == "mineru":
        return call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event, page_ranges=page_ranges, pages=pages)
    if engine == "docling":
        return call_docling_table_extractor(file_path, config=config, cancel_event=cancel_event, pages=pages)

    model = config.get("PADDLEOCR_MCP_MODEL", "PaddleOCR-VL-1.6")
    res = call_paddleocr_job(file_path, model=model, config=config, timeout=timeout, cancel_event=cancel_event)
    md = res.get("combined_markdown", "")
    if not md and is_mineru_available(config, file_path) and not res.get("failover") and engine in ("auto", "mineru"):
        print("[Dual-Cloud Failover] AIStudio 未返回有效解析结果，尝试 MinerU API 进行视觉兜底...")
        md = call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event, page_ranges=page_ranges, pages=pages)
    return md


def run_ppstructure_vlm_pipeline(
    pdf_path: str,
    pages: Optional[List[int]] = None,
    cancel_event=None,
    config: Optional[dict] = None,
) -> str:
    """
    PP-StructureV3 + PaddleOCR-VL-1.6 两阶段融合提取管线：
    1. 第一阶段（定位）：由 PP-StructureV3 快速分析版面，识别表格所在页面、提取表格区域切图（Crop）；
    2. 第二阶段（精细解析）：将表格切图（或目标页）送入 PaddleOCR-VL-1.6 进行多模态精细解析，输出精准结构化表格；
    3. 产出合并后的结构化 Markdown。
    """
    if config is None:
        config = load_config()

    if cancel_event is not None and cancel_event.is_set():
        return ""

    engine = (config.get("OCR_ENGINE") or os.environ.get("OCR_ENGINE") or "auto").lower()
    if engine == "mineru":
        print("[PP-StructureV3 -> VLM] 检测到 OCR_ENGINE='mineru'，直接调用 MinerU API 解析...")
        return call_mineru_api(pdf_path, config=config, timeout=600, cancel_event=cancel_event, page_ranges=pages)

    # 1. 前置定位：PP-StructureV3 版面分析与表格切图定位
    print(f"[PP-StructureV3 -> VLM] 阶段 1: 正在使用 PP-StructureV3 进行版面分析与表格定位...")
    target_file = pdf_path
    temp_subset = None
    if pages and os.path.exists(pdf_path) and pdf_path.lower().endswith(".pdf"):
        try:
            import fitz
            import tempfile as _tmp
            doc_full = fitz.open(pdf_path)
            if len(pages) < len(doc_full):
                doc_sub = fitz.open()
                for p in pages:
                    if 0 <= p < len(doc_full):
                        doc_sub.insert_pdf(doc_full, from_page=p, to_page=p)
                doc_full.close()
                fd, temp_subset = _tmp.mkstemp(prefix="ppstruct_subset_", suffix=".pdf")
                os.close(fd)
                doc_sub.save(temp_subset)
                doc_sub.close()
                target_file = temp_subset
            else:
                doc_full.close()
        except Exception as e:
            print(f"[PP-StructureV3 -> VLM] 子集生成跳过: {e}")

    try:
        pp_res = call_pp_structure_v3_api(target_file, config=config, timeout=600, cancel_event=cancel_event)
    finally:
        if temp_subset and os.path.exists(temp_subset):
            try:
                os.remove(temp_subset)
            except Exception:
                pass

    pp_md = pp_res.get("combined_markdown", "")
    pages_data = pp_res.get("pages", [])

    # 若阶段 1 发生 MinerU 双活故障转移且已获得解析结果，直接产出结果，避免重复冗余请求
    if pp_res.get("failover") and pp_md:
        print("[PP-StructureV3 -> VLM] 阶段 1 已通过 MinerU 双活容灾成功解析，直接产出结果")
        return pp_md

    # 收集包含表格的页面与切图 URL
    table_pages = set()
    table_crop_images = []
    for p_info in pages_data:
        sub_p_idx = p_info["page_idx"]
        orig_p_idx = pages[sub_p_idx] if (pages and sub_p_idx < len(pages)) else sub_p_idx
        md = p_info.get("markdown", "")
        images = p_info.get("images", {})
        has_table = bool(re.search(r'(?:<table|\|[^\n]+\|[^\n]+\||\|[\s:\-|]+\||(?:表|Table)\s*\d+)', md, re.IGNORECASE))
        if has_table or images:
            table_pages.add(orig_p_idx)
            for img_name, img_url in images.items():
                if "table" in img_name.lower() or "tab" in img_name.lower() or len(images) == 1:
                    table_crop_images.append((orig_p_idx, img_name, img_url))

    if pages:
        for p in pages:
            table_pages.add(p)

    sorted_pages = sorted(list(table_pages))
    print(f"[PP-StructureV3 -> VLM] 定位到 {len(sorted_pages)} 个候选表格页面: {[p+1 for p in sorted_pages]}, {len(table_crop_images)} 个表格切图")

    if cancel_event is not None and cancel_event.is_set():
        return pp_md

    # 2. 后置解析：优先将切图提交给 PaddleOCR-VL-1.6 进行针对性 VLM 解析
    if table_crop_images:
        print(f"[PP-StructureV3 -> VLM] 阶段 2: 正在将 {len(table_crop_images)} 个表格切图提交 PaddleOCR-VL-1.6 精细解析...")
        vlm_mds = []
        for orig_p_idx, img_name, img_url in table_crop_images:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                crop_job = call_paddleocr_job(img_url, model="PaddleOCR-VL-1.6", config=config, timeout=120, cancel_event=cancel_event)
                c_md = crop_job.get("combined_markdown", "")
                if c_md:
                    vlm_mds.append(c_md)
            except Exception as e:
                print(f"[PP-StructureV3 -> VLM] 切图 {img_name} VLM 解析异常: {e}")

        if vlm_mds:
            combined_vlm_md = "\n\n".join(vlm_mds)
            print(f"[PP-StructureV3 -> VLM] PaddleOCR-VL-1.6 成功完成切图精细解析！")
            return combined_vlm_md

    # 若无独立切图或切图解析为空，针对候选页面执行 PaddleOCR-VL 识别
    if sorted_pages:
        print(f"[PP-StructureV3 -> VLM] 阶段 2: 针对 {len(sorted_pages)} 个目标页面提交 PaddleOCR-VL-1.6 识别...")
        vlm_md = run_paddleocr_vl(pdf_path, pages=sorted_pages, cancel_event=cancel_event)
        if vlm_md:
            return vlm_md

    # 兜底返回 PP-StructureV3 自身的 Markdown
    return pp_md or ""


# ===========================================================================
# 兼容与辅助接口
# ===========================================================================

call_paddleocr_vl_online = call_paddleocr_vl_online_api

def call_paddleocr_vl_via_uvx(file_path: str, config: dict, cancel_event=None) -> str:
    """[已废弃兼容接口] 统一转向 call_paddleocr_vl_online_api。"""
    timeout = 1200 if str(file_path).lower().endswith('.pdf') else 120
    return call_paddleocr_vl_online_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)


def crops_to_temp_images(crop_items: list) -> list:
    """将裁剪出的表格图片存为临时 PNG 文件路径列表。"""
    image_paths = []
    for i, item in enumerate(crop_items):
        img = item.get("image")
        if img is None:
            continue
        fd, temp_img_path = tempfile.mkstemp(prefix=f"table_crop_{i}_", suffix=".png")
        os.close(fd)
        img.save(temp_img_path, format="PNG")
        image_paths.append(temp_img_path)
    return image_paths


def run_paddleocr_vl(pdf_path: str, pages=None, cancel_event=None, enable_doclayout: bool = True) -> str:
    """
    PaddleOCR-VL 识别入口。
    - 若指定 pages，逐页渲染为图片提交在线 API
    - 否则传整个 PDF 给在线 API
    """
    config = load_config()
    engine = (config.get("OCR_ENGINE") or os.environ.get("OCR_ENGINE") or "auto").lower()
    if engine == "docling":
        return call_docling_table_extractor(pdf_path, pages=pages, config=config, cancel_event=cancel_event)
    if engine == "mineru":
        return call_mineru_api(pdf_path, page_ranges=pages, pages=pages, config=config, cancel_event=cancel_event)

    if cancel_event is not None and cancel_event.is_set():
        return ""

    # 0. 若显式指定了页面列表，按连续页码切片为多页子 PDF 提交（完整保留跨页上下文）
    if pages is not None:
        try:
            doc_full = fitz.open(pdf_path)
            target_pages = sorted(list(set(p for p in pages if 0 <= p < len(doc_full))))
            if target_pages:
                # 将连续页码合并为篇章块（例如 [28, 29] 合并为连续 2 页 PDF 提交，避免跨页断裂）
                page_blocks = []
                curr_block = []
                for p in target_pages:
                    if not curr_block or p == curr_block[-1] + 1:
                        curr_block.append(p)
                    else:
                        page_blocks.append(curr_block)
                        curr_block = [p]
                if curr_block:
                    page_blocks.append(curr_block)

                print(f"[OCR] 将 {len(target_pages)} 个目标页组织为 {len(page_blocks)} 个连续 PDF 块提交 PaddleOCR-VL: {[ [p+1 for p in b] for b in page_blocks ]}")
                block_mds = []
                for block in page_blocks:
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    # 生成连续子 PDF
                    doc_sub = fitz.open()
                    for p in block:
                        doc_sub.insert_pdf(doc_full, from_page=p, to_page=p)
                    fd, temp_sub_pdf = tempfile.mkstemp(prefix=f"paddle_block_p{block[0]+1}_p{block[-1]+1}_", suffix=".pdf")
                    os.close(fd)
                    doc_sub.save(temp_sub_pdf)
                    doc_sub.close()
                    try:
                        b_md = call_paddleocr_vl_online_api(temp_sub_pdf, config=config, timeout=240, cancel_event=cancel_event)
                        if b_md:
                            block_mds.append(b_md)
                    finally:
                        if os.path.exists(temp_sub_pdf):
                            try:
                                os.remove(temp_sub_pdf)
                            except Exception:
                                pass
                doc_full.close()
                if block_mds:
                    return "\n\n".join(block_mds)
            doc_full.close()
        except Exception as e:
            print(f"[OCR Notice] 连续块 PaddleOCR-VL 识别异常: {e}")

    # 1. 传整个 PDF 给在线 API
    print(f"[OCR] 正在将整个 PDF 提交 PaddleOCR-VL-1.6 在线 API 识别: {pdf_path}")
    full_md = call_paddleocr_vl_online_api(pdf_path, config=config, timeout=600, cancel_event=cancel_event)

    if full_md and parse_structured_vlm_content is not None:
        try:
            dfs = parse_structured_vlm_content(full_md)
            if dfs and len(dfs) > 0:
                print(f"[OCR] PaddleOCR-VL 在线 API 成功识别并抽取出 {len(dfs)} 个结构化表格！")
                return full_md
        except Exception:
            pass

    # 补充保障：逐页将包含图表关键词的 PDF 页面渲染为图像调用
    try:
        doc = fitz.open(pdf_path)
        if len(doc) > 1:
            print(f"[OCR] 正在尝试对 PDF 候选页面逐页进行 PaddleOCR-VL 图像识别...")
            page_mds = []
            for page_idx in range(len(doc)):
                if cancel_event is not None and cancel_event.is_set():
                    break
                page = doc[page_idx]
                page_text = page.get_text("text")
                if page_may_contain_tables and not page_may_contain_tables(page_text):
                    continue

                mat = fitz.Matrix(200 / 72.0, 200 / 72.0)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                fd, temp_page_img = tempfile.mkstemp(prefix=f"paddle_page_{page_idx}_", suffix=".png")
                os.close(fd)
                try:
                    pix.save(temp_page_img)
                    print(f"[OCR] 正在对 PDF 第 {page_idx+1} 页执行 PaddleOCR-VL 识别...")
                    p_md = call_paddleocr_vl_online_api(temp_page_img, config=config, timeout=120, cancel_event=cancel_event)
                    if p_md:
                        page_mds.append(p_md)
                finally:
                    if os.path.exists(temp_page_img):
                        try:
                            os.remove(temp_page_img)
                        except Exception:
                            pass

            doc.close()
            if page_mds:
                return "\n\n".join(page_mds)
        else:
            doc.close()
    except Exception as e:
        print(f"[OCR Notice] 逐页 PaddleOCR-VL 识别提示: {e}")

    # 离线备用降级：当 PaddleOCR 在线服务无结果且配置了 Docling 备用时，启动 Docling TableFormer 兜底
    if not full_md and config and config.get("DOCLING_OFFLINE_FALLBACK", True):
        try:
            print("[OCR Fallback] PaddleOCR 未返回表格，尝试启动 IBM Docling (TableFormer) 离线备用提取...")
            docling_md = call_docling_table_extractor(pdf_path, pages=pages, config=config, cancel_event=cancel_event)
            if docling_md:
                print("[OCR Fallback] ✓ Docling (TableFormer) 离线备用提取成功完成！")
                return docling_md
        except Exception as e_d:
            print(f"[OCR Fallback Notice] Docling 备用提取异常: {e_d}")

    return full_md or ""


# ===========================================================================
# 4. IBM Docling (TableFormer) 离线表格提取备用引擎
# ===========================================================================

_DOCLING_CHECKED = False
_DOCLING_AVAILABLE = False


def is_docling_available() -> bool:
    """检查当前环境中是否安装并可用 IBM Docling。"""
    global _DOCLING_CHECKED, _DOCLING_AVAILABLE
    if not _DOCLING_CHECKED:
        try:
            from docling.document_converter import DocumentConverter
            _DOCLING_AVAILABLE = True
        except ImportError:
            _DOCLING_AVAILABLE = False
        except Exception:
            _DOCLING_AVAILABLE = False
        _DOCLING_CHECKED = True
    return _DOCLING_AVAILABLE


def extract_via_docling(
    pdf_path: str,
    pages: Optional[List[int]] = None,
    config: Optional[Dict[str, Any]] = None,
    cancel_event=None,
) -> List[Dict[str, Any]]:
    """
    使用 IBM Docling (内置 TableFormer) 提取 PDF 表格（离线高精度备用方案）。

    专为以下场景设计：
    1. 保持 PP-StructureV3 + PaddleOCR-VL 为首选云端主引擎的前提下，
       将 Docling 作为无网络/无 Token/在线服务限流故障时的离线备用；
    2. 深度处理跨栏复杂学术表格、合并单元格及跨页续表。

    返回标准化表格列表:
    [
        {
            'df': pd.DataFrame,
            'page_idx': int (0-indexed),
            'table_idx': int,
            'bbox': [x0, y0, x1, y1] or None,
        },
        ...
    ]
    """
    if not os.path.exists(pdf_path):
        return []

    if cancel_event is not None and cancel_event.is_set():
        return []

    if not is_docling_available():
        logger.debug("[Docling] docling 未安装，跳过 Docling 离线表格提取")
        return []

    if config is None:
        config = load_config() if callable(load_config) else {}

    if not config.get("DOCLING_ENABLED", True):
        logger.debug("[Docling] DOCLING_ENABLED 为 False，跳过")
        return []

    results = []
    effective_pdf = pdf_path
    temp_slice = None

    try:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions

        # 若指定了特定候选页面且 fitz 可用，为提升速度生成原子子 PDF 仅解析目标页
        page_index_map = None
        if pages is not None and fitz is not None:
            try:
                doc = fitz.open(pdf_path)
                total_p = len(doc)
                valid_pages = [p for p in pages if 0 <= p < total_p]
                if valid_pages and len(valid_pages) < total_p:
                    fd, temp_slice = tempfile.mkstemp(prefix="docling_slice_", suffix=".pdf")
                    os.close(fd)
                    sub_doc = fitz.open()
                    page_index_map = {}
                    for sub_idx, orig_p in enumerate(valid_pages):
                        sub_doc.insert_pdf(doc, from_page=orig_p, to_page=orig_p)
                        page_index_map[sub_idx] = orig_p
                    sub_doc.save(temp_slice)
                    sub_doc.close()
                    effective_pdf = temp_slice
                doc.close()
            except Exception as e_slice:
                logger.debug(f"[Docling] 创建页面切片失败，将全文档解析: {e_slice}")
                effective_pdf = pdf_path

        # 根据页面文字层与图像智能决定是否启用 OCR (扫描件/无文本层/包含潜在图片表格时启用 OCR，纯矢量文字页则禁用 OCR 以加速)
        needs_ocr = False
        try:
            with fitz.open(effective_pdf) as check_doc:
                for p_check in check_doc:
                    text_len = len(p_check.get_text("text").strip())
                    if text_len <= 50:
                        needs_ocr = True
                        break
                    # 正文虽可搜索，但若页面内嵌入栅格图像 (图片表格/扫描插图)，亦须启用 OCR
                    images = p_check.get_images(full=True)
                    if images:
                        for img_info in images:
                            img_w, img_h = img_info[2], img_info[3]
                            if (img_w >= 80 and img_h >= 40) or (img_w * img_h >= 4000):
                                needs_ocr = True
                                break
                    if needs_ocr:
                        break
        except Exception:
            needs_ocr = True

        if config and ("do_ocr" in config or "DO_OCR" in config):
            needs_ocr = bool(config.get("do_ocr", config.get("DO_OCR", needs_ocr)))

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_table_structure = True
        pipeline_options.do_ocr = needs_ocr

        format_opts = {
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
        if hasattr(InputFormat, "IMAGE"):
            try:
                from docling.document_converter import ImageFormatOption
                format_opts[InputFormat.IMAGE] = ImageFormatOption(pipeline_options=pipeline_options)
            except Exception:
                pass

        converter = DocumentConverter(format_options=format_opts)

        conv_res = converter.convert(effective_pdf)
        if not conv_res or not conv_res.document:
            return []

        doc_item = conv_res.document
        tables = getattr(doc_item, "tables", [])
        if not tables:
            return []

        for tbl_idx, tbl in enumerate(tables):
            if cancel_event is not None and cancel_event.is_set():
                break

            df = tbl.export_to_dataframe()
            if df is None or df.empty or len(df) < 1:
                continue

            # 导出官方 HTML 以完整保留 rowspan / colspan 跨行跨列结构
            if hasattr(tbl, "export_to_html"):
                try:
                    raw_html = tbl.export_to_html(doc_item)
                except Exception:
                    try:
                        raw_html = tbl.export_to_html()
                    except Exception:
                        raw_html = None
                if isinstance(raw_html, str):
                    df.attrs["raw_html"] = raw_html

            # 获取页码与边界框坐标并归一化坐标原点
            p_idx = 0
            bbox = None
            if hasattr(tbl, "prov") and tbl.prov:
                first_prov = tbl.prov[0]
                raw_page_no = getattr(first_prov, "page_no", 1)  # 1-indexed
                sub_page_idx = max(0, raw_page_no - 1)
                if page_index_map is not None:
                    p_idx = page_index_map.get(sub_page_idx, sub_page_idx)
                else:
                    p_idx = sub_page_idx

                raw_bbox = getattr(first_prov, "bbox", None)
                if raw_bbox:
                    l = getattr(raw_bbox, "l", 0.0)
                    t = getattr(raw_bbox, "t", 0.0)
                    r = getattr(raw_bbox, "r", 0.0)
                    b = getattr(raw_bbox, "b", 0.0)
                    coord_origin = getattr(raw_bbox, "coord_origin", "BOTTOMLEFT")
                    page_h = 792.0
                    try:
                        with fitz.open(pdf_path) as orig_doc:
                            if 0 <= p_idx < len(orig_doc):
                                page_h = orig_doc[p_idx].rect.height
                    except Exception:
                        pass

                    coord_origin_name = getattr(coord_origin, "name", str(coord_origin))
                    if "BOTTOMLEFT" in str(coord_origin).upper() or str(coord_origin_name).upper() == "BOTTOMLEFT":
                        y0 = min(page_h - t, page_h - b)
                        y1 = max(page_h - t, page_h - b)
                        bbox = [float(l), float(y0), float(r), float(y1)]
                    else:
                        bbox = [float(l), float(min(t, b)), float(r), float(max(t, b))]

            # 获取表标题/说明
            caption = ""
            try:
                caption = tbl.caption_text(doc_item) or ""
            except Exception:
                caption = ""

            df.attrs["extractor"] = "docling_tableformer"
            df.attrs["page_idx"] = p_idx
            if caption:
                df.attrs["table_title"] = caption

            results.append({
                "df": df,
                "page_idx": p_idx,
                "table_idx": tbl_idx,
                "bbox": bbox,
            })

    except Exception as e:
        logger.warning(f"[Docling] Docling 提取异常: {e}")
    finally:
        if temp_slice and os.path.exists(temp_slice):
            try:
                os.remove(temp_slice)
            except Exception:
                pass

    if results:
        print(f"[Docling] TableFormer 离线备用成功提取到 {len(results)} 个表格")
    return results


def call_docling_table_extractor(
    pdf_path: str,
    pages: Optional[List[int]] = None,
    config: Optional[Dict[str, Any]] = None,
    cancel_event=None,
) -> str:
    """
    通过 Docling 离线提取表格并将结果导出为 Markdown 字符串，
    供上游直接对接 parse_structured_vlm_content 统一后处理管线。
    """
    tbls = extract_via_docling(pdf_path, pages=pages, config=config, cancel_event=cancel_event)
    if not tbls:
        return ""

    md_blocks = []
    for idx, item in enumerate(tbls):
        df = item.get("df")
        if df is None or df.empty:
            continue
        p = item.get("page_idx", 0)
        title = df.attrs.get("table_title", f"Table {idx+1}")
        raw_html = df.attrs.get("raw_html")
        if raw_html and isinstance(raw_html, str) and "<table" in raw_html.lower():
            table_md = raw_html
        else:
            try:
                table_md = df.to_markdown(index=False)
            except Exception:
                cols = [str(c) for c in df.columns]
                lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
                for _, r_data in df.iterrows():
                    lines.append("| " + " | ".join(str(v).replace("\n", " ").strip() for v in r_data.values) + " |")
                table_md = "\n".join(lines)
        block = f"### Table {idx+1} (Page {p+1}): {title}\n\n{table_md}\n"
        md_blocks.append(block)

    return "\n\n".join(md_blocks)


# ===========================================================================
# OCR-Hub 多引擎聚合网关与可插拔后端注册表 (Backend Registry)
# ===========================================================================

class OCRBackend:
    """抽象 OCR 提取后端基类。"""
    name: str = "base"

    def get_capabilities(self) -> Dict[str, bool]:
        return {
            "tables": True,
            "formulas": False,
            "autorotate": False,
            "crops": False,
        }

    def probe_health(self) -> Dict[str, Any]:
        return {"status": "healthy", "latency_ms": 0.0, "message": "OK"}

    def extract(self, file_path: str, **kwargs) -> Any:
        raise NotImplementedError


class PaddleOCRVLBackend(OCRBackend):
    name = "paddleocr_vl"

    def get_capabilities(self) -> Dict[str, bool]:
        return {"tables": True, "formulas": True, "autorotate": True, "crops": True}

    def probe_health(self) -> Dict[str, Any]:
        token = os.getenv("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN", "")
        if not token:
            try:
                cfg = load_config()
                token = cfg.get("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN", "")
            except Exception:
                pass
        healthy = bool(token)
        return {
            "status": "healthy" if healthy else "unauthenticated",
            "message": "Token configured" if healthy else "Missing AIStudio token"
        }

    def extract(self, file_path: str, **kwargs) -> str:
        return call_paddleocr_vl_online_api(file_path, **kwargs)


class MinerUBackend(OCRBackend):
    name = "mineru"

    def get_capabilities(self) -> Dict[str, bool]:
        return {
            "tables": True,
            "formulas": True,
            "autorotate": True,
            "crops": False,
            "dense_tables": True,
            "wireless_tables": True,
        }

    def probe_health(self) -> Dict[str, Any]:
        try:
            cfg = load_config()
            key = os.getenv("MINERU_API_KEY", "") or cfg.get("MINERU_API_KEY", "")
        except Exception:
            key = os.getenv("MINERU_API_KEY", "")
        healthy = bool(key)
        return {
            "status": "healthy" if healthy else "agent_fallback",
            "message": "API key configured" if healthy else "Using Agent API fallback (<=10MB, <=20 pages)"
        }

    def extract(self, file_path: str, **kwargs) -> Any:
        if "page_ranges" not in kwargs and "pages" in kwargs:
            kwargs["page_ranges"] = kwargs.pop("pages")
        structured = kwargs.pop("structured", False)
        if "config" not in kwargs:
            kwargs["config"] = getattr(self, "config", None) or load_config()
        if structured:
            return call_mineru_structured_api(file_path, **kwargs)
        return call_mineru_api(file_path, **kwargs)


class TeleOCRClient:
    """TeleOCR (vLLM / OpenAI-compatible Vision API) 客户端。"""

    def __init__(self, api_base: str = "", model: str = "TeleOCR", api_key: str = ""):
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
        self.api_base = (api_base or os.getenv("TELEOCR_API_BASE", "") or cfg.get("TELEOCR_API_BASE", "")).rstrip("/")
        self.model = model or os.getenv("TELEOCR_MODEL", "") or cfg.get("TELEOCR_MODEL", "TeleOCR")
        self.api_key = api_key or os.getenv("TELEOCR_API_KEY", "") or cfg.get("TELEOCR_API_KEY", "")

    def is_available(self) -> bool:
        return bool(self.api_base)

    def extract_from_image(self, img_bytes: bytes) -> str:
        if not self.is_available() or not img_bytes:
            return ""
        import base64
        b64 = base64.b64encode(img_bytes).decode("utf-8")
        url = f"{self.api_base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all tables from this image as Markdown tables."},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                    ]
                }
            ],
            "temperature": 0.0,
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.debug(f"[TeleOCR] 请求异常: {e}")
        return ""


class TeleOCRBackend(OCRBackend):
    name = "teleocr"

    def __init__(self):
        self.client = TeleOCRClient()

    def get_capabilities(self) -> Dict[str, bool]:
        return {"tables": True, "formulas": False, "autorotate": True, "crops": True}

    def probe_health(self) -> Dict[str, Any]:
        healthy = self.client.is_available()
        return {
            "status": "healthy" if healthy else "offline",
            "message": "Endpoint configured" if healthy else "TELEOCR_API_BASE not set"
        }

    def extract(self, file_path: str, **kwargs) -> str:
        if not self.client.is_available() or not os.path.exists(file_path):
            return ""
        low = file_path.lower()
        if low.endswith((".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp")):
            try:
                with open(file_path, "rb") as f:
                    return self.client.extract_from_image(f.read())
            except Exception as e:
                logger.debug(f"[TeleOCR] 图像提取异常: {e}")
                return ""
        if low.endswith(".pdf") and fitz is not None:
            pages = kwargs.get("pages")
            cancel_event = kwargs.get("cancel_event")
            try:
                doc = fitz.open(file_path)
                target_pages = pages if pages is not None else range(len(doc))
                md_blocks = []
                for p_idx in target_pages:
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    if 0 <= p_idx < len(doc):
                        pix = doc[p_idx].get_pixmap(dpi=200)
                        img_bytes = pix.tobytes("png")
                        page_md = self.client.extract_from_image(img_bytes)
                        if page_md:
                            md_blocks.append(f"### Page {p_idx+1}\n\n{page_md}")
                doc.close()
                return "\n\n".join(md_blocks)
            except Exception as e_pdf:
                logger.debug(f"[TeleOCR] PDF 提取异常: {e_pdf}")
                return ""
        return ""


class DoclingBackend(OCRBackend):
    name = "docling"

    def get_capabilities(self) -> Dict[str, bool]:
        return {"tables": True, "formulas": False, "autorotate": False, "crops": False}

    def probe_health(self) -> Dict[str, Any]:
        avail = is_docling_available()
        return {
            "status": "healthy" if avail else "uninstalled",
            "message": "docling installed" if avail else "docling library not installed"
        }

    def extract(self, file_path: str, **kwargs) -> str:
        return call_docling_table_extractor(file_path, **kwargs)


class RapidTableBackend(OCRBackend):
    name = "rapid_table"

    def get_capabilities(self) -> Dict[str, bool]:
        return {"tables": True, "formulas": False, "autorotate": False, "crops": True, "offline": True}

    def probe_health(self) -> Dict[str, Any]:
        try:
            from .rapid_table_extractor import is_rapid_table_available
        except ImportError:
            try:
                from rapid_table_extractor import is_rapid_table_available
            except ImportError:
                is_rapid_table_available = lambda: False
        avail = is_rapid_table_available()
        return {
            "status": "healthy" if avail else "uninstalled",
            "message": "rapid_table available" if avail else "rapid_table not installed"
        }

    def extract(self, file_path: str, **kwargs) -> Any:
        try:
            from .rapid_table_extractor import extract_via_rapid_table
        except ImportError:
            try:
                from rapid_table_extractor import extract_via_rapid_table
            except ImportError:
                extract_via_rapid_table = lambda *args, **kwargs: []
        return extract_via_rapid_table(file_path, **kwargs)


class OCRBackendRegistry:
    """OCR-Hub 引擎聚合注册表与能力探测总控。"""

    def __init__(self):
        self.backends: Dict[str, OCRBackend] = {
            "paddleocr_vl": PaddleOCRVLBackend(),
            "mineru": MinerUBackend(),
            "teleocr": TeleOCRBackend(),
            "docling": DoclingBackend(),
            "rapid_table": RapidTableBackend(),
        }

    def register(self, backend: OCRBackend):
        self.backends[backend.name] = backend

    def get_backend(self, name: str) -> Optional[OCRBackend]:
        return self.backends.get(name)

    def get_capabilities(self, name: Optional[str] = None) -> Dict[str, Any]:
        """动态发现并返回 capabilities 清单。"""
        if name:
            b = self.backends.get(name)
            return b.get_capabilities() if b else {}
        return {k: v.get_capabilities() for k, v in self.backends.items()}

    def probe_health(self, name: Optional[str] = None) -> Dict[str, Any]:
        """对各引擎执行健康探测。"""
        if name:
            b = self.backends.get(name)
            return b.probe_health() if b else {"status": "unknown"}
        return {k: v.probe_health() for k, v in self.backends.items()}

    def dispatch(self, capability: str = "tables", prefer: Optional[str] = None) -> Optional[OCRBackend]:
        """根据需求能力动态调度可用且健康的最佳后端。"""
        if prefer and prefer in self.backends:
            b = self.backends[prefer]
            h = b.probe_health()
            if h.get("status") == "healthy" and b.get_capabilities().get(capability, False):
                return b

        for name, b in self.backends.items():
            if b.get_capabilities().get(capability, False):
                h = b.probe_health()
                if h.get("status") == "healthy":
                    return b

        return None


# 全局注册表单例
_ocr_registry = None

def get_ocr_registry() -> OCRBackendRegistry:
    global _ocr_registry
    if _ocr_registry is None:
        _ocr_registry = OCRBackendRegistry()
    return _ocr_registry

