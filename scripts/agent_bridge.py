#!/usr/bin/env python3
"""
agent_bridge.py — 基于对话 AI Agent（当前智能体/MCP/子代理）的原生大语言模型推理桥接器。

核心机理：
1. 零外部 API Key 依赖：直接借助当前正在对话的 AI Agent（多模态大语言模型）作为推理大脑；
2. 异步双向 IPC 协议：
   - 提取管线在遇到疑难表格（如三层复合表头、非对称跨页、模糊三线表）时，生成包含高清表格切图 (PNG) 和候选文本的 Reasoning Task Packet；
   - 将任务写入 `.agent_reasoning/pending/<task_id>.json`；
   - 对话中的 AI Agent 使用原生工具（如 view_file 查看 PNG 切图、大模型多模态理解）推导标准二维矩阵；
   - AI Agent 将解析结果写入 `.agent_reasoning/resolved/<task_id>.json` 或通过 CLI 回写；
   - 提取管线即时加载已解析结果，生成完美的 Excel 文件。
"""
try:
    from .system_detector import (
        IS_MACOS, IS_WINDOWS, get_paper_tables_dir, get_zotero_db_path,
        get_zotero_storage_dir, get_journal_downloader_script,
        get_agent_reasoning_dir, get_audit_db_path, get_targeted_audit_db_path, adapt_path
    )
except ImportError:
    from system_detector import (
        IS_MACOS, IS_WINDOWS, get_paper_tables_dir, get_zotero_db_path,
        get_zotero_storage_dir, get_journal_downloader_script,
        get_agent_reasoning_dir, get_audit_db_path, get_targeted_audit_db_path, adapt_path
    )


import os
import sys
import json
import time
import uuid
import re
from typing import List, Dict, Any, Optional, Tuple, Union
import pandas as pd

def _resolve_default_bridge_dir() -> str:
    env_val = os.getenv("AGENT_BRIDGE_DIR")
    if env_val:
        return env_val
    legacy = adapt_path("/Volumes/ExFat/AIHub/paper_tables/.agent_reasoning")
    if os.path.isdir(os.path.dirname(legacy)):
        return legacy
    skill_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(skill_root, ".agent_reasoning")

BRIDGE_DIR = _resolve_default_bridge_dir()
PENDING_DIR = os.path.join(BRIDGE_DIR, "pending")
RESOLVED_DIR = os.path.join(BRIDGE_DIR, "resolved")
CROPS_DIR = os.path.join(BRIDGE_DIR, "crops")

def ensure_bridge_dirs():
    """确保智能体通信目录存在。"""
    for d in [BRIDGE_DIR, PENDING_DIR, RESOLVED_DIR, CROPS_DIR]:
        os.makedirs(d, exist_ok=True)

