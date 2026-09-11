#!/usr/bin/env python3
"""
models.py — 统一学术表格领域模型 (Table Intermediate Representation, Table IR)。

解决全流程中在不同提取源（Online HTML、在线附表、PDF 本地、OCR/VLM）之间
传递裸 dict、DataFrame、元组导致的数据结构割裂、属性丢失和接口断裂问题。
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple, Union
import pandas as pd


@dataclass
class TableCell:
    """单个表格单元格模型。"""
    row: int
    col: int
    value: str
    rowspan: int = 1
    colspan: int = 1
    is_header: bool = False


@dataclass
class ExtractedTable:
    """
    统一学术表格实体对象 (Table IR)。
    包含数据本体、元数据（页码/坐标/标号/表题）、提取溯源及质检状态。
    """
    df: pd.DataFrame
    label: str = ""                         # 如 "Table 1", "表4.1"
    title: str = ""                         # 如 "Table 1. Main geological characteristics"
    page_idx: Optional[int] = None          # 0-indexed PDF 页码
    table_idx: int = 0                      # 该页内或文档内表序号
    bbox: Optional[Tuple[float, float, float, float]] = None # [x0, y0, x1, y1]
    source: str = "unknown"                 # "online_html", "online_supp", "pdf_native", "pdf_ocr", etc.
    confidence: float = 1.0                 # 置信度 (0.0 ~ 1.0)
    footnotes: List[str] = field(default_factory=list)
    raw_content: Optional[str] = None       # 原始 HTML / Markdown 文本（若有）
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 保证 DataFrame 与属性同步
        self.sync_attrs()

    def sync_attrs(self):
        """将标准属性同步至 underlying DataFrame.attrs，确保下游兼容性。"""
        if self.df is not None and hasattr(self.df, "attrs"):
            if self.label:
                self.df.attrs["label"] = self.label
            elif self.df.attrs.get("label"):
                self.label = str(self.df.attrs.get("label"))

            if self.title:
                self.df.attrs["table_title"] = self.title
            elif self.df.attrs.get("table_title"):
                self.title = str(self.df.attrs.get("table_title"))

            if self.page_idx is not None:
                self.df.attrs["page_idx"] = self.page_idx
            elif "page_idx" in self.df.attrs:
                self.page_idx = self.df.attrs["page_idx"]

            if self.source and self.source != "unknown":
                self.df.attrs["extractor"] = self.source
            elif self.df.attrs.get("extractor"):
                self.source = str(self.df.attrs.get("extractor"))

    def to_dataframe(self) -> pd.DataFrame:
        """返回带有同步 attrs 的 DataFrame。"""
        self.sync_attrs()
        return self.df

    @property
    def empty(self) -> bool:
        return self.df is None or self.df.empty

    @property
    def shape(self) -> Tuple[int, int]:
        return self.df.shape if self.df is not None else (0, 0)

    @property
    def attrs(self) -> Dict[str, Any]:
        self.sync_attrs()
        return self.df.attrs if self.df is not None else {}

    def __len__(self) -> int:
        return len(self.df) if self.df is not None else 0

    def __getitem__(self, key: str) -> Any:
        if key == "df":
            return self.df
        if key in ("label", "table_label"):
            return self.label
        if key in ("title", "table_title", "caption"):
            return self.title
        if key == "page_idx":
            return self.page_idx
        if key == "table_idx":
            return self.table_idx
        if key in ("bbox", "crop_bbox"):
            return self.bbox
        if key in ("source", "extractor"):
            return self.source
        if key in ("confidence", "score"):
            return self.confidence
        if key == "footnotes":
            return self.footnotes
        if key in ("raw_content", "markdown", "html"):
            return self.raw_content
        if key in self.metadata:
            return self.metadata[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def to_dict(self) -> Dict[str, Any]:
        """导出为兼容传统字典管道的格式。"""
        self.sync_attrs()
        return {
            "df": self.df,
            "label": self.label,
            "table_title": self.title,
            "caption": self.title,
            "page_idx": self.page_idx if self.page_idx is not None else 0,
            "table_idx": self.table_idx,
            "bbox": self.bbox,
            "confidence": self.confidence,
            "extractor": self.source,
            "source": self.source,
            "footnotes": self.footnotes,
        }

    @classmethod
    def from_any(cls, obj: Any, default_page: Optional[int] = None) -> Optional["ExtractedTable"]:
        """通用反序列化构造器：无缝转换 dict, pd.DataFrame 或 ExtractedTable 实例。"""
        if obj is None:
            return None
        if isinstance(obj, cls):
            return obj

        if isinstance(obj, pd.DataFrame):
            df = obj
            attrs = getattr(df, "attrs", {})
            return cls(
                df=df,
                label=str(attrs.get("label", "")),
                title=str(attrs.get("table_title", attrs.get("caption", ""))),
                page_idx=attrs.get("page_idx", default_page),
                source=str(attrs.get("extractor", attrs.get("source", "dataframe"))),
            )

        if isinstance(obj, dict):
            df = obj.get("df")
            if df is None:
                return None
            attrs = getattr(df, "attrs", {})
            label = obj.get("label") or attrs.get("label", "")
            title = obj.get("table_title") or obj.get("caption") or attrs.get("table_title", attrs.get("caption", ""))
            page_idx = obj.get("page_idx")
            if page_idx is None:
                page_idx = attrs.get("page_idx", default_page)
            bbox = obj.get("bbox") or obj.get("crop_bbox")
            source = obj.get("extractor") or obj.get("source") or attrs.get("extractor", "dict")
            confidence = float(obj.get("confidence") or obj.get("score") or 1.0)
            footnotes = obj.get("footnotes", [])

            return cls(
                df=df,
                label=str(label),
                title=str(title),
                page_idx=page_idx,
                table_idx=obj.get("table_idx", 0),
                bbox=bbox,
                source=str(source),
                confidence=confidence,
                footnotes=footnotes if isinstance(footnotes, list) else [],
                raw_content=obj.get("markdown") or obj.get("html"),
            )

        return None
