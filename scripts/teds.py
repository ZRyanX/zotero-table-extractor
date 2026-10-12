#!/usr/bin/env python3
"""
teds.py — IBM TEDS (Tree-Edit-Distance-based Similarity) 学术表格拓扑与内容相似度度量。

参考:
- Zhong et al., "Image-based table recognition: data, modeling, and evaluation" (ICDAR 2020)
- Zhang-Shasha (1989) 经典树编辑距离算法 (Tree Edit Distance)

功能:
1. 将 DataFrame 或 HTML table 转换为标准的 DOM 拓扑树 (table -> tr -> td/th)
2. 完整保留和比对 colspan、rowspan 属性
3. 支持 TEDS 结构拓扑相似度 (structure_only=True) 与 全文内容+拓扑相似度 (structure_only=False)
4. 自适应采样机制，确保大表格在毫秒级内完成高保真拓扑相似度评估
"""

import re
from typing import Optional, Union, List, Dict, Any
import pandas as pd


class TableNode:
    """表格 DOM 树节点模型。"""
    __slots__ = ('tag', 'text', 'attrs', 'children')

    def __init__(self, tag: str, text: str = "", attrs: Optional[Dict[str, Any]] = None, children: Optional[List['TableNode']] = None):
        self.tag = tag.lower().strip()
        self.text = (text or "").strip()
        self.attrs = attrs or {}
        self.children = children or []

    def __repr__(self):
        attrs_str = f" {self.attrs}" if self.attrs else ""
        text_str = f" text={self.text!r}" if self.text else ""
        return f"<{self.tag}{attrs_str}{text_str}>({len(self.children)} children)"


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """计算两字符串的归一化编辑相似度 (0.0 ~ 1.0)。"""
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    len1, len2 = len(s1), len(s2)
    max_len = max(len1, len2)
    if max_len == 0:
        return 1.0

    prev = list(range(len2 + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1] * (len2 + 1)
        for j, c2 in enumerate(s2):
            curr[j + 1] = min(
                prev[j + 1] + 1,
                curr[j] + 1,
                prev[j] + (0 if c1 == c2 else 1)
            )
        prev = curr
    dist = prev[len2]
    return max(0.0, 1.0 - dist / max_len)


def dataframe_to_table_tree(df: pd.DataFrame, max_rows: Optional[int] = None, max_cols: Optional[int] = None) -> TableNode:
    """
    将 pandas DataFrame 转换为结构化 TableNode 树。
    若指定 max_rows/max_cols，支持行与列的双向自适应采样；默认对全量行列进行完整树转换。
    """
    if df is None or df.empty:
        return TableNode("table")

    # 1. 列采样 (保留前部与后部主要列拓扑)
    sampled_df = df
    if max_cols and sampled_df.shape[1] > max_cols:
        head_c = max_cols // 2
        tail_c = max_cols - head_c
        sampled_df = pd.concat([sampled_df.iloc[:, :head_c], sampled_df.iloc[:, -tail_c:]], axis=1)

    # 2. 行采样
    if max_rows and len(sampled_df) > max_rows:
        head_n = max_rows // 2
        tail_n = max_rows - head_n
        sampled_df = pd.concat([sampled_df.iloc[:head_n], sampled_df.iloc[-tail_n:]])

    root = TableNode("table")
    
    # 3. Header (tr -> th)
    is_default_cols = (
        isinstance(df.columns, pd.RangeIndex) or
        all(c == i for i, c in enumerate(df.columns)) or
        all(str(c) == str(i) for i, c in enumerate(df.columns))
    )
    if not is_default_cols:
        th_nodes = []
        for col in sampled_df.columns:
            th_nodes.append(TableNode("th", text=str(col) if col is not None else ""))
        root.children.append(TableNode("tr", children=th_nodes))

    # 4. Body (tr -> td)
    for _, row in sampled_df.iterrows():
        td_nodes = []
        for val in row:
            v_str = "" if pd.isna(val) else str(val).strip()
            td_nodes.append(TableNode("td", text=v_str))
        root.children.append(TableNode("tr", children=td_nodes))

    return root


def parse_html_to_tree(html_content: str, max_rows: Optional[int] = None) -> TableNode:
    """
    将 HTML 中的 <table> 转换为结构化 TableNode 树。
    保留 rowspan, colspan 属性。默认无截断解析全量行。
    """
    if not html_content or "<table" not in html_content.lower():
        return TableNode("table")

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_content, "html.parser")
        tbl = soup.find("table")
        if not tbl:
            return TableNode("table")

        root = TableNode("table")
        rows = tbl.find_all("tr")
        if max_rows and len(rows) > max_rows:
            head_n = max_rows // 2
            tail_n = max_rows - head_n
            rows = rows[:head_n] + rows[-tail_n:]

        for tr in rows:
            tr_node = TableNode("tr")
            for cell in tr.find_all(["th", "td"]):
                tag = cell.name.lower()
                attrs = {}
                try:
                    if cell.get("colspan"):
                        attrs["colspan"] = int(cell["colspan"])
                except Exception:
                    pass
                try:
                    if cell.get("rowspan"):
                        attrs["rowspan"] = int(cell["rowspan"])
                except Exception:
                    pass
                text = cell.get_text(separator=" ", strip=True)
                tr_node.children.append(TableNode(tag, text=text, attrs=attrs))
            if tr_node.children:
                root.children.append(tr_node)
        return root
    except Exception:
        # Fallback to regex-based parser
        root = TableNode("table")
        tr_matches = re.findall(r'<tr[^>]*>(.*?)</tr>', html_content, re.DOTALL | re.IGNORECASE)
        if max_rows and len(tr_matches) > max_rows:
            head_n = max_rows // 2
            tail_n = max_rows - head_n
            tr_matches = tr_matches[:head_n] + tr_matches[-tail_n:]
        for tr_html in tr_matches:
            tr_node = TableNode("tr")
            cells = re.findall(r'<(td|th)([^>]*)>(.*?)</\1>', tr_html, re.DOTALL | re.IGNORECASE)
            for tag, attr_str, cell_content in cells:
                attrs = {}
                m_cs = re.search(r'colspan=["\']?(\d+)', attr_str, re.IGNORECASE)
                m_rs = re.search(r'rowspan=["\']?(\d+)', attr_str, re.IGNORECASE)
                if m_cs:
                    attrs["colspan"] = int(m_cs.group(1))
                if m_rs:
                    attrs["rowspan"] = int(m_rs.group(1))
                clean_text = re.sub(r'<[^>]+>', ' ', cell_content).strip()
                tr_node.children.append(TableNode(tag.lower(), text=clean_text, attrs=attrs))
            if tr_node.children:
                root.children.append(tr_node)
        return root


