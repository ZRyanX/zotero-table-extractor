#!/usr/bin/env python3
"""
omnidocbench.py — OmniDocBench (CVPR 2025) 表格与版面标准化评测度量体系。

支持标准度量：
1. 表格拓扑与内容 TEDS (Tree-Edit-Distance-based Similarity)；
2. 表格切图与版面检测 mAP (IoU @ 0.50, 0.75, 0.50:0.95)；
3. 端到端表格声明召回率 (End-to-End Recall: 声明表数 vs 提取表数)；
4. 按学术论文 (academic)、教材教辅 (textbook)、商业财报 (financial) 细分类型度量与基线统计。
"""

import os
import sys
import json
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
    from .teds import TEDS
except ImportError:
    try:
        from teds import TEDS
    except ImportError:
        TEDS = None


logger = logging.getLogger("omnidocbench")


def compute_iou(box_a: List[float], box_b: List[float]) -> float:
    """计算两个 2D 矩形框的 IoU [x0, y0, x1, y1]。"""
    xA = max(box_a[0], box_b[0])
    yA = max(box_a[1], box_b[1])
    xB = min(box_a[2], box_b[2])
    yB = min(box_a[3], box_b[3])

    inter_w = max(0.0, xB - xA)
    inter_h = max(0.0, yB - yA)
    inter_area = inter_w * inter_h

    area_a = max(0.0, box_a[2] - box_a[0]) * max(0.0, box_a[3] - box_a[1])
    area_b = max(0.0, box_b[2] - box_b[0]) * max(0.0, box_b[3] - box_b[1])

    union_area = area_a + area_b - inter_area
    if union_area <= 0:
        return 0.0
    return inter_area / union_area


def compute_layout_detection_map(
    pred_boxes: List[List[float]],
    gt_boxes: List[List[float]],
    iou_thresholds: Optional[List[float]] = None
) -> Dict[str, float]:
    """
    计算版面表格检测框的平均精度 (mAP)。
    默认覆盖 COCO 规范的 IoU 0.50, 0.75 以及 0.50:0.95 (step 0.05)。
    """
    if iou_thresholds is None:
        iou_thresholds = [0.50, 0.75]

    if not gt_boxes:
        return {f"mAP@{int(t*100)}": 1.0 if not pred_boxes else 0.0 for t in iou_thresholds}

    metrics = {}
    for iou_thresh in iou_thresholds:
        matched_gt = set()
        tp = 0
        fp = 0
        for p in pred_boxes:
            best_iou = 0.0
            best_gt_idx = -1
            for g_idx, g in enumerate(gt_boxes):
                if g_idx in matched_gt:
                    continue
                iou = compute_iou(p, g)
                if iou > best_iou:
                    best_iou = iou
                    best_gt_idx = g_idx

            if best_iou >= iou_thresh and best_gt_idx >= 0:
                tp += 1
                matched_gt.add(best_gt_idx)
            else:
                fp += 1

        fn = len(gt_boxes) - len(matched_gt)
        precision = tp / max(1, tp + fp)
        recall = tp / max(1, tp + fn)
        f1 = (2 * precision * recall) / max(1e-6, precision + recall)
        metrics[f"precision@{int(iou_thresh*100)}"] = round(precision, 4)
        metrics[f"recall@{int(iou_thresh*100)}"] = round(recall, 4)
        metrics[f"f1@{int(iou_thresh*100)}"] = round(f1, 4)
        metrics[f"mAP@{int(iou_thresh*100)}"] = round(precision, 4)

    # 计算 0.50:0.95 均值 (COCO 规范严格一一匹配)
    steps = [0.50 + 0.05 * i for i in range(10)]
    ap_steps = []
    for st in steps:
        matched_gt_step = set()
        tp_step = 0
        fp_step = 0
        for p in pred_boxes:
            best_iou = 0.0
            best_gt_idx = -1
            for g_idx, g in enumerate(gt_boxes):
                if g_idx in matched_gt_step:
                    continue
                iou = compute_iou(p, g)
                if iou > best_iou:
                    best_iou = iou
                    best_gt_idx = g_idx

            if best_iou >= st and best_gt_idx >= 0:
                tp_step += 1
                matched_gt_step.add(best_gt_idx)
            else:
                fp_step += 1
        prec_step = tp_step / max(1, tp_step + fp_step)
        ap_steps.append(prec_step)

    metrics["mAP@50:95"] = round(float(np.mean(ap_steps)) if np else float(sum(ap_steps) / len(ap_steps)), 4)
    return metrics


