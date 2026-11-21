"""模型级联：默认 qwen3.8-flash，检索分数达到阈值时升到 qwen-plus。

阈值在最终检索链路（section_window_120 + dense+BM25 RRF）上，用开发集分 5 箱标定。
标定结果写入 reports/cascade_threshold.json。未标定前一律走快速模型。
自评 confidence 只用于对比实验，不进入本模块的主路径。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
THRESHOLD_PATH = ROOT / "reports" / "cascade_threshold.json"

# 升级条件：检索 top-1 RRF 分数小于等于阈值（分数越低越不自信）。
# 未标定文件时 threshold 为 None，表示不升级。
_cached: dict | None = None


def retrieval_score(hits: list[dict] | None) -> dict:
    """从锁定混合检索的命中列表计算级联信号。主信号是 top-1 RRF 分数。"""
    items = list(hits or [])
    top1 = float(items[0]["score"]) if items else 0.0
    top2 = float(items[1]["score"]) if len(items) > 1 else 0.0
    dense1 = float(items[0].get("dense_score") or 0.0) if items else 0.0
    return {
        "signal": "top1_rrf",
        "score": top1,
        "top1_rrf": top1,
        "top2_rrf": top2,
        "margin_rrf": top1 - top2,
        "top1_dense": dense1,
        "n_hits": len(items),
    }


def load_threshold() -> dict:
    global _cached
    if _cached is not None:
        return _cached
    if not THRESHOLD_PATH.is_file():
        _cached = {
            "ok": False,
            "signal": "top1_rrf",
            "upgrade_when": "score_lte",
            "threshold": None,
            "fast_model": "qwen3.8-flash",
            "strong_model": "qwen-plus",
            "note": "尚未标定，默认不升级",
        }
        return _cached
    data = json.loads(THRESHOLD_PATH.read_text(encoding="utf-8"))
    _cached = data
    return data


def reload_threshold() -> dict:
    global _cached
    _cached = None
    return load_threshold()


def choose_model(score: float | None, *, threshold_data: dict | None = None) -> dict:
    """按标定阈值选择模型。score 为 top-1 RRF；越小越倾向升级。"""
    settings = get_settings()
    fast = settings.qwen_fast_model or "qwen3.8-flash"
    strong = settings.qwen_strong_model or "qwen-plus"
    data = threshold_data or load_threshold()
    threshold = data.get("threshold")
    upgraded = False
    reason = "no_threshold_always_fast"
    if threshold is not None and score is not None:
        if float(score) <= float(threshold):
            upgraded = True
            reason = "score_lte_threshold"
        else:
            reason = "score_gt_threshold"
    model = strong if upgraded else fast
    return {
        "model": model,
        "upgraded": upgraded,
        "fast_model": fast,
        "strong_model": strong,
        "signal": data.get("signal") or "top1_rrf",
        "score": score,
        "threshold": threshold,
        "reason": reason,
    }
