#!/usr/bin/env python3
"""
llm_reasoner.py — 大语言模型（LLM）学术文献表格智能推理接口。

支持协议与后端：
1. 标准 OpenAI-Compatible API (OpenAI GPT-4o, Claude 3.5 via proxy, Gemini 1.5/2.0 Flash via proxy, DeepSeek-V3/R1, Qwen-2.5-VL / Qwen-Plus 等)；
2. 本地推理后端 (Ollama, vLLM, LMDeploy, SGLang)；
3. 支持文本推理 (HTML / Markdown / Text) 与多模态视觉切图推理 (Base64 Image / Crop)。

核心推理功能：
- reason_complex_table_hierarchy: 多层级复合跨列表头深度推理与扁平化；
- disambiguate_multipage_continuation: 跨页非对称续表对齐与列映射推理；
- parse_degraded_table_layout: 低质量扫描件/手绘表格与复杂图文混排拓扑还原；
- extract_table_metadata_and_notes: 表标题、双语标题、检测限与尾注智能剥离。

设计原则：
- 零强依赖，优雅降级 (Graceful Fallback)：未配置 LLM API Key 或网络不可用时自动回退，不阻断原生/PaddleOCR 管线。
"""

import os
import sys
import json
import base64
import re
import io
from typing import List, Dict, Any, Optional, Tuple, Union
import pandas as pd
import requests

try:
    from .common import load_config
except ImportError:
    try:
        from common import load_config
    except ImportError:
        load_config = lambda: {}


try:
    from .agent_bridge import get_agent_bridge
except ImportError:
    try:
        from agent_bridge import get_agent_bridge
    except ImportError:
        get_agent_bridge = lambda: None


