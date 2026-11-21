#!/usr/bin/env python3
"""对开发集已生成的引用做小样本 LLM 评判，对照自动句级精度。不据此调参。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from scripts.stage5_common import prepare_stage5_home  # noqa: E402

# 固定抽查题，便于复现；真正读句的人工抽查另见 reports/stage5_citation_audit.json。
SAMPLE_IDS = ["Q001", "Q025", "Q039", "Q041", "Q008"]


def _judge_one(item: dict, settings) -> dict:
    citations = item.get("citations") or []
    auto = item.get("citation_score") or {}
    payload_cites = []
    for c in citations:
        payload_cites.append(
            {
                "sid": c.get("sid"),
                "valid_evidence": c.get("valid_evidence"),
                "doc_id": c.get("doc_id"),
                "section_path": c.get("section_path"),
                "text": c.get("text"),
                "auto_correct": None,
            }
        )
    auto_correct_sids = {c["sid"] for c in citations if False}
    # 自动对错来自 citation_score 明细时可能没有带 correct 字段；按 doc 对齐即可，交给模型独立判断。
    system = (
        "你是引用质量评判员。判断回答里每条 [S#] 引用的句子，是否在事实上支持该句所附着的那部分说法。"
        "只依据给定的引用句文本，不要编造语料。"
        "对每条引用输出 JSON：sid, supports (true/false), reason（不超过40字）。"
        "最后给整题 citation_ok（true/false）。不要输出其它文字。"
    )
    user = json.dumps(
        {
            "question": item.get("id"),
            "answer": item.get("answer"),
            "citations": payload_cites,
        },
        ensure_ascii=False,
    )
    # 重新带上题目文本：调用方传入的 item 可能无 question 字段，用 id 也行，但尽量完整。
    body = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"题目编号：{item.get('id')}\n回答：{item.get('answer')}\n引用句：{json.dumps(payload_cites, ensure_ascii=False)}"},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    url = (settings.deepseek_base_url or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(url, headers=headers, json=body)
    data = resp.json() if resp.status_code == 200 else {}
    text = ""
    if resp.status_code == 200:
        choices = data.get("choices") or []
        if choices:
            text = ((choices[0] or {}).get("message") or {}).get("content") or ""
    parsed = None
    err = None
    if resp.status_code != 200:
        err = f"http_{resp.status_code}"
    else:
        try:
            parsed = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
    return {
        "id": item.get("id"),
        "n_cited": auto.get("n_cited"),
        "auto_precision": auto.get("precision"),
        "auto_n_correct": auto.get("n_correct"),
        "judge": parsed,
        "error_type": err,
        "raw": text if err else None,
    }


def main() -> int:
    prepare_stage5_home()
    settings = get_settings()
    if not settings.deepseek_api_key:
        report = {
            "ok": False,
            "error_type": "missing_DEEPSEEK_API_KEY",
            "note": "未配置评判密钥，LLM 评判未测。",
        }
        out = ROOT / "reports" / "stage5_llm_judge.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    src = json.loads((ROOT / "reports" / "stage5_dev_citation.json").read_text(encoding="utf-8"))
    by_id = {x["id"]: x for x in src.get("items") or []}
    items = []
    for qid in SAMPLE_IDS:
        item = by_id.get(qid)
        if not item or not item.get("ok_generation"):
            items.append({"id": qid, "error_type": "missing_or_failed_generation"})
            continue
        judged = _judge_one(item, settings)
        items.append(judged)
        time.sleep(0.4)

    n_ok = sum(1 for x in items if x.get("judge") and not x.get("error_type"))
    report = {
        "ok": n_ok == len(SAMPLE_IDS),
        "model": "deepseek-chat",
        "sample_ids": SAMPLE_IDS,
        "n_judged": n_ok,
        "n_sample": len(SAMPLE_IDS),
        "note": "仅作对照，不参与调参，也不替代自动句级精度或人工抽查。",
        "items": items,
    }
    out = ROOT / "reports" / "stage5_llm_judge.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "n_judged": n_ok, "sample_ids": SAMPLE_IDS, "report": str(out)}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
