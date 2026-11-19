#!/usr/bin/env python3
"""实测缓存规则：纯政策可命中，工具类绕过，同义变体不误命中。

命中率分母只含精确文本的纯政策问答（含冷启动未命中与重复命中），不含同义变体和工具类。
填缓存时回填第五段已实测答案，本项只考核缓存键与命中，不重复计费生成。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cache import flush_namespace, snapshot_namespace  # noqa: E402
from app.observability import disable_tracing, reset_tracing  # noqa: E402
from app.routing import classify  # noqa: E402
from app.runtime import handle_ask  # noqa: E402
from eval.stage6_traffic import SYNONYM_VARIANTS, TOOL_QUESTIONS  # noqa: E402
from scripts.stage6_common import (  # noqa: E402
    load_member_context,
    load_questions,
    load_stage5_dev,
    write_json,
)


def main() -> int:
    load_dotenv(ROOT / ".env")
    token = disable_tracing()
    try:
        return _run()
    finally:
        reset_tracing(token)


def _run() -> int:
    flush_namespace()
    dev = load_questions("dev")
    stage5 = {item["id"]: item for item in load_stage5_dev()["items"]}
    ctx = load_member_context()

    eligible = []
    for q in dev:
        route = classify(q["question"])
        if route.intent == "policy" and route.cacheable:
            eligible.append(q)
    if not eligible:
        raise RuntimeError("no_cacheable_policy_questions")

    events = []
    n_hit = n_miss = 0

    def ask_policy(question: str, *, precomputed=None, label: str, qid: str, in_denominator: bool, lookup_only: bool = False):
        nonlocal n_hit, n_miss
        before = snapshot_namespace()
        result = handle_ask(
            question,
            ctx=ctx,
            use_cascade=False,
            use_cache=True,
            precomputed_answer=precomputed,
            lookup_only=lookup_only,
        )
        after = snapshot_namespace()
        hit = bool(result.get("cache_hit"))
        event = {
            "id": qid,
            "label": label,
            "in_denominator": in_denominator,
            "intent": result.get("intent"),
            "cacheable": result.get("cacheable"),
            "cache_hit": hit,
            "cache_bypassed": bool(result.get("cache_bypassed")),
            "keys_added": len(after - before),
            "ok": result.get("ok"),
        }
        events.append(event)
        if in_denominator:
            if hit:
                n_hit += 1
            else:
                n_miss += 1
        return result, event

    # 第一轮：冷启动，应全部未命中，并写入缓存。
    first_pass = []
    for q in eligible:
        src = stage5[q["id"]]
        precomputed = {
            "ok": src.get("ok_generation"),
            "question": q["question"],
            "answer": src.get("answer"),
            "model": src.get("model"),
            "citations": src.get("citations"),
            "citation_score": src.get("citation_score"),
            "usage": src.get("usage"),
        }
        _res, ev = ask_policy(
            q["question"],
            precomputed=precomputed,
            label="cold_unique",
            qid=q["id"],
            in_denominator=True,
        )
        first_pass.append(ev)

    cold_all_miss = all(not ev["cache_hit"] for ev in first_pass)
    cold_all_written = all(ev["keys_added"] >= 1 for ev in first_pass)

    # 第二轮：同一批精确文本，应全部命中。
    second_pass = []
    for q in eligible:
        _res, ev = ask_policy(q["question"], label="exact_repeat", qid=q["id"], in_denominator=True)
        second_pass.append(ev)
    exact_all_hit = all(ev["cache_hit"] for ev in second_pass)

    n_eligible = n_hit + n_miss
    hit_rate = (n_hit / n_eligible) if n_eligible else None

    # 同义变体：不得误命中，不进分母。
    synonym_events = []
    n_syn_false = 0
    for syn in SYNONYM_VARIANTS:
        src_q = next(q for q in eligible if q["id"] == syn["source_id"])
        # 源题应已在缓存中
        result, ev = ask_policy(syn["question"], label="synonym", qid=syn["id"], in_denominator=False, lookup_only=True)
        # 同义变体只核查是否误命中，不在未命中时触发生成。
        ev["source_id"] = syn["source_id"]
        ev["source_cached"] = True
        ev["false_hit"] = bool(result.get("cache_hit"))
        synonym_events.append(ev)
        if ev["false_hit"]:
            n_syn_false += 1
    synonym_no_false_hit = n_syn_false == 0

    # 工具类：绕过缓存，不新增政策/检索缓存键，不进分母。
    tool_events = []
    tool_ok = True
    for item in TOOL_QUESTIONS:
        before = snapshot_namespace()
        result = handle_ask(item["question"], ctx=ctx, use_cascade=False, use_cache=True)
        after = snapshot_namespace()
        ev = {
            "id": item["id"],
            "label": "tool",
            "in_denominator": False,
            "intent": result.get("intent"),
            "tool": result.get("tool"),
            "expect_tool": item["expect_tool"],
            "cacheable": result.get("cacheable"),
            "cache_hit": bool(result.get("cache_hit")),
            "cache_bypassed": bool(result.get("cache_bypassed")),
            "keys_added": sorted(after - before),
            "ok": result.get("ok"),
            "error_type": result.get("error_type"),
        }
        ev["pass"] = (
            ev["intent"] == "tool"
            and ev["tool"] == item["expect_tool"]
            and ev["cache_bypassed"]
            and not ev["cache_hit"]
            and ev["keys_added"] == []
        )
        if not ev["pass"]:
            tool_ok = False
        tool_events.append(ev)

    ok = bool(
        cold_all_miss
        and cold_all_written
        and exact_all_hit
        and synonym_no_false_hit
        and tool_ok
        and hit_rate is not None
    )
    report = {
        "ok": ok,
        "timezone": "Asia/Shanghai",
        "protocol": {
            "denominator": "仅精确文本的纯政策问答（开发集可缓存题的冷启动 + 原句重复），不含同义变体和工具类",
            "fill_method": "回填 reports/stage5_dev_citation.json 中已实测的答案，不重复生成",
            "normalization": "Unicode NFKC + 首尾与连续空白折叠，不做同义改写",
        },
        "n_eligible_policy": len(eligible),
        "n_denominator": n_eligible,
        "n_hits": n_hit,
        "n_misses": n_miss,
        "hit_rate": hit_rate,
        "cold_all_miss": cold_all_miss,
        "cold_all_written": cold_all_written,
        "exact_repeat_all_hit": exact_all_hit,
        "n_synonym_variants": len(SYNONYM_VARIANTS),
        "n_synonym_false_hits": n_syn_false,
        "synonym_no_false_hit": synonym_no_false_hit,
        "n_tool_questions": len(TOOL_QUESTIONS),
        "tool_bypass_ok": tool_ok,
        "tool_events": tool_events,
        "synonym_events": synonym_events,
        "policy_events_summary": {
            "cold_misses": sum(1 for e in first_pass if not e["cache_hit"]),
            "repeat_hits": sum(1 for e in second_pass if e["cache_hit"]),
        },
    }
    flush_namespace()
    out = ROOT / "reports" / "stage6_cache.json"
    write_json(out, report)
    print(
        json.dumps(
            {
                "ok": ok,
                "hit_rate": hit_rate,
                "n_hits": n_hit,
                "n_denominator": n_eligible,
                "synonym_false_hits": n_syn_false,
                "tool_bypass_ok": tool_ok,
                "report": str(out.relative_to(ROOT)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