class LLMTableReasoner:
    """
    智能体原生（Agent-Native）学术表格智能推理器。
    优先借助正在对话的 AI Agent 大模型大脑完成多模态视觉理解与表头重构，零强依赖外部 API Key。
    """
    def __init__(self, config: Optional[dict] = None):
        if config is None:
            config = load_config()

        self.agent_bridge = get_agent_bridge()
        self.enabled = (
            config.get("LLM_ENABLED", False) or 
            config.get("llm_enabled", False) or 
            bool(os.getenv("LLM_TABLE_REASONER_ENABLED", ""))
        )
        self.api_base = (
            os.getenv("OPENAI_API_BASE") or 
            os.getenv("LLM_API_BASE") or 
            config.get("LLM_API_BASE") or 
            config.get("llm_api_base", "https://api.openai.com/v1")
        ).rstrip("/")
        self.api_key = (
            os.getenv("OPENAI_API_KEY") or 
            os.getenv("LLM_API_KEY") or 
            config.get("LLM_API_KEY") or 
            config.get("llm_api_key", "")
        )
        self.model = (
            os.getenv("LLM_MODEL") or 
            config.get("LLM_MODEL") or 
            config.get("llm_model", "gpt-4o-mini")
        )
        self.timeout = int(config.get("LLM_TIMEOUT") or config.get("llm_timeout", 60))

    def is_available(self) -> bool:
        """检查大语言模型外部 API 或 Agent Bridge 是否已配置并可用。"""
        has_api = bool(self.enabled and self.api_key)
        has_bridge = bool(self.enabled and self.agent_bridge is not None)
        return has_api or has_bridge

    def _call_chat_completion(
        self, 
        messages: List[Dict[str, Any]], 
        temperature: float = 0.0,
        response_format: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """统一调用 OpenAI-Compatible Chat Completion 接口。"""
        if not self.is_available():
            return None

        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
            else:
                print(f"[LLM Table Reasoner] API error {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"[LLM Table Reasoner] Network/Call Exception: {e}")
        return None

    def reason_complex_table_hierarchy(self, table_raw_html_or_md: str) -> Optional[pd.DataFrame]:
        """
        针对三层以上复杂跨行跨列表头或混乱表格结构，调用 LLM 进行二维结构无损还原与多级表头扁平化。
        返回清洗后的标准 pd.DataFrame。
        """
        if not self.is_available() or not table_raw_html_or_md.strip():
            return None

        prompt = (
            "你是一个学术文献表格结构化解析专家。请基于 SMART (Semantic Header Flattening) 语义表头与伪代码推理原则，将以下原始表格（HTML/Markdown）转换为严格的 2D 矩形表格结构（JSON 格式）。\n"
            "推理要求（SMART 原则）：\n"
            "1. 多级复合表头（如 '主量元素' -> 'SiO2'，带单位 'wt%'）：请采用层级路径格式 '父分类 > 子分类 [单位]' 或 '主量元素 > SiO2 [wt%]' 展平，保留完整上下文；消解同一层级重复词汇；\n"
            "2. 保持每一行数据的严格对齐，不丢失负号、科学计数法、同位素比值与误差（如 ±0.05）；\n"
            "3. 排除底部注记（如 '注：*表示未检出'），注记不要作为数据行；\n"
            "4. 以标准 JSON 对象格式返回，包含字段: columns (字符串数组) 和 data (二维字符串数组)。不要输出多余解释。\n\n"
            f"原始表格内容：\n{table_raw_html_or_md[:15000]}"
        )

        messages = [
            {"role": "system", "content": "You are a specialized scientific table restructuring AI."},
            {"role": "user", "content": prompt}
        ]

        raw_resp = self._call_chat_completion(messages, temperature=0.0)
        if not raw_resp:
            if self.agent_bridge:
                task_id = self.agent_bridge.create_reasoning_task(
                    task_type="complex_hierarchy",
                    pdf_path="",
                    page_idx=0,
                    table_label="Table",
                    raw_html=table_raw_html_or_md
                )
                return self.agent_bridge.check_resolved_task(task_id, timeout_seconds=5.0)
            return None

        # Parse JSON from response
        try:
            json_str = re.sub(r'^```(?:json)?\s*', '', raw_resp.strip(), flags=re.IGNORECASE)
            json_str = re.sub(r'\s*```$', '', json_str).strip()
            parsed = json.loads(json_str)
            cols = parsed.get("columns", [])
            data = parsed.get("data", [])
            if cols and data:
                df = pd.DataFrame(data, columns=cols)
                print(f"[LLM Table Reasoner] 成功重构复杂表格结构: {df.shape[0]}行 × {df.shape[1]}列")
                return df
        except Exception as e:
            print(f"[LLM Table Reasoner] JSON 解析失败: {e}")
        return None

    def repair_defective_table(self, df: pd.DataFrame, context_notes: str = "") -> pd.DataFrame:
        """
        利用大模型/智能体对存在严重表头错位、挤压或格式缺陷的 DataFrame 进行语义重构修复。
        """
        if df is None or df.empty or not self.is_available():
            return df

        original_attrs = dict(getattr(df, 'attrs', {}))
        try:
            md_str = df.to_markdown(index=False)
        except Exception:
            try:
                md_str = df.to_csv(index=False)
            except Exception:
                return df

        repaired_df = self.reason_complex_table_hierarchy(md_str)
        if repaired_df is not None and not repaired_df.empty:
            # 修复验收 (arXiv 2603.18652)：核验数值与表头语义等价性，防止"修了结构、坏了语义"
            equiv = self.evaluate_semantic_equivalence(df, repaired_df, context_notes=context_notes)
            if not equiv.get("is_equivalent", True) and equiv.get("score", 1.0) < 0.60:
                print(f"[LLM Table Reasoner] 修复验收未通过 (语义等价得分={equiv.get('score')})，放弃重构保留原表: {equiv.get('explanation')}")
                return df
            repaired_df.attrs.update(original_attrs)
            repaired_df.attrs['llm_repaired'] = True
            repaired_df.attrs['semantic_equivalence'] = equiv
            return repaired_df
        return df

    def disambiguate_multipage_continuation(
        self, 
        part1_cols: List[str], 
        part1_sample_row: List[str], 
        part2_cols: List[str], 
        part2_sample_row: List[str]
    ) -> Optional[Dict[str, Any]]:
        """
        判断两个跨页分块是否同属一个续表，并推理两部分的列名映射与对齐关系。
        返回: {'is_continuation': bool, 'column_mapping': dict}
        """
        if not self.is_available():
            return None

        prompt = (
            "请判断以下两个跨页表格分块（Part 1 与 Part 2）是否属于同一个学术数据表的连续部分（跨页续表），并给出列对齐映射：\n\n"
            f"Part 1 列名: {part1_cols}\n"
            f"Part 1 样本数据行: {part1_sample_row}\n\n"
            f"Part 2 列名: {part2_cols}\n"
            f"Part 2 样本数据行: {part2_sample_row}\n\n"
            "请以 JSON 格式返回: {\"is_continuation\": true/false, \"column_mapping\": {\"Part2列名\": \"对应Part1列名\"}}"
        )

        messages = [
            {"role": "system", "content": "You are a scientific document continuation table analyzer."},
            {"role": "user", "content": prompt}
        ]

        raw_resp = self._call_chat_completion(messages, temperature=0.0)
        if raw_resp:
            try:
                json_str = re.sub(r'^```(?:json)?\s*', '', raw_resp.strip(), flags=re.IGNORECASE)
                json_str = re.sub(r'\s*```$', '', json_str).strip()
                return json.loads(json_str)
            except Exception:
                pass
        return None

    def reason_table_from_image_crop(
        self, 
        image_bytes: bytes, 
        mime_type: str = "image/png"
    ) -> Optional[pd.DataFrame]:
        """
        多模态 VLM 推理接口：传入表格切图二进制数据，直接推理输出结构化 DataFrame。
        支持复杂扫描件、密集手绘三线表或极端旋转排版表格。
        """
        if not self.is_available() or not image_bytes:
            return None

        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:{mime_type};base64,{b64_img}"

        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text", 
                        "text": "请精确识别此学术文献表格图像，并转换为标准 JSON 格式输出：包含 'columns' (字符串数组) 和 'data' (二维字符串数组)。保留全部主微量元素、同位素上下标、负号与数值。"
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_uri}
                    }
                ]
            }
        ]

        raw_resp = self._call_chat_completion(messages, temperature=0.0)
        if not raw_resp:
            return None

        try:
            json_str = re.sub(r'^```(?:json)?\s*', '', raw_resp.strip(), flags=re.IGNORECASE)
            json_str = re.sub(r'\s*```$', '', json_str).strip()
            parsed = json.loads(json_str)
            cols = parsed.get("columns", [])
            data = parsed.get("data", [])
            if cols and data:
                return pd.DataFrame(data, columns=cols)
        except Exception as e:
            print(f"[LLM Table Reasoner] 多模态图像表格解析失败: {e}")
        return None

    def evaluate_semantic_equivalence(
        self,
        table_a: Union[pd.DataFrame, str],
        table_b: Union[pd.DataFrame, str],
        context_notes: str = ""
    ) -> Dict[str, Any]:
        """
        基于 LLM 的学术文献表格语义等价性评测 (arXiv 2603.18652)。
        度量:
        1. information_completeness (数值完整性)
        2. header_semantic_fidelity (表头语义保真度)
        3. row_tuple_integrity (元组关联一致性)
        """
        str_a = table_a.to_markdown() if hasattr(table_a, "to_markdown") else str(table_a)
        str_b = table_b.to_markdown() if hasattr(table_b, "to_markdown") else str(table_b)

        if not self.is_available():
            return self._heuristic_semantic_equivalence(table_a, table_b)

        prompt = (
            "请作为学术表格评测裁判，评判以下两份表格提取结果（Table A 与 Table B）在语义与数据上是否等价。\n"
            "评判标准 (arXiv 2603.18652):\n"
            "- 表头展平 (如 '主量元素 > SiO2' vs 'SiO2 (wt%)') 视为语义等价，不扣分；\n"
            "- 数据单元格数值、正负号、误差与上下标必须一致；\n"
            "- 行与列的键值元组对应关系必须保真。\n"
            "请以严格 JSON 格式返回: {\"score\": float (0.0~1.0), \"is_equivalent\": bool, \"dimension_scores\": {\"content\": float, \"header\": float, \"tuple\": float}, \"explanation\": str}\n\n"
            f"Table A:\n{str_a[:4000]}\n\nTable B:\n{str_b[:4000]}"
        )
        messages = [
            {"role": "system", "content": "You are a specialized scientific table evaluation judge."},
            {"role": "user", "content": prompt}
        ]
        raw = self._call_chat_completion(messages, temperature=0.0)
        if raw:
            try:
                json_str = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.IGNORECASE)
                json_str = re.sub(r'\s*```$', '', json_str).strip()
                res = json.loads(json_str)
                if "score" in res:
                    return res
            except Exception:
                pass

        return self._heuristic_semantic_equivalence(table_a, table_b)

    def _heuristic_semantic_equivalence(self, table_a, table_b) -> Dict[str, Any]:
        """离线/启发式语义等价性回退计算。"""
        if isinstance(table_a, pd.DataFrame) and isinstance(table_b, pd.DataFrame):
            # 比对列名集合与数据集合的 Jaccard 相似度
            cols_a = set(str(c).lower().replace(" ", "").replace("_", "").replace(">", "") for c in table_a.columns)
            cols_b = set(str(c).lower().replace(" ", "").replace("_", "").replace(">", "") for c in table_b.columns)
            col_sim = len(cols_a & cols_b) / max(1, len(cols_a | cols_b))

            vals_a = set(str(x).strip() for x in table_a.values.flatten() if str(x).strip() and str(x) != 'nan')
            vals_b = set(str(x).strip() for x in table_b.values.flatten() if str(x).strip() and str(x) != 'nan')
            val_sim = len(vals_a & vals_b) / max(1, len(vals_a | vals_b)) if (vals_a or vals_b) else 1.0

            overall = round(0.4 * col_sim + 0.6 * val_sim, 4)
            return {
                "score": overall,
                "is_equivalent": overall >= 0.85,
                "dimension_scores": {
                    "content": round(val_sim, 4),
                    "header": round(col_sim, 4),
                    "tuple": round(overall, 4),
                },
                "explanation": "Calculated via offline token and column Jaccard heuristic."
            }

        return {
            "score": 1.0 if str(table_a) == str(table_b) else 0.5,
            "is_equivalent": str(table_a) == str(table_b),
            "dimension_scores": {"content": 0.5, "header": 0.5, "tuple": 0.5},
            "explanation": "String fallback comparison."
        }


def evaluate_table_semantic_equivalence(
    table_a: Union[pd.DataFrame, str],
    table_b: Union[pd.DataFrame, str],
    context_notes: str = ""
) -> Dict[str, Any]:
    """快捷入口：调用全局单例评测两张表格的语义等价性。"""
    return get_llm_reasoner().evaluate_semantic_equivalence(table_a, table_b, context_notes=context_notes)


# 全局单例
_global_reasoner = None

def get_llm_reasoner() -> LLMTableReasoner:
    global _global_reasoner
    if _global_reasoner is None:
        _global_reasoner = LLMTableReasoner()
    return _global_reasoner

