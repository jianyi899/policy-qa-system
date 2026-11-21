#!/usr/bin/env python3
"""冲突总序的入库后校验：用真实语料切片验证范围优先，不跑测试集生成、不调参。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.conflict import explain_conflict, infer_scope_from_question, resolve  # noqa: E402
from app.search import retrieve  # noqa: E402
from scripts.stage5_common import prepare_stage5_home  # noqa: E402
from scripts.stage5_eval import load_questions  # noqa: E402


def first_docs(resolution: dict, n: int = 8) -> list[dict]:
    out = []
    seen = set()
    for item in resolution.get("applicable") or []:
        doc_id = item.get("doc_id")
        if doc_id in seen:
            continue
        seen.add(doc_id)
        out.append(
            {
                "doc_id": doc_id,
                "scope_match": item.get("scope_match"),
                "source_type": item.get("source_type"),
                "scope_value": item.get("scope_value"),
                "effective_date": item.get("effective_date"),
                "conflict_rank": item.get("conflict_rank"),
            }
        )
        if len(out) >= n:
            break
    return out


def main() -> int:
    home = prepare_stage5_home()
    errors: list[str] = []
    q041 = next(x for x in load_questions("dev") if x["id"] == "Q041")
    inferred = infer_scope_from_question(q041["question"])
    search = retrieve(q041["question"], top_k=40)
    resolution = resolve(search["hits"], {"sku": inferred.get("sku"), "category": inferred.get("category")})
    expl = explain_conflict(resolution)
    docs041 = first_docs(resolution)
    ids041 = [d["doc_id"] for d in docs041]
    if inferred.get("category") != "手机数码":
        errors.append(f"Q041 category not inferred: {inferred}")
    if "P15" not in ids041:
        errors.append(f"Q041 applicable docs missing P15: {ids041}")
    elif ids041:
        # P15 作为品类专项应排在通用文档之前。
        p15_i = ids041.index("P15")
        general_ids = [d["doc_id"] for d in docs041 if d.get("scope_match") == "general"]
        if general_ids:
            first_general = ids041.index(general_ids[0])
            if p15_i > first_general:
                errors.append(f"P15 should outrank general docs: {ids041}")
    if not expl.get("has_finer_scope"):
        errors.append("Q041 explanation should have finer scope")

    # Q042 口径只在开发侧构造同口径查询做冲突排序夹具，不用测试集题干跑生成，也不据此调参。
    fixture_q042 = "SKU-HX-WATCH-S3 的质量保证期限适用哪一条？是否覆盖通用 12 个月规定？"
    inferred42 = infer_scope_from_question(fixture_q042)
    search42 = retrieve(fixture_q042, top_k=40)
    resolution42 = resolve(search42["hits"], {"sku": inferred42.get("sku"), "category": inferred42.get("category")})
    expl42 = explain_conflict(resolution42)
    docs042 = first_docs(resolution42)
    ids042 = [d["doc_id"] for d in docs042]
    if inferred42.get("sku") != "SKU-HX-WATCH-S3":
        errors.append(f"Q042 sku not inferred: {inferred42}")
    if "P08" not in ids042:
        errors.append(f"Q042 applicable docs missing P08: {ids042}")
    if "P07" in ids042 and "P08" in ids042 and ids042.index("P08") > ids042.index("P07"):
        errors.append(f"P08 (sku) should outrank P07 (general): {ids042}")
    if not expl42.get("has_finer_scope") or "覆盖" not in (expl42.get("text") or ""):
        errors.append("Q042 explanation should state sku coverage")
    # 不读取或保存 Q042 的生成答案，本脚本只验证冲突排序。

    report = {
        "ok": not errors,
        "policy_qa_home": str(home),
        "errors": errors,
        "q041": {
            "inferred": inferred,
            "first_docs": docs041,
            "has_finer_scope": expl.get("has_finer_scope"),
            "explanation": expl.get("text"),
        },
        "q042_conflict_fixture_only": {
            "note": "仅验证冲突排序与说明，正式答题数字以测试集第二次跑分为准，不在此调参。",
            "inferred": inferred42,
            "first_docs": docs042,
            "has_finer_scope": expl42.get("has_finer_scope"),
            "explanation": expl42.get("text"),
        },
    }
    out = ROOT / "reports" / "stage5_conflict.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "errors": errors, "q041_docs": ids041, "q042_docs": ids042, "report": str(out)}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
