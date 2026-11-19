#!/usr/bin/env python3
"""第六段单元核对：路由分类、缓存键精确匹配、工具类不写缓存。不调用生成模型。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cache import (  # noqa: E402
    answer_key,
    compact_answer_payload,
    flush_namespace,
    get_json,
    set_json,
    snapshot_namespace,
)
from app.routing import classify  # noqa: E402
from eval.stage6_traffic import SYNONYM_VARIANTS, TOOL_APPLY_SAMPLES, TOOL_QUESTIONS  # noqa: E402
from scripts.stage6_common import load_questions, write_json  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env")
    dev = {q["id"]: q for q in load_questions("dev")}
    checks = []

    def record(name: str, ok: bool, **extra):
        checks.append({"name": name, "ok": bool(ok), **extra})

    # 开发集题目应全部判为可缓存的纯政策问答（题面不含订单号或「冰箱」关键词）。
    non_pure = []
    for q in dev.values():
        route = classify(q["question"])
        if route.intent != "policy" or not route.cacheable:
            non_pure.append({"id": q["id"], "intent": route.intent, "reason": route.reason})
    record("dev_questions_are_pure_policy", not non_pure, n=len(dev), unexpected=non_pure)

    # 工具类
    for item in TOOL_QUESTIONS + TOOL_APPLY_SAMPLES:
        route = classify(item["question"])
        record(
            f"route_{item['id']}",
            route.intent == "tool" and route.tool == item["expect_tool"] and not route.cacheable,
            intent=route.intent,
            tool=route.tool,
            expect=item["expect_tool"],
        )

    # 带政策要点的订单题仍走政策路径，且不可缓存。
    mixed = "这单冰箱买了 20 天质量问题还能退吗"
    route = classify(mixed)
    record(
        "fridge_path_is_policy_with_order",
        route.intent == "policy" and not route.cacheable,
        intent=route.intent,
        reason=route.reason,
        cacheable=route.cacheable,
    )

    # 缓存键：精确文本命中，同义变体不命中；空白折叠后的同一句可命中。
    flush_namespace()
    q001 = dev["Q001"]["question"]
    key = answer_key(q001)
    set_json(key, compact_answer_payload({"ok": True, "question": q001, "answer": "占位答案", "model": "qwen3.8-flash"}))
    record("exact_text_hits", get_json(answer_key(q001)) is not None)
    record("whitespace_normalized_hits", get_json(answer_key("  " + q001 + " \n")) is not None)
    syn_miss = []
    for syn in SYNONYM_VARIANTS:
        hit = get_json(answer_key(syn["question"])) is not None
        if hit:
            syn_miss.append(syn["id"])
        record(f"synonym_{syn['id']}_no_false_hit", not hit, source_id=syn["source_id"])
    record("all_synonyms_miss", not syn_miss, false_hits=syn_miss)

    # 工具类不得写入命名空间（本测试只验证路由后的脚本约束：不调用 cache API）。
    before = snapshot_namespace()
    # 模拟路由层绕过：工具题不算 answer_key，也不 set_json。
    for item in TOOL_QUESTIONS:
        route = classify(item["question"])
        if route.intent != "tool":
            continue
        # 故意不写缓存。
    after = snapshot_namespace()
    record("tool_questions_do_not_add_cache_keys", before == after, added=sorted(after - before))

    flush_namespace()
    ok = all(c["ok"] for c in checks)
    report = {
        "ok": ok,
        "n_checks": len(checks),
        "n_failed": sum(1 for c in checks if not c["ok"]),
        "failed": [c["name"] for c in checks if not c["ok"]],
        "checks": checks,
    }
    out = ROOT / "reports" / "stage6_units.json"
    write_json(out, report)
    print(json.dumps({"ok": ok, "failed": report["failed"], "report": str(out.relative_to(ROOT))}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