class AgentReasoningBridge:
    """
    智能体原生推理桥接器。
    """
    def __init__(self, bridge_dir: Optional[str] = None):
        self.bridge_dir = bridge_dir or BRIDGE_DIR
        self.pending_dir = os.path.join(self.bridge_dir, "pending")
        self.resolved_dir = os.path.join(self.bridge_dir, "resolved")
        self.crops_dir = os.path.join(self.bridge_dir, "crops")
        ensure_bridge_dirs()

    def create_reasoning_task(
        self,
        task_type: str,
        pdf_path: str,
        page_idx: int,
        table_label: str,
        crop_image_path: Optional[str] = None,
        crop_image_bytes: Optional[bytes] = None,
        raw_text: Optional[str] = None,
        raw_html: Optional[str] = None,
        candidate_columns: Optional[List[str]] = None,
        context_notes: Optional[str] = None
    ) -> str:
        """
        生成并提交一个供当前对话 AI Agent 推理的待办任务包。
        返回 task_id。
        """
        task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        
        # 保存切图文件
        saved_crop_path = ""
        if crop_image_bytes:
            saved_crop_path = os.path.join(self.crops_dir, f"{task_id}.png")
            with open(saved_crop_path, "wb") as f:
                f.write(crop_image_bytes)
        elif crop_image_path and os.path.exists(crop_image_path):
            saved_crop_path = crop_image_path

        task_payload = {
            "task_id": task_id,
            "task_type": task_type,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pdf_path": pdf_path,
            "pdf_title": os.path.splitext(os.path.basename(pdf_path))[0],
            "page_idx": page_idx,
            "table_label": table_label,
            "crop_image_path": saved_crop_path,
            "candidate_columns": candidate_columns or [],
            "raw_text_snippet": (raw_text or "")[:4000],
            "raw_html_snippet": (raw_html or "")[:8000],
            "context_notes": context_notes or "",
            "instructions": (
                "请对话中的 AI Agent 查看 crop_image_path 表格图像并结合文本，"
                "输出标准二维结构化 JSON: {'columns': [列名列表], 'data': [[行1数据], [行2数据], ...]}"
            )
        }

        task_file = os.path.join(self.pending_dir, f"{task_id}.json")
        with open(task_file, "w", encoding="utf-8") as f:
            json.dump(task_payload, f, ensure_ascii=False, indent=2)

        print(f"[Agent Bridge] 已为当前 AI Agent 生成推理任务包: {task_id}")
        if saved_crop_path:
            print(f"  • 表格切图路径: {saved_crop_path}")
        return task_id

    def _read_resolved_df(self, resolved_file: str, task_id: str) -> Optional[pd.DataFrame]:
        try:
            with open(resolved_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            cols = data.get("columns", [])
            rows = data.get("data", [])
            if cols and rows:
                df = pd.DataFrame(rows, columns=cols)
                print(f"[Agent Bridge] 成功加载 AI Agent 推理结果: {df.shape[0]}行 × {df.shape[1]}列")
                # 清理已完成的任务待办标记
                pending_file = os.path.join(self.pending_dir, f"{task_id}.json")
                if os.path.exists(pending_file):
                    try:
                        os.remove(pending_file)
                    except Exception:
                        pass
                return df
        except Exception as e:
            print(f"[Agent Bridge] 读取解析结果失败: {e}")
        return None

    def check_resolved_task(
        self, 
        task_id: str, 
        timeout_seconds: float = 0.0, 
        poll_interval: float = 0.5,
        cancel_event = None
    ) -> Optional[pd.DataFrame]:
        """
        检查 AI Agent 是否已完成推理并回写响应。
        若指定 timeout_seconds > 0，则按 poll_interval 轮询等待直到完成或超时。
        针对非交互式环境自动防止死等；若已完成，读取并转换为标准 pd.DataFrame 返回。
        """
        resolved_file = os.path.join(self.resolved_dir, f"{task_id}.json")

        if timeout_seconds <= 0:
            if os.path.exists(resolved_file):
                return self._read_resolved_df(resolved_file, task_id)
            return None

        # 检查是否为无外部智能体监听的静默无头环境，避免盲目阻塞死等
        is_interactive = bool(sys.stdin.isatty() or os.getenv("AGENT_INTERACTIVE") or os.getenv("AGENT_BRIDGE_WAIT"))
        effective_timeout = timeout_seconds if is_interactive else min(timeout_seconds, 10.0)

        start_t = time.time()
        last_log_t = start_t
        while True:
            if cancel_event is not None and getattr(cancel_event, 'is_set', lambda: False)():
                print(f"[Agent Bridge] 任务 {task_id} 等待被取消")
                break

            if os.path.exists(resolved_file):
                return self._read_resolved_df(resolved_file, task_id)

            elapsed = time.time() - start_t
            if elapsed >= effective_timeout:
                if not is_interactive and timeout_seconds > effective_timeout:
                    print(f"[Agent Bridge] 检测到无头运行环境，任务 {task_id} 快速释放等待 ({effective_timeout}s)，优雅回退")
                break

            if time.time() - last_log_t >= 5.0:
                print(f"[Agent Bridge] 等待 AI Agent 推理任务 {task_id} ({elapsed:.1f}s / {effective_timeout}s)...")
                last_log_t = time.time()

            time.sleep(poll_interval)
        return None

    def resolve_task_directly(self, task_id: str, df: pd.DataFrame) -> bool:
        """
        供 AI Agent 或子代理直接调用回写结构化结果。
        """
        if df is None or df.empty:
            return False
        
        resolved_file = os.path.join(self.resolved_dir, f"{task_id}.json")
        payload = {
            "task_id": task_id,
            "resolved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "columns": list(df.columns),
            "data": df.values.tolist(),
            "shape": list(df.shape)
        }
        with open(resolved_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[Agent Bridge] 任务 {task_id} 已成功回写解析响应！")
        return True

    def list_pending_tasks(self) -> List[Dict[str, Any]]:
        """列出所有等待 AI Agent 推理的待办任务。"""
        tasks = []
        if not os.path.exists(self.pending_dir):
            return tasks
        for f in sorted(os.listdir(self.pending_dir)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(self.pending_dir, f), "r", encoding="utf-8") as fp:
                        tasks.append(json.load(fp))
                except Exception:
                    pass
        return tasks


# 全局单例
_global_agent_bridge = None

def get_agent_bridge() -> AgentReasoningBridge:
    global _global_agent_bridge
    if _global_agent_bridge is None:
        _global_agent_bridge = AgentReasoningBridge()
    return _global_agent_bridge


if __name__ == "__main__":
    bridge = get_agent_bridge()
    tasks = bridge.list_pending_tasks()
    print(f"=== Agent Reasoning Bridge 状态 ===")
    print(f"通信目录: {BRIDGE_DIR}")
    print(f"待办推理任务数: {len(tasks)}")
    for t in tasks[:5]:
        print(f"  • [{t['task_id']}] {t['pdf_title']} - {t['table_label']} (Page {t['page_idx']+1})")
        if t.get('crop_image_path'):
            print(f"    切图: {t['crop_image_path']}")
