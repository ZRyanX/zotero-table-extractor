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
from typing import List, Dict, Any, Optional

import requests

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
    from .common import load_config
except ImportError:
    from common import load_config

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

class MinerUClient:
    """
    MinerU 官方云端 API 客户端 (mineru.net)。
    官方提供每日 1,000 页免费提取额度，用于多云双活与百度 AIStudio 队列熔断兜底。
    """
    def __init__(self, api_key: str = "", api_base: str = "https://mineru.net"):
        self.api_key = api_key or os.getenv("MINERU_API_KEY", "")
        self.api_base = (api_base or os.getenv("MINERU_API_BASE", "https://mineru.net") or "https://mineru.net").rstrip("/")

    def submit_task(self, file_path: str, is_ocr: bool = True, enable_table: bool = True, enable_formula: bool = True) -> Optional[str]:
        """
        提交解析任务至 MinerU API，返回 batch_id 或 task_id。
        支持本地 PDF / 图片文件及远程 URL。
        """
        if not self.api_key:
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
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=60)
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    task_id = data.get("task_id") or data.get("batch_id")
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
        payload = {
            "files": [{"name": filename}],
            "model_version": "vlm",
            "enable_formula": enable_formula,
            "enable_table": enable_table,
            "is_ocr": is_ocr
        }

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

    def poll_task(self, task_id: str, timeout: int = 600, cancel_event=None) -> Dict[str, Any]:
        """
        轮询 MinerU 任务状态，直至完成或超时。
        """
        if not self.api_key or not task_id:
            return {"success": False, "error": "invalid_args"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        query_url = f"{self.api_base}/api/v4/extract-results/batch/{task_id}"
        task_query_url = f"{self.api_base}/api/v4/extract/task/{task_id}"

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
                if resp.status_code == 404:
                    resp = requests.get(task_query_url, headers=headers, timeout=30)
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
            data = res_json.get("data", {})
            state = data.get("state") or data.get("status")
            error_msg = data.get("message") or data.get("errorMsg") or data.get("err_msg")

            extract_results = data.get("extract_result") or data.get("extract_results") or []
            if isinstance(extract_results, list) and extract_results:
                first_res = extract_results[0]
                if isinstance(first_res, dict):
                    state = first_res.get("state") or first_res.get("status") or state
                    if not error_msg:
                        error_msg = first_res.get("err_msg") or first_res.get("message") or first_res.get("errorMsg")

            if state in ("done", "success"):
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

            extract_results = result_data.get("extract_result") or result_data.get("extract_results") or []
            if isinstance(extract_results, list) and extract_results:
                first_res = extract_results[0]
                if isinstance(first_res, dict):
                    if "markdown" in first_res and first_res["markdown"]:
                        return first_res["markdown"]
                    if "full_zip_url" in first_res and first_res["full_zip_url"]:
                        return self._download_and_extract_md(first_res["full_zip_url"])

            zip_url = result_data.get("result_url") or result_data.get("full_zip_url")
            if zip_url:
                return self._download_and_extract_md(zip_url)

        return ""

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


def mineru_submit_task(file_path: str, config: Optional[dict] = None) -> Optional[str]:
    """快捷提交 MinerU 任务。"""
    if config is None:
        config = load_config()
    client = MinerUClient(
        api_key=config.get("MINERU_API_KEY", ""),
        api_base=config.get("MINERU_API_BASE", "https://mineru.net")
    )
    return client.submit_task(file_path)


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


def call_mineru_api(file_path: str, config: Optional[dict] = None, timeout: int = 600, cancel_event=None) -> str:
    """
    通过 MinerU 官方云端 API 进行高保真多模态解析并返回 Markdown 表格字符串。
    """
    if config is None:
        config = load_config()
    client = MinerUClient(
        api_key=config.get("MINERU_API_KEY", ""),
        api_base=config.get("MINERU_API_BASE", "https://mineru.net")
    )
    task_id = client.submit_task(file_path)
    if not task_id:
        return ""
    poll_res = client.poll_task(task_id, timeout=timeout, cancel_event=cancel_event)
    if not poll_res.get("success"):
        return ""
    return client.parse_mineru_table_result(poll_res.get("data", {}))


# 全局默认 MinerU 客户端实例
mineru_client = MinerUClient()


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
        or config.get("PADDLEOCR_MCP_AISTUDIO_ACCESS_TOKEN")
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
        # 若未配置百度 Token 但配置了 MinerU，直接平滑切换
        if config.get("MINERU_API_KEY"):
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

    # 提交作业（支持指数退避与满队列重试，并在队列爆满或超时时无缝熔断至 MinerU）
    retry_waits = [5, 15, 30, 60, 90]
    job_id = None
    for attempt, wait_time in enumerate(retry_waits):
        if cancel_event is not None and cancel_event.is_set():
            return {"success": False, "model": model, "error": "cancelled", "combined_markdown": "", "pages": []}
        try:
            if is_url:
                h = dict(headers)
                h["Content-Type"] = "application/json"
                payload = {
                    "fileUrl": file_path,
                    "model": model,
                    "optionalPayload": OPTIONAL_PAYLOAD
                }
                job_response = requests.post(JOB_URL, json=payload, headers=h, timeout=120)
            else:
                data = {
                    "model": model,
                    "optionalPayload": json.dumps(OPTIONAL_PAYLOAD)
                }
                with open(file_path, "rb") as f:
                    files = {"file": f}
                    job_response = requests.post(JOB_URL, headers=headers, data=data, files=files, timeout=120)
        except Exception as e:
            print(f"[{model}] 提交作业失败: {e}")
            is_net_err = isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)) or "timeout" in str(e).lower() or "connection" in str(e).lower()
            if is_net_err and config.get("MINERU_API_KEY") and config.get("OCR_ENGINE", "auto") in ("auto", "mineru"):
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
            if config.get("MINERU_API_KEY") and config.get("OCR_ENGINE", "auto") in ("auto", "mineru"):
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
            if job_response.status_code in (500, 502, 503, 504) and config.get("MINERU_API_KEY") and config.get("OCR_ENGINE", "auto") in ("auto", "mineru"):
                print(f"[{model}] 百度 AIStudio 服务端异常 (status={job_response.status_code})，触发多云熔断，自动切换至 MinerU API...")
                mineru_md = call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)
                if mineru_md:
                    return _make_failover_result(mineru_md)
            return {"success": False, "model": model, "error": f"status_{job_response.status_code}", "combined_markdown": "", "pages": []}
    else:
        print(f"[{model}] 提交作业重试 {len(retry_waits)} 次后仍失败")
        if config.get("MINERU_API_KEY") and config.get("OCR_ENGINE", "auto") in ("auto", "mineru"):
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
            if config.get("MINERU_API_KEY") and config.get("OCR_ENGINE", "auto") in ("auto", "mineru"):
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
            if config.get("MINERU_API_KEY") and config.get("OCR_ENGINE", "auto") in ("auto", "mineru"):
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

