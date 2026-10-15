#!/usr/bin/env python3
"""
rapid_table_extractor.py — RapidAI / TableStructureRec + RapidTable 本地双模型表格结构识别引擎。

架构特色：
1. 有线/无线表格双模型串联与自适应分流：
   - 支持 TableStructureRec (TableCls 判别有线 wired vs 无线 lineless)；
   - 有线表格: WiredTableRec 或 RapidTable (SLANet-Plus, 轻量 ONNX CPU 毫秒级推理)；
   - 无线表格: LinelessTableRec 或 UniTable (UniTable 依赖 PyTorch，作为独立可选依赖；未安装 PyTorch 时平滑回退至 SLANet-Plus 或启发式处理)。
2. 明确区分 ONNX 与 PyTorch 依赖：
   - SLANet-Plus 采用轻量 ONNX 格式，零 GPU 强依赖，纯 CPU 毫秒级推理；
   - UniTable 为 PyTorch 深度序列模型，需独立安装 PyTorch 环境；
   - RapidTable 2/3 依赖 rapidocr>=3.0.0，需正确配套安装。
3. 输出完整 DOM 与合并单元格信息 (rowspan/colspan)；
4. 既可作为 8 引擎多方投票 (three_way_vote) 的候选方接入，亦可在云端 OCR 接管前作为本地快速试跑层。
"""

import os
import sys
import logging
from typing import List, Dict, Any, Optional, Tuple, Union

try:
    import numpy as np
except ImportError:
    np = None

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

try:
    from PIL import Image
except ImportError:
    Image = None

logger = logging.getLogger("rapid_table")


_RAPID_TABLE_AVAILABLE: Optional[bool] = None


def is_rapid_table_available() -> bool:
    """检查 rapid_table 或 table_structure_rec 及其运行环境是否真实可用 (检查真实初始化状态并缓存)。"""
    global _RAPID_TABLE_AVAILABLE
    if _RAPID_TABLE_AVAILABLE is not None:
        return _RAPID_TABLE_AVAILABLE

    try:
        import rapid_table
    except (ImportError, Exception):
        rapid_table = None

    try:
        import table_structure_rec
    except (ImportError, Exception):
        table_structure_rec = None

    if rapid_table is None and table_structure_rec is None:
        _RAPID_TABLE_AVAILABLE = False
        return False

    try:
        ext = RapidTableExtractor()
        _RAPID_TABLE_AVAILABLE = ext.is_ready()
    except Exception:
        _RAPID_TABLE_AVAILABLE = False

    return _RAPID_TABLE_AVAILABLE


