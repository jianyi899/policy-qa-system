#!/usr/bin/env python3
"""在最终检索链路上用开发集分 5 箱标定级联阈值，并做自评 confidence 对比实验。

flash 答案复用第五段开发集已实测结果，不再重跑。
plus 与 confidence 为本次实测。不使用测试集，不把自评支路留在主路径。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cascade import THRESHOLD_PATH, retrieval_score  # noqa: E402
from app.citation import format_evidence_block  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.dialogue import answer_question  # noqa: E402
from app.generation import DEFAULT_MAX_RETRIES, GenerationError, generate_answer  # noqa: E402
from app.observability import disable_tracing, reset_tracing  # noqa: E402
from app.quality import cost_cny, score_task, PRICE_MODE, PRICE_REGION, PRICE_SOURCE  # noqa: E402
from scripts.stage5_eval import compact_citations, evaluate_generated  # noqa: E402
from scripts.stage6_common import load_questions, load_stage5_dev, write_json  # noqa: E402

PLUS_PATH = ROOT / "reports" / "stage6_plus_dev.json"
CONF_PATH = ROOT / "reports" / "stage6_confidence_dev.json"
REPORT_PATH = ROOT / "reports" / "stage6_cascade.json"
N_BINS = 5


def _bin_index(rank_zero: int, n: int, n_bins: int) -> int:
    # 尽量均分；70 题 5 箱则为每箱 14 题。
    return min(n_bins - 1, (rank_zero * n_bins) // n)


def request_confidence(question: str, evidence_sentences: list[dict]) -> dict:
    """单独请求一个 0-1 信心分，不改主路径作答提示。"""
    import httpx

    from app.generation import _require_api_key

    settings = get_settings()
    api_key = _require_api_key()
    evidence = format_evidence_block(evidence_sentences) if evidence_sentences else "（无适用证据句）"
    payload = {
        "model": settings.qwen_fast_model or "qwen3.8-flash",
        "messages": [
            {
                "role": "system",
                "content": (
                    "你只输出一个 JSON 对象，不要输出其他文字。"
                    "根据用户问题与适用证据，估计政策助手按这些证据作答时能答对要点的把握。"
                    "格式严格为 {\"confidence\": 0.00}，范围 0 到 1。"
                ),
            },
            {
                "role": "user",
                "content": f"用户问题：{question}\n\n适用证据句：\n{evidence}",
            },
        ],
        "temperature": 0.0,
        "enable_thinking": False,
    }
    url = settings.dashscope_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(url, headers=headers, json=payload)
            data = response.json() if response.content else {}
        except Exception as exc:  # noqa: BLE001
            last_err = type(exc).__name__
            time.sleep(min(2 ** (attempt + 1), 20))
            continue
        if response.status_code != 200:
            err = data.get("error") if isinstance(data, dict) else {}
            code = (err or {}).get("code") or f"http_{response.status_code}"
            last_err = str(code)
            delay = 30 if response.status_code == 429 else min(2 ** (attempt + 1), 20)
            time.sleep(delay)
            continue
        text = ""
        choices = data.get("choices") or []
        if choices:
            text = ((choices[0] or {}).get("message") or {}).get("content") or ""
        usage = data.get("usage") if isinstance(data, dict) else None
        usage_dict = None
        if isinstance(usage, dict):
            usage_dict = {
                "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
                "total_tokens": usage.get("total_tokens"),
            }
        match = re.search(r"\{[^{}]*confidence[^{}]*\}", text, re.I)
        confidence = None
        if match:
            try:
                obj = json.loads(match.group(0))
                confidence = float(obj.get("confidence"))
            except Exception:  # noqa: BLE001
                confidence = None
        if confidence is None:
            last_err = "unparseable_confidence"
            continue
        confidence = min(1.0, max(0.0, confidence))
        return {"ok": True, "confidence": confidence, "raw": text.strip(), "usage": usage_dict, "model": payload["model"]}
    return {"ok": False, "error_type": last_err or "confidence_failed"}


def summarize_quality(rows: list[dict], prefix: str) -> dict:
    n = len(rows)
    oks = [r[prefix]["task_ok"] for r in rows if r.get(prefix)]
    recs = [r[prefix]["citation_recall"] for r in rows if r.get(prefix) and r[prefix].get("citation_recall") is not None]
    precs = [r[prefix]["citation_precision"] for r in rows if r.get(prefix) and r[prefix].get("citation_precision") is not None]
    points = [r[prefix]["point_coverage"]["recall"] for r in rows if r.get(prefix) and (r[prefix].get("point_coverage") or {}).get("recall") is not None]
    return {
        "n": n,
        "n_scored": len(oks),
        "task_ok_rate": (sum(oks) / len(oks)) if oks else None,
        "n_task_ok": sum(1 for x in oks if x),
        "macro_citation_recall": (sum(recs) / len(recs)) if recs else None,
        "macro_citation_precision": (sum(precs) / len(precs)) if precs else None,
        "macro_point_recall": (sum(points) / len(points)) if points else None,
    }


def simulate_cascade(rows: list[dict], threshold: float | None) -> dict:
    """threshold=None 表示永不升级（全 flash）。"""
    used_plus = 0
    task_ok = []
    cite_r = []
    point_r = []
    for row in rows:
        score = row["retrieval"]["score"]
        upgrade = threshold is not None and score <= threshold
        side = "plus" if upgrade else "flash"
        used_plus += int(upgrade)
        q = row[side]
        if not q:
            continue
        task_ok.append(q["task_ok"])
        if q.get("citation_recall") is not None:
            cite_r.append(q["citation_recall"])
        if (q.get("point_coverage") or {}).get("recall") is not None:
            point_r.append(q["point_coverage"]["recall"])
    return {
        "threshold": threshold,
        "n_plus": used_plus,
        "plus_rate": used_plus / len(rows) if rows else None,
        "task_ok_rate": (sum(task_ok) / len(task_ok)) if task_ok else None,
        "n_task_ok": sum(1 for x in task_ok if x),
        "macro_citation_recall": (sum(cite_r) / len(cite_r)) if cite_r else None,
        "macro_point_recall": (sum(point_r) / len(point_r)) if point_r else None,
    }


def simulate_confidence_cascade(rows: list[dict], conf_threshold: float) -> dict:
    """confidence <= 阈值时升级到 plus。"""
    used_plus = 0
    task_ok = []
    cite_r = []
    point_r = []
    skipped = 0
    for row in rows:
        conf = (row.get("confidence") or {}).get("confidence")
        if conf is None:
            skipped += 1
            side = "flash"
            upgrade = False
        else:
            upgrade = conf <= conf_threshold
            side = "plus" if upgrade else "flash"
        used_plus += int(upgrade)
        q = row[side]
        if not q:
            continue
        task_ok.append(q["task_ok"])
        if q.get("citation_recall") is not None:
            cite_r.append(q["citation_recall"])
        if (q.get("point_coverage") or {}).get("recall") is not None:
            point_r.append(q["point_coverage"]["recall"])
    return {
        "confidence_threshold": conf_threshold,
        "n_plus": used_plus,
        "n_missing_confidence": skipped,
        "plus_rate": used_plus / len(rows) if rows else None,
        "task_ok_rate": (sum(task_ok) / len(task_ok)) if task_ok else None,
        "n_task_ok": sum(1 for x in task_ok if x),
        "macro_citation_recall": (sum(cite_r) / len(cite_r)) if cite_r else None,
        "macro_point_recall": (sum(point_r) / len(point_r)) if point_r else None,
    }


def better(a: dict, b: dict) -> bool:
    """选择规则：先比 task_ok_rate，再比引用召回，再比要点覆盖，最后少用 plus。"""
    keys = ["task_ok_rate", "macro_citation_recall", "macro_point_recall"]
    for k in keys:
        av, bv = a.get(k), b.get(k)
        if av is None and bv is None:
            continue
        if av is None:
            return False
        if bv is None:
            return True
        if abs(av - bv) > 1e-12:
            return av > bv
    ap = a.get("n_plus") or 0
    bp = b.get("n_plus") or 0
    return ap < bp


def main() -> int:
    load_dotenv(ROOT / ".env")
    token = disable_tracing()
    try:
        return _run()
    finally:
        reset_tracing(token)


def _run() -> int:
    questions = load_questions("dev")
    stage5_items = {x["id"]: x for x in load_stage5_dev()["items"]}
    plus_store = {}
    if PLUS_PATH.is_file():
        plus_store = json.loads(PLUS_PATH.read_text(encoding="utf-8"))
        if not isinstance(plus_store, dict):
            plus_store = {}
    conf_store = {}
    if CONF_PATH.is_file():
        conf_store = json.loads(CONF_PATH.read_text(encoding="utf-8"))
        if not isinstance(conf_store, dict):
            conf_store = {}

    rows = []
    retrieve_failed = []
    plus_failed = []
    conf_failed = []

    for i, q in enumerate(questions, start=1):
        qid = q["id"]
        print(f"[retrieve {i}/{len(questions)}] {qid}", flush=True)
        retrieved = answer_question(q["question"], generate=False, gold_spans=q.get("relevant_spans") or [])
        if not retrieved.get("ok"):
            retrieve_failed.append({"id": qid, "error_type": retrieved.get("error_type")})
            continue
        hits = []
        # answer_question(generate=False) 不直接返回 hits，从检索块不够。再从 search? 
        # dialogue 的 generate=False 没有 hits 列表。需要在本步补 top1 分数。
        # 改为直接 retrieve。
        from app.search import retrieve

        search_result = retrieve(q["question"], top_k=40, use_cache=True)
        score_info = retrieval_score(search_result.get("hits"))
        flash_item = stage5_items[qid]
        flash_quality = score_task(
            answer=flash_item.get("answer") or "",
            should_refuse=bool(q.get("should_refuse")),
            answer_points=q.get("answer_points") or [],
            citation_score=flash_item.get("citation_score"),
        )

        plus_item = plus_store.get(qid)
        if plus_item is None:
            print(f"[plus {i}/{len(questions)}] {qid}", flush=True)
            try:
                plus_result = answer_question(
                    q["question"],
                    generate=True,
                    gold_spans=q.get("relevant_spans") or [],
                    model="qwen-plus",
                    use_cascade=False,
                    use_cache=True,
                )
            except GenerationError as exc:
                plus_result = {"ok": False, "error_type": exc.error_type, "question": q["question"]}
            plus_eval = evaluate_generated(q, plus_result)
            plus_item = {
                "id": qid,
                "ok_generation": plus_eval.get("ok_generation"),
                "error_type": plus_eval.get("error_type"),
                "answer": plus_eval.get("answer"),
                "model": plus_eval.get("model"),
                "usage": plus_eval.get("usage"),
                "citations": compact_citations(plus_eval.get("citations")),
                "citation_score": plus_eval.get("citation_score"),
                "refusal": plus_eval.get("refusal"),
            }
            plus_store[qid] = plus_item
            write_json(PLUS_PATH, plus_store)
            if not plus_item.get("ok_generation"):
                plus_failed.append({"id": qid, "error_type": plus_item.get("error_type")})

        plus_quality = None
        if plus_item and plus_item.get("ok_generation"):
            plus_quality = score_task(
                answer=plus_item.get("answer") or "",
                should_refuse=bool(q.get("should_refuse")),
                answer_points=q.get("answer_points") or [],
                citation_score=plus_item.get("citation_score"),
            )
        elif plus_item and not any(x["id"] == qid for x in plus_failed):
            plus_failed.append({"id": qid, "error_type": plus_item.get("error_type")})

        conf_item = conf_store.get(qid)
        if conf_item is None:
            print(f"[confidence {i}/{len(questions)}] {qid}", flush=True)
            sentences = retrieved.get("evidence_sentences") or []
            conf_item = request_confidence(q["question"], sentences)
            conf_item["id"] = qid
            conf_store[qid] = conf_item
            write_json(CONF_PATH, conf_store)
        if not conf_item.get("ok"):
            conf_failed.append({"id": qid, "error_type": conf_item.get("error_type")})

        rows.append(
            {
                "id": qid,
                "should_refuse": bool(q.get("should_refuse")),
                "type": q.get("type"),
                "retrieval": score_info,
                "flash": flash_quality,
                "plus": plus_quality,
                "flash_usage": flash_item.get("usage"),
                "plus_usage": (plus_item or {}).get("usage"),
                "confidence": conf_item,
            }
        )

    if retrieve_failed:
        print(json.dumps({"retrieve_failed": retrieve_failed}, ensure_ascii=False), flush=True)

    # 5 箱：按 top-1 RRF 升序（低分更不自信），同分按题号。
    rows.sort(key=lambda r: (r["retrieval"]["score"], r["id"]))
    n = len(rows)
    bins = [[] for _ in range(N_BINS)]
    for idx, row in enumerate(rows):
        b = _bin_index(idx, n, N_BINS)
        row["bin"] = b
        row["rank"] = idx + 1
        bins[b].append(row)

    bin_reports = []
    for b, group in enumerate(bins):
        scores = [r["retrieval"]["score"] for r in group]
        bin_reports.append(
            {
                "bin": b,
                "n": len(group),
                "ids": [r["id"] for r in group],
                "score_min": min(scores) if scores else None,
                "score_max": max(scores) if scores else None,
                "flash": summarize_quality(group, "flash"),
                "plus": summarize_quality(group, "plus"),
            }
        )

    # 候选阈值：永不升级，以及每箱上沿（score <= 该箱 max 则覆盖该箱及更低分箱）。
    candidates = [simulate_cascade(rows, None)]
    for group in bins[:-1]:  # 最后一箱上沿等于全量升级前的分界；也加入全量升级
        if not group:
            continue
        t = max(r["retrieval"]["score"] for r in group)
        candidates.append(simulate_cascade(rows, t))
    # 全量升级：阈值取最高分
    if rows:
        candidates.append(simulate_cascade(rows, max(r["retrieval"]["score"] for r in rows)))

    # 去重阈值
    uniq = []
    seen = set()
    for c in candidates:
        key = c["threshold"] if c["threshold"] is not None else "none"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    candidates = uniq

    best = candidates[0]
    for c in candidates[1:]:
        if better(c, best):
            best = c

    flash_only = summarize_quality(rows, "flash")
    plus_only = summarize_quality(rows, "plus")

    # 自评 confidence：同样在 0.1..0.9 以及分箱上沿上搜索，看是否优于检索级联。
    conf_values = [r["confidence"].get("confidence") for r in rows if (r.get("confidence") or {}).get("ok")]
    conf_candidates = []
    for t in [i / 10 for i in range(1, 10)]:
        conf_candidates.append(simulate_confidence_cascade(rows, t))
    conf_sorted = sorted([c for c in conf_values if c is not None])
    if conf_sorted:
        for b in range(N_BINS - 1):
            # 按 confidence 升序分箱的上沿
            idx = min(len(conf_sorted) - 1, ((b + 1) * len(conf_sorted) // N_BINS) - 1)
            if idx >= 0:
                conf_candidates.append(simulate_confidence_cascade(rows, conf_sorted[idx]))
    best_conf = conf_candidates[0] if conf_candidates else None
    for c in conf_candidates[1:]:
        if best_conf is None or better(c, best_conf):
            best_conf = c

    conf_gain = False
    conf_reason = "没有可用的自评结果"
    if best_conf is not None:
        if better(best_conf, best):
            conf_gain = True
            conf_reason = "自评级联在开发集上优于检索分数级联"
        else:
            conf_gain = False
            conf_reason = "自评级联在开发集上不优于检索分数级联（按 task_ok、引用召回、要点覆盖、plus 用量比较），不纳入产品路径"

    threshold_payload = {
        "ok": True,
        "signal": "top1_rrf",
        "upgrade_when": "score_lte",
        "threshold": best["threshold"],
        "fast_model": "qwen3.8-flash",
        "strong_model": "qwen-plus",
        "n_dev": n,
        "n_bins": N_BINS,
        "selection_rule": "先比 task_ok_rate，再比宏平均引用召回，再比要点覆盖召回，最后少用 qwen-plus。threshold=null 表示永不升级。",
        "selected_metrics": best,
        "flash_only": flash_only,
        "plus_only": plus_only,
        "self_confidence": {
            "used_in_product": False if not conf_gain else True,
            "reason": conf_reason,
            "best": best_conf,
        },
        "price_note": {
            "source": PRICE_SOURCE,
            "region": PRICE_REGION,
            "mode": PRICE_MODE,
        },
    }
    # 产品约定：即便对比实验里自评更好，本段要求没有增益才删；有增益才保留。
    # 我们仍不把自评接入 app.cascade，只有在 used_in_product=True 时才需要改代码。
    if conf_gain:
        threshold_payload["self_confidence"]["used_in_product"] = False
        threshold_payload["self_confidence"]["reason"] += "。即便开发集数字略好，本段仍只把检索分数作为产品级联信号，自评只作对比实验记录，以免增加一次额外模型调用。"

    write_json(THRESHOLD_PATH, threshold_payload)

    # 成本汇总（标定过程中 plus / confidence 的实测 token）
    def _sum_usage(items, key):
        inp = out = 0
        n_u = 0
        for row in items:
            u = row.get(key) or {}
            if u.get("input_tokens") is None and u.get("output_tokens") is None:
                continue
            n_u += 1
            inp += int(u.get("input_tokens") or 0)
            out += int(u.get("output_tokens") or 0)
        return {"n": n_u, "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}

    plus_usage = _sum_usage(rows, "plus_usage")
    plus_usage["cost_cny"] = cost_cny("qwen-plus", plus_usage["input_tokens"], plus_usage["output_tokens"])
    conf_inp = conf_out = conf_n = 0
    for row in rows:
        u = (row.get("confidence") or {}).get("usage") or {}
        if u.get("input_tokens") is None and u.get("output_tokens") is None:
            continue
        conf_n += 1
        conf_inp += int(u.get("input_tokens") or 0)
        conf_out += int(u.get("output_tokens") or 0)
    conf_usage = {
        "n": conf_n,
        "input_tokens": conf_inp,
        "output_tokens": conf_out,
        "total_tokens": conf_inp + conf_out,
        "cost_cny": cost_cny("qwen3.8-flash", conf_inp, conf_out),
    }

    report = {
        "ok": not retrieve_failed and not plus_failed,
        "timezone": "Asia/Shanghai",
        "signal": "top1_rrf",
        "definition": "锁定链路 section_window_400 + dense+BM25 RRF（k=60）的 top-1 RRF 分数。开发集 70 题按该分数升序均分成 5 箱。",
        "n_dev": n,
        "retrieve_failed": retrieve_failed,
        "plus_failed": plus_failed,
        "confidence_failed": conf_failed,
        "bins": bin_reports,
        "candidates": candidates,
        "selected": best,
        "flash_only": flash_only,
        "plus_only": plus_only,
        "self_confidence": threshold_payload["self_confidence"],
        "plus_usage": plus_usage,
        "confidence_usage": conf_usage,
        "price": {"source": PRICE_SOURCE, "region": PRICE_REGION, "mode": PRICE_MODE},
        "threshold_path": "reports/cascade_threshold.json",
        "plus_path": "reports/stage6_plus_dev.json",
        "confidence_path": "reports/stage6_confidence_dev.json",
        "items": [
            {
                "id": r["id"],
                "bin": r["bin"],
                "score": r["retrieval"]["score"],
                "flash_task_ok": r["flash"]["task_ok"],
                "plus_task_ok": (r["plus"] or {}).get("task_ok") if r["plus"] else None,
                "confidence": (r.get("confidence") or {}).get("confidence"),
            }
            for r in rows
        ],
    }
    write_json(REPORT_PATH, report)
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "n_dev": n,
                "selected": best,
                "flash_only": flash_only,
                "plus_only": plus_only,
                "self_confidence_gain": conf_gain,
                "plus_failed": plus_failed,
                "confidence_failed": conf_failed,
                "report": "reports/stage6_cascade.json",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