def _get_postorder_nodes(root: TableNode) -> List[TableNode]:
    """对树进行后序遍历，返回节点列表。"""
    nodes = []
    def traverse(curr: TableNode):
        for c in curr.children:
            traverse(c)
        nodes.append(curr)
    traverse(root)
    return nodes


def zhang_shasha_ted(t1: TableNode, t2: TableNode, structure_only: bool = False) -> float:
    """
    使用经典 Zhang-Shasha 树编辑距离算法计算两棵树的最小加权操作代价。
    """
    nodes1 = _get_postorder_nodes(t1)
    nodes2 = _get_postorder_nodes(t2)
    n1, n2 = len(nodes1), len(nodes2)

    if n1 == 0 and n2 == 0:
        return 0.0
    if n1 == 0:
        return float(n2)
    if n2 == 0:
        return float(n1)

    idx1 = {node: i + 1 for i, node in enumerate(nodes1)}
    idx2 = {node: i + 1 for i, node in enumerate(nodes2)}

    def leftmost(curr: TableNode, idx_map: Dict[TableNode, int]) -> int:
        c = curr
        while c.children:
            c = c.children[0]
        return idx_map[c]

    l1 = [0] + [leftmost(node, idx1) for node in nodes1]
    l2 = [0] + [leftmost(node, idx2) for node in nodes2]

    # 计算关键根集合 Key Roots
    kr1 = set()
    for i in range(1, n1 + 1):
        is_kr = True
        for j in range(i + 1, n1 + 1):
            if l1[j] == l1[i]:
                is_kr = False
                break
        if is_kr:
            kr1.add(i)

    kr2 = set()
    for i in range(1, n2 + 1):
        is_kr = True
        for j in range(i + 1, n2 + 1):
            if l2[j] == l2[i]:
                is_kr = False
                break
        if is_kr:
            kr2.add(i)

    def match_cost(u_node: TableNode, v_node: TableNode) -> float:
        # 对齐 IBM TEDS (PubTabNet metric.py CustomConfig.rename) 官方标准编辑代价:
        # 1. 标签不同 (如 tr 对比 td, th 对比 td 等) 直接返回代价 1.0
        if u_node.tag != v_node.tag:
            return 1.0

        if u_node.tag in ("th", "td"):
            # 2. 拓扑属性 (colspan, rowspan) 不匹配返回代价 1.0
            try:
                u_cs = int(u_node.attrs.get("colspan", 1) or 1)
            except Exception:
                u_cs = 1
            try:
                v_cs = int(v_node.attrs.get("colspan", 1) or 1)
            except Exception:
                v_cs = 1
            try:
                u_rs = int(u_node.attrs.get("rowspan", 1) or 1)
            except Exception:
                u_rs = 1
            try:
                v_rs = int(v_node.attrs.get("rowspan", 1) or 1)
            except Exception:
                v_rs = 1

            if u_cs != v_cs or u_rs != v_rs:
                return 1.0

            # 3. 单元格内容对齐 (区分大小写，归一化编辑距离 0.0 ~ 1.0，完全遵循 IBM 规范)
            if not structure_only:
                s1 = u_node.text.strip()
                s2 = v_node.text.strip()
                if s1 or s2:
                    sim = _levenshtein_ratio(s1, s2)
                    return 1.0 - sim
                return 0.0

            return 0.0

        return 0.0

    treedist = [[0.0] * (n2 + 1) for _ in range(n1 + 1)]

    for i in sorted(kr1):
        for j in sorted(kr2):
            m_len = i - l1[i] + 2
            n_len = j - l2[j] + 2
            forestdist = [[0.0] * n_len for _ in range(m_len)]

            for m in range(1, m_len):
                forestdist[m][0] = forestdist[m - 1][0] + 1.0
            for n in range(1, n_len):
                forestdist[0][n] = forestdist[0][n - 1] + 1.0

            for m in range(1, m_len):
                u = l1[i] + m - 1
                u_node = nodes1[u - 1]
                for n in range(1, n_len):
                    v = l2[j] + n - 1
                    v_node = nodes2[v - 1]
                    if l1[u] == l1[i] and l2[v] == l2[j]:
                        forestdist[m][n] = min(
                            forestdist[m - 1][n] + 1.0,
                            forestdist[m][n - 1] + 1.0,
                            forestdist[m - 1][n - 1] + match_cost(u_node, v_node)
                        )
                        treedist[u][v] = forestdist[m][n]
                    else:
                        k = l1[u] - l1[i]
                        l = l2[v] - l2[j]
                        forestdist[m][n] = min(
                            forestdist[m - 1][n] + 1.0,
                            forestdist[m][n - 1] + 1.0,
                            forestdist[k][l] + treedist[u][v]
                        )

    return treedist[n1][n2]


