#!/usr/bin/env python3
"""
pdf_page_filter.py — 移植自 zotero-figure 的启发式 PDF 页面过滤预检模块。
在对 PDF 页面运行深度学习视觉分析前，先提取字符并匹配多语言图表关键词，
快速过滤无需识别的纯文本页面，节省推理计算资源。

修复：旧版仅靠关键词判断，误杀跨页续表（续表页只有数据行无标题）与
纯数值数据页。新增「数据密集排列」启发式：当页面含多行、每行含多个
数字 token 时，即使无图表关键词也判定为可能含表格。
"""

import re
from typing import List, Optional

LATIN_PAGE_HINT = re.compile(
    r'(?:^|[^a-z0-9_])(?:fig(?:ure)?s?|tables?|charts?|schemes?|equations?|eqs?|formulae?|figura|figure|tabella|tabelle|grafico|grafici|schema|schemi|equazione|equazioni)(?:$|[^a-z0-9_])',
    re.IGNORECASE
)

RUSSIAN_PAGE_HINT = re.compile(
    r'(?:^|[^а-яё0-9_])(?:рис(?:\.|унок|унка|унки)?|табл(?:\.|ица|ицы)?|график(?:а|и)?|схем(?:а|ы)|формул(?:а|ы)|уравнен(?:ие|я))(?:$|[^а-яё0-9_])',
    re.IGNORECASE
)

CHINESE_NUMBER = r'[0-9一二三四五六七八九十百零〇\u2160-\u217fIVXLCDMivxlcdm]+(?:[-—–._][0-9一二三四五六七八九十百零〇\u2160-\u217fIVXLCDMivxlcdm]+)*'
CHINESE_PAGE_HINT = re.compile(
    f'(?:附表|附录表|补充表|图|表|式)\\s*{CHINESE_NUMBER}|(?:上|下|本|该|如(?:下)?)\\s*(?:图|表|式)|图\\s*(?:中|示)|表\\s*中|公式|方程',
    re.IGNORECASE
)

MATHEMATICAL_PAGE_HINT = re.compile(r'[=∑∫√±≤≥≈≠]')

# 续表标记（跨页续表常见前缀，无需完整 Table N 关键词）
CONTINUATION_HINT = re.compile(
    r'(?:continued|cont\'d|contd|续表|续上表|（续）|\(续\)|接上页|接上表)',
    re.IGNORECASE
)


def _has_dense_data_rows(page_text: str, min_rows: int = 3) -> bool:
    """
    启发式检测页面是否包含多行密集数据（跨页续表或纯数据页）。
    判据：至少 min_rows 行，每行含 >=2 个数字 token 且行长度短（< 120 字符）。
    """
    if not page_text:
        return False
    lines = page_text.split('\n')
    data_row_count = 0
    for line in lines:
        line = line.strip()
        if not line or len(line) > 120:
            continue
        num_tokens = len(re.findall(r'\b\d+(?:\.\d+)?\b', line))
        if num_tokens >= 2:
            data_row_count += 1
            if data_row_count >= min_rows:
                return True
    return False


def page_may_contain_tables(page_text: str) -> bool:
    """
    判断 PDF 页面文本是否可能包含表格或图表元素。
    如果页面纯图片/无可提取文本，或者匹配到中/英/俄文图表关键词、
    数学符号、续表标记、或密集数据行，则返回 True。
    """
    if not page_text or len(page_text.strip()) < 80:
        # 无文本或极少文本（如仅有页眉页脚）的扫描页/插图大表页，必须交由视觉模型检测
        return True

    text = page_text.lower()
    if bool(
        LATIN_PAGE_HINT.search(text) or
        CHINESE_PAGE_HINT.search(page_text) or
        RUSSIAN_PAGE_HINT.search(text) or
        MATHEMATICAL_PAGE_HINT.search(text) or
        CONTINUATION_HINT.search(text)
    ):
        return True

    # 数据密集行检测：捕获跨页续表、纯数据页（无标题关键词）
    if _has_dense_data_rows(page_text):
        return True

    return False
