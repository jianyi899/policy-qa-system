"""已锁定的检索链路配置。文件在第三段选型后写入 reports/locked_retrieval.json。"""

from __future__ import annotations

import json
from pathlib import Path

from app.chunking import STRATEGY_SPECS, get_strategy, params_dict

ROOT = Path(__file__).resolve().parents[1]
LOCKED_PATH = ROOT / "reports" / "locked_retrieval.json"

# 在尚未写入锁定文件时，回退到 B0：章节窗口 400 + 纯密集检索。
DEFAULT_PIPELINE = {
    "chunking_name": "section_window_400",
    "chunking": params_dict(STRATEGY_SPECS["section_window_400"], name="section_window_400"),
    "retrieval": "dense_only",
    "bm25": False,
    "rrf": False,
    "rerank": False,
    "embedding_model": "text-embedding-v4",
    "embedding_dim": 1024,
}


def load_locked_pipeline() -> dict:
    if LOCKED_PATH.is_file():
        return json.loads(LOCKED_PATH.read_text(encoding="utf-8"))
    return dict(DEFAULT_PIPELINE)


def locked_chunking_params():
    pipe = load_locked_pipeline()
    name = pipe.get("chunking_name") or "section_window_400"
    return name, get_strategy(name)