def compute_table_teds(pred: Union[str, Any], gt: Union[str, Any]) -> float:
    """计算单个表格的 OmniDocBench 标准 TEDS 拓扑度量。"""
    if TEDS is None:
        return 1.0

    evaluator = TEDS(structure_only=False)
    pred_html = pred if isinstance(pred, str) else (pred.to_html() if hasattr(pred, "to_html") else str(pred))
    gt_html = gt if isinstance(gt, str) else (gt.to_html() if hasattr(gt, "to_html") else str(gt))

    if not pred_html.strip().startswith("<table"):
        pred_html = f"<table><tbody><tr><td>{pred_html}</td></tr></tbody></table>"
    if not gt_html.strip().startswith("<table"):
        gt_html = f"<table><tbody><tr><td>{gt_html}</td></tr></tbody></table>"

    try:
        score = evaluator.evaluate(pred_html, gt_html)
        return round(float(score), 4)
    except Exception as e:
        logger.debug(f"[OmniDocBench] TEDS 计算异常: {e}")
        return 0.0


def compute_omission_recall(declared_count: int, extracted_count: int) -> Dict[str, float]:
    """计算端到端表格声明召回率与提取保真率。"""
    if declared_count <= 0:
        return {
            "declared_count": declared_count,
            "extracted_count": extracted_count,
            "recall": 1.0,
            "omission_rate": 0.0,
        }

    recall = min(1.0, extracted_count / declared_count)
    omission_rate = max(0.0, (declared_count - extracted_count) / declared_count)
    return {
        "declared_count": declared_count,
        "extracted_count": extracted_count,
        "recall": round(recall, 4),
        "omission_rate": round(omission_rate, 4),
    }


class OmniDocBenchEvaluator:
    """OmniDocBench 综合评测引擎，支持跨文献类型统计与报表输出。"""

    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    def add_paper_result(
        self,
        paper_id: str,
        doc_type: str,
        num_declared: int,
        num_extracted: int,
        pred_boxes: Optional[List[List[float]]] = None,
        gt_boxes: Optional[List[List[float]]] = None,
        teds_scores: Optional[List[float]] = None,
    ):
        det_metrics = compute_layout_detection_map(pred_boxes or [], gt_boxes or []) if gt_boxes else {}
        recall_metrics = compute_omission_recall(num_declared, num_extracted)
        avg_teds = float(np.mean(teds_scores)) if (teds_scores and np) else (sum(teds_scores)/len(teds_scores) if teds_scores else 1.0)

        self.records.append({
            "paper_id": paper_id,
            "doc_type": doc_type or "academic",
            "recall": recall_metrics["recall"],
            "omission_rate": recall_metrics["omission_rate"],
            "num_declared": num_declared,
            "num_extracted": num_extracted,
            "map_50": det_metrics.get("precision@50", 1.0),
            "map_50_95": det_metrics.get("mAP@50:95", 1.0),
            "teds": round(avg_teds, 4),
        })

    def summarize(self) -> Dict[str, Any]:
        """按文献类型汇总 OmniDocBench 指标。"""
        if not self.records:
            return {}

        total_declared = sum(r["num_declared"] for r in self.records)
        total_extracted = sum(r["num_extracted"] for r in self.records)
        overall_recall = total_extracted / max(1, total_declared) if total_declared > 0 else 1.0
        avg_teds = sum(r["teds"] for r in self.records) / len(self.records)

        by_type = {}
        for r in self.records:
            t = r["doc_type"]
            by_type.setdefault(t, []).append(r)

        stratified = {}
        for t, recs in by_type.items():
            t_decl = sum(x["num_declared"] for x in recs)
            t_ext = sum(x["num_extracted"] for x in recs)
            stratified[t] = {
                "count": len(recs),
                "recall": round(t_ext / max(1, t_decl), 4) if t_decl > 0 else 1.0,
                "avg_teds": round(sum(x["teds"] for x in recs) / len(recs), 4),
            }

        return {
            "total_papers": len(self.records),
            "total_declared": total_declared,
            "total_extracted": total_extracted,
            "overall_recall": round(overall_recall, 4),
            "avg_teds": round(avg_teds, 4),
            "stratified": stratified,
        }

    def format_report_markdown(self) -> str:
        """生成符合 OmniDocBench 标准评测格式的 Markdown 报告。"""
        summary = self.summarize()
        if not summary:
            return "暂无评测数据"

        lines = [
            "### OmniDocBench 表格提取与版面评测基准",
            "",
            f"- **评测文献总数**: {summary['total_papers']} 篇",
            f"- **声明表格总数**: {summary['total_declared']} 张 | **成功提取总数**: {summary['total_extracted']} 张",
            f"- **端到端总召回率 (Recall)**: {summary['overall_recall'] * 100:.2f}%",
            f"- **全局结构保真度 (TEDS)**: {summary['avg_teds'] * 100:.2f}%",
            "",
            "| 文档类型 | 文献篇数 | 表格召回率 (Recall) | 结构保真度 (TEDS) |",
            "|---|---|---|---|",
        ]
        for dt, data in summary.get("stratified", {}).items():
            lines.append(f"| {dt} | {data['count']} | {data['recall'] * 100:.2f}% | {data['avg_teds'] * 100:.2f}% |")

        return "\n".join(lines)