class RapidTableExtractor:
    """
    RapidAI TableStructureRec + RapidTable 结构识别包装器。
    支持 TableCls / 启发式区分 wired/wireless 表格，自适应走 SLANet-Plus 或 UniTable。
    """

    def __init__(self, model_dir: Optional[str] = None):
        self.model_dir = model_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models"
        )
        self._table_engine = None
        self._wired_engine = None
        self._wireless_engine = None
        self._wired_tsr_engine = None
        self._lineless_tsr_engine = None
        self._table_cls = None
        self._initialized = False

    def is_ready(self) -> bool:
        """检查引擎是否真正完成初始化并具备可用识别模型。"""
        self._init_engine()
        return any(x is not None for x in [
            self._table_engine, self._wired_engine, self._wireless_engine,
            self._wired_tsr_engine, self._lineless_tsr_engine
        ])

    def _init_engine(self):
        if self._initialized:
            return
        self._initialized = True

        # 1. 尝试加载官方 TableStructureRec 模块 (TableCls, WiredTableRec, LinelessTableRec)
        try:
            import table_structure_rec
            if hasattr(table_structure_rec, "TableCls"):
                try:
                    self._table_cls = table_structure_rec.TableCls()
                except Exception as e_cls:
                    logger.debug(f"[TableStructureRec] TableCls 初始化跳过: {e_cls}")
            if hasattr(table_structure_rec, "WiredTableRec"):
                try:
                    self._wired_tsr_engine = table_structure_rec.WiredTableRec()
                except Exception as e_w:
                    logger.debug(f"[TableStructureRec] WiredTableRec 初始化跳过: {e_w}")
            if hasattr(table_structure_rec, "LinelessTableRec"):
                try:
                    self._lineless_tsr_engine = table_structure_rec.LinelessTableRec()
                except Exception as e_l:
                    logger.debug(f"[TableStructureRec] LinelessTableRec 初始化跳过: {e_l}")
            if self._wired_tsr_engine or self._lineless_tsr_engine:
                logger.info("[TableStructureRec] 成功加载官方 TableStructureRec 组件")
        except ImportError:
            pass
        except Exception as e_tsr:
            logger.debug(f"[TableStructureRec] 引擎加载跳过: {e_tsr}")

        # 2. 尝试加载 RapidTable 模块
        try:
            from rapid_table import RapidTable

            def _build_rapid_cfg(model_type_name: str, model_dir_val: Optional[str] = None):
                try:
                    from rapid_table import RapidTableInput, ModelType
                    enum_name = model_type_name.upper().replace("_", "")
                    m_type = getattr(ModelType, enum_name, model_type_name)
                    kwargs = {"model_type": m_type}
                    if model_dir_val:
                        kwargs["model_dir_or_path"] = model_dir_val
                    try:
                        return RapidTableInput(**kwargs)
                    except TypeError:
                        if "model_dir_or_path" in kwargs:
                            kwargs["model_path"] = kwargs.pop("model_dir_or_path")
                        return RapidTableInput(**kwargs)
                except Exception:
                    pass
                cfg_obj = {"model_type": model_type_name}
                if model_dir_val:
                    cfg_obj["model_dir_or_path"] = model_dir_val
                return cfg_obj

            def _instantiate_engine(model_type_name: str, model_dir_val: Optional[str] = None):
                cfg = _build_rapid_cfg(model_type_name, model_dir_val)
                for init_attempt in [
                    lambda: RapidTable(cfg),
                    lambda: RapidTable(cfg=cfg),
                    lambda: RapidTable(model_type=model_type_name, model_dir=model_dir_val) if model_dir_val else RapidTable(model_type=model_type_name),
                    lambda: RapidTable(model_type=model_type_name),
                ]:
                    try:
                        inst = init_attempt()
                        if inst is not None:
                            return inst
                    except TypeError:
                        continue
                    except Exception:
                        continue
                return None

            wired = _instantiate_engine("slanet_plus", self.model_dir)
            if wired is None:
                wired = _instantiate_engine("slanet_plus")
            if wired is None:
                try:
                    wired = RapidTable()
                except Exception:
                    wired = None
            self._wired_engine = wired

            # UniTable 使用 PyTorch，作为独立可选依赖；
            # 仅当已在环境引入 torch 或无任何可用引擎时进行尝试，避免在轻量 ONNX 推理或沙箱 mock 中意外引入重度依赖
            wireless = None
            if "torch" in sys.modules or (wired is None and self._wired_tsr_engine is None and self._lineless_tsr_engine is None):
                try:
                    import torch
                    wireless = _instantiate_engine("unitable", self.model_dir)
                    if wireless is None:
                        wireless = _instantiate_engine("unitable")
                except (ImportError, Exception):
                    logger.debug("[RapidTable] 未安装 PyTorch 或 PyTorch 无法加载，跳过 UniTable 权重加载 (作为独立可选依赖)")

            self._wireless_engine = wireless
            self._table_engine = self._wired_engine or self._wireless_engine or self._wired_tsr_engine or self._lineless_tsr_engine
            if self._table_engine is not None:
                logger.info("[RapidTable] 成功加载 rapid_table 引擎")
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[RapidTable] rapid_table 引擎加载跳过: {e}")

    def classify_table_type(self, img_or_array) -> str:
        """
        判断表格形态：'wired' (有线框网格表) 或 'wireless' (无线框/三线表)。
        优先使用官方 TableCls 模型推理；若不可用则平滑回退为形态学线条密度判别。
        """
        if img_or_array is None:
            return "wireless"

        # 1. 优先调用 TableStructureRec 官方 TableCls 模型推理
        if self._table_cls is not None:
            try:
                cls_res = self._table_cls(img_or_array)
                val_str = ""
                if isinstance(cls_res, str):
                    val_str = cls_res.lower()
                elif isinstance(cls_res, (list, tuple)) and len(cls_res) > 0:
                    val_str = str(cls_res[0]).lower()

                if val_str:
                    if "lineless" in val_str or "wireless" in val_str:
                        return "wireless"
                    elif "wired" in val_str:
                        return "wired"
            except Exception as e_cls:
                logger.debug(f"[TableStructureRec] TableCls 推理异常，回退启发式判别: {e_cls}")

        # 2. 启发式回退：基于形态学线条密度的快速判别
        try:
            if Image is not None and isinstance(img_or_array, Image.Image):
                gray = np.array(img_or_array.convert("L"))
            elif np is not None and isinstance(img_or_array, np.ndarray):
                if len(img_or_array.shape) == 3:
                    gray = np.mean(img_or_array, axis=2).astype(np.uint8)
                else:
                    gray = img_or_array
            else:
                return "wireless"

            h_diff = np.diff(gray, axis=0)
            v_diff = np.diff(gray, axis=1)
            sharp_lines = (np.abs(h_diff) > 80).sum() + (np.abs(v_diff) > 80).sum()
            total_pixels = gray.size
            line_density = sharp_lines / max(total_pixels, 1)

            if line_density > 0.045:
                return "wired"
            return "wireless"
        except Exception:
            return "wireless"

    @staticmethod
    def _extract_html_content(obj: Any) -> Optional[str]:
        """递归解包 RapidTable / TableStructureRec 输出（支持 RapidTableOutput, pred_htmls, pred_html, (html, elapse), 字典及元组组合）。"""
        if obj is None:
            return None
        if isinstance(obj, str):
            s = obj.strip()
            return s if s else None
        if isinstance(obj, (list, tuple)) and len(obj) >= 1 and isinstance(obj[0], str) and ("<table" in obj[0].lower() or "<html" in obj[0].lower()):
            return obj[0].strip()
        if hasattr(obj, "pred_html") and obj.pred_html is not None:
            return RapidTableExtractor._extract_html_content(obj.pred_html)
        if hasattr(obj, "pred_htmls") and obj.pred_htmls is not None:
            return RapidTableExtractor._extract_html_content(obj.pred_htmls)
        if isinstance(obj, dict):
            for k in ("pred_html", "pred_htmls", "html", "table_str", "table_html"):
                if k in obj and obj[k] is not None:
                    val = RapidTableExtractor._extract_html_content(obj[k])
                    if val:
                        return val
            return None
        if isinstance(obj, (list, tuple)):
            extracted = [RapidTableExtractor._extract_html_content(item) for item in obj]
            valid = [s for s in extracted if s and isinstance(s, str) and s.strip()]
            if not valid:
                return None
            return valid[0] if len(valid) == 1 else "\n".join(valid)
        return None

    def parse_html_to_dataframe(self, html_input: Union[str, List[str], Any]) -> Optional[pd.DataFrame]:
        """从 RapidTable 输出的 HTML <table> 字符串、列表或输出对象重建带有合并单元格展开的 DataFrame。"""
        if html_input is None or pd is None:
            return None

        html_str = self._extract_html_content(html_input)
        if not html_str or "<table" not in html_str.lower():
            return None

        try:
            import io
            # 1. 若包含 <th> 标签，默认解析 (pandas 自动将 <th> 识别为列名)
            # 2. 若全为 <td>，尝试 header=0 将首行作为表头；若单行或导致空表，回退无指定 header 解析
            dfs = None
            if "<th" in html_str.lower():
                dfs = pd.read_html(io.StringIO(html_str))
            else:
                try:
                    dfs = pd.read_html(io.StringIO(html_str), header=0)
                    if dfs and dfs[0].empty:
                        dfs = pd.read_html(io.StringIO(html_str))
                except Exception:
                    dfs = pd.read_html(io.StringIO(html_str))

            if dfs:
                df = dfs[0]
                if not df.empty:
                    df.attrs["raw_html"] = html_str
                    df.attrs["extractor"] = "rapid_table"
                    return df
        except Exception as e:
            logger.debug(f"[RapidTable] read_html 解析异常: {e}")

        # 手工正则提取 <tr> / <td> 回退
        try:
            import re
            rows = []
            row_matches = re.findall(r'<tr[^>]*>(.*?)</tr>', html_str, re.DOTALL | re.IGNORECASE)
            for r_html in row_matches:
                cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', r_html, re.DOTALL | re.IGNORECASE)
                cleaned_cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                if any(cleaned_cells):
                    rows.append(cleaned_cells)
            if rows:
                max_cols = max(len(r) for r in rows)
                padded = [r + [""] * (max_cols - len(r)) for r in rows]
                if len(padded) > 1:
                    header = padded[0]
                    df = pd.DataFrame(padded[1:], columns=header)
                else:
                    df = pd.DataFrame(padded)
                df.attrs["raw_html"] = html_str
                df.attrs["extractor"] = "rapid_table"
                return df
        except Exception as e2:
            logger.debug(f"[RapidTable] 正则解析回退异常: {e2}")

        return None

    def extract_table_from_image(self, img) -> Optional[pd.DataFrame]:
        """识别单个表格图像切片，返回结构化 DataFrame。"""
        self._init_engine()
        if not self.is_ready():
            return None

        try:
            table_type = self.classify_table_type(img)
            result = None
            if table_type == "wired":
                if self._wired_tsr_engine is not None:
                    res_tsr = self._wired_tsr_engine(img)
                    result = res_tsr[0] if isinstance(res_tsr, (list, tuple)) else res_tsr
                elif self._wired_engine is not None:
                    result = self._wired_engine(img)
                elif self._table_engine is not None:
                    result = self._table_engine(img)
            else:
                if self._lineless_tsr_engine is not None:
                    res_tsr = self._lineless_tsr_engine(img)
                    result = res_tsr[0] if isinstance(res_tsr, (list, tuple)) else res_tsr
                elif self._wireless_engine is not None:
                    result = self._wireless_engine(img)
                elif self._wired_engine is not None:
                    result = self._wired_engine(img)
                elif self._table_engine is not None:
                    result = self._table_engine(img)

            if result is None:
                return None

            table_html = self._extract_html_content(result)
            if not table_html:
                return None

            df = self.parse_html_to_dataframe(table_html)
            if df is not None:
                df.attrs["table_type"] = table_type
                df.attrs["extractor"] = "rapid_table"
                df.attrs["raw_html"] = table_html
            return df
        except Exception as e:
            logger.warning(f"[RapidTable] 图像识别异常: {e}")
            return None

    def extract_from_pdf(
        self,
        pdf_path: str,
        pages: Optional[List[int]] = None,
        crop_boxes: Optional[Dict[int, List[List[float]]]] = None,
        dpi: int = 200,
    ) -> List[Dict[str, Any]]:
        """从 PDF 中抽取表格，支持结合给定版面检测框或自动定位表格区域。"""
        if not is_rapid_table_available() or fitz is None:
            return []

        self._init_engine()
        if not self.is_ready():
            return []

        results = []
        try:
            doc = fitz.open(pdf_path)
            target_pages = pages if pages is not None else range(len(doc))

            for page_idx in target_pages:
                if page_idx >= len(doc):
                    continue
                page = doc[page_idx]

                # 若指定了切片 crop_boxes，逐框识别；若未指定，优先尝试通过页面表格检测定位紧致切片
                bboxes = (crop_boxes or {}).get(page_idx, [])
                if not bboxes and hasattr(page, "find_tables"):
                    try:
                        tabs = page.find_tables()
                        if tabs and tabs.tables:
                            bboxes = [list(tab.bbox) for tab in tabs.tables if tab.bbox]
                    except Exception:
                        bboxes = []

                if bboxes:
                    for t_idx, bbox in enumerate(bboxes):
                        pad = 4.0
                        rect = fitz.Rect(
                            max(0.0, bbox[0] - pad),
                            max(0.0, bbox[1] - pad),
                            min(page.rect.width, bbox[2] + pad),
                            min(page.rect.height, bbox[3] + pad)
                        )
                        pix = page.get_pixmap(dpi=dpi, clip=rect)
                        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                        df = self.extract_table_from_image(img)
                        if df is not None and not df.empty:
                            df.attrs["page_idx"] = page_idx
                            df.attrs["table_idx"] = t_idx
                            df.attrs["bbox"] = bbox
                            results.append({
                                "df": df,
                                "page_idx": page_idx,
                                "table_idx": t_idx,
                                "bbox": bbox,
                                "extractor": "rapid_table",
                                "source": "rapid_table",
                            })
                else:
                    # 整页图像降级处理 (仅当无法探测子框时)
                    pix = page.get_pixmap(dpi=dpi)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    df = self.extract_table_from_image(img)
                    if df is not None and not df.empty:
                        df.attrs["page_idx"] = page_idx
                        df.attrs["table_idx"] = 0
                        results.append({
                            "df": df,
                            "page_idx": page_idx,
                            "table_idx": 0,
                            "bbox": [0.0, 0.0, page.rect.width, page.rect.height],
                            "extractor": "rapid_table",
                            "source": "rapid_table",
                        })
            doc.close()
        except Exception as e:
            logger.warning(f"[RapidTable] PDF 解析异常: {e}")

        return results


def extract_via_rapid_table(
    pdf_path: str,
    pages: Optional[List[int]] = None,
    crop_boxes: Optional[Dict[int, List[List[float]]]] = None,
) -> List[Dict[str, Any]]:
    """外部统一调用接口：通过 RapidTable 提取表格并标准化返回。"""
    extractor = RapidTableExtractor()
    return extractor.extract_from_pdf(pdf_path, pages=pages, crop_boxes=crop_boxes)