def call_paddleocr_vl_online_api(file_path: str, config: Optional[dict] = None, timeout: int = 600, cancel_event=None) -> str:
    """
    通过 AIStudio 在线 API 调用 PaddleOCR-VL-1.6。
    支持多云双活与容灾回退：
    - 若配置 OCR_ENGINE="mineru"，直接调用 MinerU API；
    - 若配置 OCR_ENGINE="auto"，优先调用 PaddleOCR-VL，在队列已满或超时时无缝自动切换至 MinerU API。
    支持传整个 PDF 文件或单页图片文件 / 图像 URL。
    返回合并后的 Markdown 字符串。
    """
    if config is None:
        config = load_config()

    engine = config.get("OCR_ENGINE", "auto").lower()
    if engine == "mineru":
        return call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)

    model = config.get("PADDLEOCR_MCP_MODEL", "PaddleOCR-VL-1.6")
    res = call_paddleocr_job(file_path, model=model, config=config, timeout=timeout, cancel_event=cancel_event)
    md = res.get("combined_markdown", "")
    if not md and config.get("MINERU_API_KEY") and not res.get("failover") and engine in ("auto", "mineru"):
        print("[Dual-Cloud Failover] AIStudio 未返回有效解析结果，尝试 MinerU API 进行视觉兜底...")
        md = call_mineru_api(file_path, config=config, timeout=timeout, cancel_event=cancel_event)
    return md


def run_ppstructure_vlm_pipeline(pdf_path: str, pages: Optional[List[int]] = None, cancel_event=None) -> str:
    """
    PP-StructureV3 + PaddleOCR-VL-1.6 两阶段融合提取管线：
    1. 第一阶段（定位）：由 PP-StructureV3 快速分析版面，识别表格所在页面、提取表格区域切图（Crop）；
    2. 第二阶段（精细解析）：将表格切图（或目标页）送入 PaddleOCR-VL-1.6 进行多模态精细解析，输出精准结构化表格；
    3. 产出合并后的结构化 Markdown。
    """
    config = load_config()

    if cancel_event is not None and cancel_event.is_set():
        return ""

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

    return full_md or ""