def teds_similarity(
    table1: Union[pd.DataFrame, str, TableNode],
    table2: Union[pd.DataFrame, str, TableNode],
    structure_only: bool = False,
    max_sample_rows: Optional[int] = None,
    max_sample_cols: Optional[int] = None
) -> float:
    """
    计算两个表格对象间的 IBM TEDS (Tree-Edit-Distance-based Similarity) 相似度 (0.0 ~ 1.0)。
    
    参数:
        table1, table2: 可以是 pd.DataFrame, HTML 字符串, 或 TableNode 树对象
        structure_only: 若为 True，仅评估 DOM 结构与跨行跨列拓扑，忽略单元格纯文字差异
        max_sample_rows: 针对超大表采样的行数上限，默认 None 进行全量无截断评估
        max_sample_cols: 针对宽表采样的列数上限，默认 None
    """
    # 1. 规范化为 TableNode (优先保留 raw_html/html 中的 colspan 与 rowspan 跨行跨列拓扑)
    def _to_node(tbl):
        if isinstance(tbl, TableNode):
            return tbl
        if isinstance(tbl, pd.DataFrame):
            raw_html = tbl.attrs.get("raw_html") or tbl.attrs.get("html") if hasattr(tbl, "attrs") else None
            if raw_html and isinstance(raw_html, str) and "<table" in raw_html.lower():
                return parse_html_to_tree(raw_html, max_rows=max_sample_rows)
            return dataframe_to_table_tree(tbl, max_rows=max_sample_rows, max_cols=max_sample_cols)
        if isinstance(tbl, str):
            return parse_html_to_tree(tbl, max_rows=max_sample_rows)
        return TableNode("table")

    node1 = _to_node(table1)
    node2 = _to_node(table2)

    # 边界情况
    len1 = len(_get_postorder_nodes(node1))
    len2 = len(_get_postorder_nodes(node2))
    if len1 <= 1 and len2 <= 1:
        return 1.0
    if len1 <= 1 or len2 <= 1:
        return 0.0

    ted = zhang_shasha_ted(node1, node2, structure_only=structure_only)
    max_nodes = max(len1, len2)
    score = 1.0 - (ted / max_nodes)
    return max(0.0, min(1.0, float(score)))


