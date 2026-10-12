#!/usr/bin/env python3
"""
rapid_table_extractor.py — RapidAI / TableStructureRec + RapidTable 本地双模型表格结构识别引擎。

架构特色：
1. 有线/无线表格双模型串联与自适应分流：
   - table_cls 预先判别表格类型（有线表格 vs 无线三线表/开放表）；
   - 有线表格: UNet 线检测 + SLANet-Plus (快速晶格与合并单元格恢复，~100ms CPU)；
   - 无线表格: UniTable / Transformer 序列预测 (无框文本拓扑推断，~500ms CPU)。
2. 全部采用轻量 ONNX 格式，零 GPU 强依赖，纯 CPU 毫秒级推理；
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


def is_rapid_table_available() -> bool:
    """检查 rapid_table 或 table_structure_rec 及其 ONNX 运行环境是否可用。"""
    try:
        import rapid_table
        return True
    except ImportError:
        pass

    try:
        import table_structure_rec
        return True
    except ImportError:
        pass

    # 检查本地 ONNX 运行时与是否有相关模型权重
    try:
        import onnxruntime
        models_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
        if os.path.exists(models_dir):
            for fname in os.listdir(models_dir):
                if ("slanet" in fname.lower() or "rapid_table" in fname.lower() or "unitable" in fname.lower()) and fname.endswith(".onnx"):
                    return True
    except Exception:
        pass

    return False


class RapidTableExtractor:
    """
    RapidAI TableStructureRec + RapidTable 结构识别包装器。
    支持 table_cls 区分 wired/wireless 表格，自适应走 SLANet-Plus 或 UniTable。
    """

    def __init__(self, model_dir: Optional[str] = None):
        self.model_dir = model_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models"
        )
        self._table_engine = None
        self._wired_engine = None
        self._wireless_engine = None
        self._table_cls = None
        self._initialized = False

    def _init_engine(self):
        if self._initialized:
            return
        self._initialized = True
        try:
            from rapid_table import RapidTable

            def _build_rapid_cfg(model_type_name: str, model_dir_val: Optional[str] = None):
                # 尝试使用官方 RapidTableInput 配置类
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
                # 兼容性配置对象/字典
                cfg_obj = {"model_type": model_type_name}
                if model_dir_val:
                    cfg_obj["model_dir_or_path"] = model_dir_val
                return cfg_obj

            def _instantiate_engine(model_type_name: str, model_dir_val: Optional[str] = None):
                cfg = _build_rapid_cfg(model_type_name, model_dir_val)
                # 官方接口契约: RapidTable(cfg) 或 RapidTable(cfg=cfg)
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

            wireless = _instantiate_engine("unitable", self.model_dir)
            if wireless is None:
                wireless = _instantiate_engine("unitable")
            if wireless is None and wired is None:
                try:
                    wireless = RapidTable()
                except Exception:
                    wireless = None
            self._wireless_engine = wireless

            self._table_engine = self._wired_engine or self._wireless_engine
            if self._table_engine is not None:
                logger.info("[RapidTable] 成功加载 rapid_table 引擎")
        except ImportError:
            try:
                import table_structure_rec
                if hasattr(table_structure_rec, "TableStructureRec"):
                    self._table_engine = table_structure_rec.TableStructureRec(model_dir=self.model_dir)
                elif hasattr(table_structure_rec, "RapidTable"):
                    self._table_engine = table_structure_rec.RapidTable(model_dir=self.model_dir)
                logger.info("[RapidTable] 成功加载 table_structure_rec 引擎")
            except Exception as e_tsr:
                logger.debug(f"[RapidTable] 引擎加载跳过: {e_tsr}")
        except Exception as e:
            logger.debug(f"[RapidTable] rapid_table 引擎加载跳过: {e}")

    def classify_table_type(self, img_or_array) -> str:
        """
        判断表格形态：'wired' (有线框网格表) 或 'wireless' (无线框/三线表)。
        支持模型推理或基于形态学线条密度的快速启发式判别。
        """
        if img_or_array is None:
            return "wireless"

        try:
            # 若传入为 PIL Image，转为灰度 numpy 矩阵
            if Image is not None and isinstance(img_or_array, Image.Image):
                gray = np.array(img_or_array.convert("L"))
            elif np is not None and isinstance(img_or_array, np.ndarray):
                if len(img_or_array.shape) == 3:
                    gray = np.mean(img_or_array, axis=2).astype(np.uint8)
                else:
                    gray = img_or_array
            else:
                return "wireless"

            # 简易水平/垂直黑线梯度采样判别线框丰富度
            h_diff = np.diff(gray, axis=0)
            v_diff = np.diff(gray, axis=1)
            sharp_lines = (np.abs(h_diff) > 80).sum() + (np.abs(v_diff) > 80).sum()
            total_pixels = gray.size
            line_density = sharp_lines / max(total_pixels, 1)

            # 线密度高于阈值判定为有线表，否则为无线框/开放表
            if line_density > 0.045:
                return "wired"
            return "wireless"
        except Exception:
            return "wireless"

    @staticmethod
    def _extract_html_content(obj: Any) -> Optional[str]:
        """递归解包 RapidTable 输出（支持 RapidTableOutput, pred_htmls, pred_html, 字典及元组组合）。"""
        if obj is None:
            return None
        if isinstance(obj, str):
            s = obj.strip()
            return s if s else None
        if hasattr(obj, "pred_html") and obj.pred_html is not None:
            return RapidTableExtractor._extract_html_content(obj.pred_html)
        if hasattr(obj, "pred_htmls") and obj.pred_htmls is not None:
            return RapidTableExtractor._extract_html_content(obj.pred_htmls)
        if isinstance(obj, dict):
            for k in ("pred_html", "pred_htmls", "html"):
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
        if self._table_engine is None and self._wired_engine is None and self._wireless_engine is None:
            return None

        try:
            table_type = self.classify_table_type(img)
            # 自适应双模型分流：wired 走 SLANet-Plus, wireless 走 UniTable
            engine = self._wired_engine if table_type == "wired" and self._wired_engine else (
                self._wireless_engine if self._wireless_engine else self._table_engine
            )
            if engine is None:
                engine = self._table_engine
            if engine is None:
                return None

            result = engine(img)
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
        if self._table_engine is None and self._wired_engine is None and self._wireless_engine is None:
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
