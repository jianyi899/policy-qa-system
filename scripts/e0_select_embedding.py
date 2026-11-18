#!/usr/bin/env python3
"""E0：用小规模实测锁定 embedding 模型与维度。禁止编造数字。

选型协议
--------
1. 仅在能调用阿里云 DashScope embedding API 时执行实测（需要 DASHSCOPE_API_KEY）。
2. 候选模型列表以 DashScope 当前可用的文本嵌入模型为准，脚本会逐个发起真实请求。
3. 对每份候选，使用同一组短中文政策句子调用嵌入接口，记录：
   - 是否成功；
   - 实际返回向量维度（以响应为准，不预先写死）；
   - 端到端延迟（本机 clock，单位毫秒）；
   - 错误类型（如有，不含密钥内容）。
4. 锁定规则（在全部候选均有实测结果后应用）：
   - 必须成功返回向量；
   - 优先选择对中文政策句子调用稳定、延迟可接受的模型；
   - 维度以该模型实测值为准；
   - 一经写入 reports/e0_result.json 并完成建列，后续阶段不得更换。
5. 若缺少 DASHSCOPE_API_KEY，不得猜模型、不得假跑、不得编造维度。
   退出码 2，并明确列出所缺密钥名称。DeepSeek 只作行判或备用，不用于本选型。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "reports" / "e0_result.json"

# 候选为协议中的默认名单；实际是否可用以 API 返回为准，维度以实测为准。
DEFAULT_CANDIDATES = [
    "text-embedding-v3",
    "text-embedding-v4",
    "text-embedding-v2",
]

SAMPLE_TEXTS = [
    "星河优选通用质保期限为自签收之日起12个月。",
    "SKU-HX-WATCH-S3 的质量保证期限为自签收之日起24个月。",
    "定制类商品不支持七天无理由退货。",
]


def missing_keys() -> list[str]:
    keys = []
    if not os.environ.get("DASHSCOPE_API_KEY"):
        keys.append("DASHSCOPE_API_KEY")
    return keys


def measure(model: str, api_key: str) -> dict:
    # 延迟到确认有密钥后再导入官方 SDK，避免无密钥环境的无谓依赖问题。
    import dashscope
    from dashscope import TextEmbedding

    dashscope.api_key = api_key
    started = time.perf_counter()
    response = TextEmbedding.call(model=model, input=SAMPLE_TEXTS)
    elapsed_ms = (time.perf_counter() - started) * 1000
    status_code = getattr(response, "status_code", None)
    if status_code != 200:
        code = getattr(response, "code", None)
        message = getattr(response, "message", None)
        return {
            "model": model,
            "ok": False,
            "status_code": status_code,
            "error_code": str(code) if code is not None else None,
            "error_type": type(message).__name__ if message is not None else "api_error",
            "latency_ms": round(elapsed_ms, 2),
        }

    embeddings = []
    output = getattr(response, "output", None) or {}
    if isinstance(output, dict):
        embeddings = output.get("embeddings") or []
    # 兼容不同 SDK 返回结构
    if not embeddings and hasattr(response, "output"):
        try:
            embeddings = response.output["embeddings"]
        except Exception:  # noqa: BLE001
            embeddings = []

    dims = []
    for item in embeddings:
        vec = item.get("embedding") if isinstance(item, dict) else None
        if vec is not None:
            dims.append(len(vec))
    unique_dims = sorted(set(dims))
    return {
        "model": model,
        "ok": True,
        "status_code": status_code,
        "n_vectors": len(dims),
        "dims": unique_dims,
        "dim": unique_dims[0] if len(unique_dims) == 1 else None,
        "latency_ms": round(elapsed_ms, 2),
    }


def main() -> int:
    load_dotenv(ROOT / ".env")
    missing = missing_keys()
    if missing:
        print("E0 未执行：缺少调用嵌入模型 API 的密钥。")
        print("缺以下环境变量（仅列名称，不采集、不打印任何密钥值）：")
        for name in missing:
            print(f"  - {name}")
        print("DeepSeek 仅用于行判或备用，不用于 E0 embedding 选型。")
        print("请在安全的环境变量中提供 DASHSCOPE_API_KEY 后重新运行本脚本。")
        return 2

    api_key = os.environ["DASHSCOPE_API_KEY"]
    candidates = [
        item.strip()
        for item in os.environ.get("E0_CANDIDATE_MODELS", ",".join(DEFAULT_CANDIDATES)).split(",")
        if item.strip()
    ]
    results = []
    for model in candidates:
        try:
            results.append(measure(model, api_key))
        except Exception as exc:  # noqa: BLE001 - 实测失败也要记录类型
            results.append(
                {
                    "model": model,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "latency_ms": None,
                }
            )

    ok_results = [r for r in results if r.get("ok") and r.get("dim")]
    selected = None
    if ok_results:
        # 在成功结果中选择延迟最低者；这是明确规则，数字全部来自本次实测。
        selected = min(ok_results, key=lambda r: r["latency_ms"])

    report = {
        "status": "selected" if selected else "no_successful_candidate",
        "protocol": "dashscope_small_scale_latency_and_dim",
        "sample_size": len(SAMPLE_TEXTS),
        "candidates": results,
        "selected_model": selected["model"] if selected else None,
        "selected_dim": selected["dim"] if selected else None,
        "note": (
            "选定后请将 EMBEDDING_MODEL / EMBEDDING_DIM 写入环境变量，"
            "再运行 python db/migrate.py 按实测维度建列。选定后不要更换。"
        ),
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if selected else 3


if __name__ == "__main__":
    raise SystemExit(main())