class TEDS:
    """
    TEDS (Tree-Edit-Distance-based Similarity) 表格拓扑与内容相似度度量类。
    基于纯 Python Zhang-Shasha 树编辑距离算法实现，编辑代价定义对齐 IBM PubTabNet (CustomConfig.rename) 语义
    (标签差异与跨行跨列不匹配代价均为 1.0，单元格文本差异按归一化编辑距离计算)。
    注意：本模块为本地纯 Python 树编辑距离拓扑对齐实现，非 IBM APTED 原生 C 库绑定环境，评测数值用于拓扑对比与一致性仲裁，非 IBM 官方认证标准测试结果。
    """
    def __init__(self, structure_only: bool = False, n_jobs: int = 1):
        self.structure_only = structure_only
        self.n_jobs = n_jobs

    def evaluate(self, pred: Union[str, pd.DataFrame, TableNode, Any], gt: Union[str, pd.DataFrame, TableNode, Any]) -> float:
        """评估预测表格与标注表格的 TEDS 得分 (0.0 ~ 1.0)。若任意一方为空表/无目标则返回 0.0。全表无采样评估。"""
        if pred is None or gt is None:
            return 0.0
        if isinstance(pred, pd.DataFrame) and pred.empty:
            return 0.0
        if isinstance(gt, pd.DataFrame) and gt.empty:
            return 0.0
        if isinstance(pred, str) and (not pred.strip() or "<table" not in pred.lower()):
            return 0.0
        if isinstance(gt, str) and (not gt.strip() or "<table" not in gt.lower()):
            return 0.0
        return teds_similarity(pred, gt, structure_only=self.structure_only, max_sample_rows=None, max_sample_cols=None)

    def batch_evaluate(self, preds: List[Any], gts: List[Any]) -> List[float]:
        return [self.evaluate(p, g) for p, g in zip(preds, gts)]
