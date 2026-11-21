#!/usr/bin/env python3
"""第五段离线自检：句级坐标、冲突总序、引用精度口径。不调用生成接口，不写用户电脑目录。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.citation import (  # noqa: E402
    gold_sentences_for_spans,
    overlaps_span,
    score_citations,
    sentence_units,
)
from app.conflict import explain_conflict, infer_scope_from_question, resolve  # noqa: E402
from app.corpus_io import load_corpus, section_index  # noqa: E402


def load_question(qid: str) -> dict:
    for line in (ROOT / "eval" / "questions.jsonl").read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        if item["id"] == qid:
            return item
    raise KeyError(qid)


def _hit(**kwargs) -> dict:
    base = {
        "section_path": "1",
        "char_start": 0,
        "char_end": 10,
        "text": "条款",
        "title": "",
        "effective_date": "2025-01-01",
        "score": 1.0,
        "scope_value": None,
    }
    base.update(kwargs)
    return base


def main() -> int:
    errors: list[str] = []
    index = section_index()
    q041 = load_question("Q041")
    for span in q041["relevant_spans"]:
        sec = index[(span["doc_id"], span["section_path"])]
        units = sentence_units(sec["body"], sec["char_start"])
        if not units:
            errors.append(f"no sentences in {span['doc_id']} {span['section_path']}")
            continue
        if not any(
            overlaps_span(u | {"doc_id": span["doc_id"], "section_path": span["section_path"]}, span)
            for u in units
        ):
            errors.append(f"sentences miss gold span {span}")
        for u in units:
            if u["char_start"] < sec["char_start"] or u["char_end"] > sec["char_end"]:
                errors.append(f"sentence out of section: {u}")

    gold = gold_sentences_for_spans(q041["relevant_spans"], index=index)
    if len(gold) < 3:
        errors.append(f"Q041 gold sentences too few: {len(gold)}")

    ok_cite = dict(gold[0], sid="S1", valid_evidence=True)
    bad_cite = {
        "sid": "S99",
        "valid_evidence": False,
        "doc_id": None,
        "section_path": None,
        "char_start": None,
        "char_end": None,
        "text": None,
    }
    scored = score_citations([ok_cite, bad_cite], q041["relevant_spans"])
    if scored["n_cited"] != 2 or scored["n_correct"] != 1 or scored["precision"] != 0.5:
        errors.append(f"score_citations unexpected: {scored}")

    inferred = infer_scope_from_question(q041["question"])
    if inferred.get("category") != "手机数码":
        errors.append(f"Q041 category not inferred: {inferred}")

    q042 = load_question("Q042")
    inferred42 = infer_scope_from_question(q042["question"])
    if inferred42.get("sku") != "SKU-HX-WATCH-S3":
        errors.append(f"Q042 sku not inferred: {inferred42}")

    # 范围优先：sku → 品类 → 通用；noise 不得进入。
    scope_hits = [
        _hit(
            doc_id="P08",
            source_type="policy",
            scope_type="sku",
            scope_value="SKU-HX-WATCH-S3",
            text="质保 24 个月",
            title="星河手表 S3 专项质保",
            effective_date="2025-03-01",
            score=0.5,
        ),
        _hit(
            doc_id="P-CAT",
            source_type="policy",
            scope_type="category",
            scope_value="智能穿戴",
            text="品类质保 18 个月",
            effective_date="2025-06-01",
            score=0.9,
        ),
        _hit(
            doc_id="P07",
            source_type="policy",
            scope_type="general",
            scope_value=None,
            text="通用质保 12 个月",
            title="通用质保",
            effective_date="2025-09-01",
            score=1.0,
        ),
        _hit(
            doc_id="N07",
            source_type="noise",
            scope_type="sku",
            scope_value="SKU-HX-WATCH-S3",
            text="仅 6 个月且通用优先",
            score=1.0,
        ),
        _hit(
            doc_id="P-OTHER",
            source_type="policy",
            scope_type="sku",
            scope_value="SKU-OTHER",
            text="其他 SKU",
            score=0.8,
        ),
    ]
    sku_resolution = resolve(scope_hits, {"sku": "SKU-HX-WATCH-S3", "category": "智能穿戴"})
    applicable_ids = [x["doc_id"] for x in sku_resolution["applicable"]]
    if applicable_ids[:3] != ["P08", "P-CAT", "P07"]:
        errors.append(f"scope order should be sku>category>general: {applicable_ids}")
    if any(x["doc_id"] in {"N07", "P-OTHER"} and x["applicability"] == "applicable" for x in sku_resolution["applicable"]):
        errors.append("noise or mismatched sku should not be applicable")
    if any(x["doc_id"] == "N07" and x["applicability"] == "applicable" for x in sku_resolution["applicable"] + sku_resolution["excluded"]):
        # excluded is fine; applicable is not
        pass
    if any(x["doc_id"] == "N07" and x["applicability"] == "applicable" for x in sku_resolution["applicable"]):
        errors.append("N07 noise ranked applicable")
    sku_expl = explain_conflict(sku_resolution)
    if not sku_expl["has_finer_scope"] or "SKU" not in sku_expl["text"] or "覆盖" not in sku_expl["text"]:
        errors.append(f"Q042-style explanation missing sku coverage: {sku_expl['text']}")

    # 范围相同再比来源：政策 → 手册 → FAQ。
    source_hits = [
        _hit(doc_id="F01", source_type="faq", scope_type="general", effective_date="2025-12-01", score=1.0),
        _hit(doc_id="M01", source_type="manual", scope_type="general", effective_date="2025-12-01", score=1.0),
        _hit(doc_id="P01", source_type="policy", scope_type="general", effective_date="2025-01-01", score=0.1),
    ]
    source_ids = [x["doc_id"] for x in resolve(source_hits, {})["applicable"]]
    if source_ids != ["P01", "M01", "F01"]:
        errors.append(f"source order should be policy>manual>faq: {source_ids}")

    # 范围与来源均相同再比生效日：较晚者优先。
    date_hits = [
        _hit(doc_id="P-OLD", source_type="policy", scope_type="general", effective_date="2024-01-01", score=1.0),
        _hit(doc_id="P-NEW", source_type="policy", scope_type="general", effective_date="2026-01-01", score=0.1),
    ]
    date_ids = [x["doc_id"] for x in resolve(date_hits, {})["applicable"]]
    if date_ids != ["P-NEW", "P-OLD"]:
        errors.append(f"later effective_date should rank first: {date_ids}")

    # Q041 口径夹具：品类专项优先于通用。
    q041_hits = [
        _hit(
            doc_id="P15",
            section_path="2.激活后的特殊退货规则",
            source_type="policy",
            scope_type="category",
            scope_value="手机数码",
            text="激活后不再支持七天无理由退货。",
            title="手机数码品类专项规定",
            effective_date="2025-04-01",
            score=1.0,
        ),
        _hit(
            doc_id="P02",
            section_path="2.时限计算",
            source_type="policy",
            scope_type="general",
            text="七天无理由时限从签收之日起算。",
            title="七天无理由退货总则",
            effective_date="2025-01-01",
            score=0.8,
        ),
        _hit(
            doc_id="N05",
            section_path="1.误导",
            source_type="noise",
            scope_type="general",
            text="干扰",
            title="干扰",
            score=0.9,
        ),
    ]
    resolution = resolve(q041_hits, {"sku": None, "category": "手机数码"})
    if resolution["applicable"][0]["doc_id"] != "P15":
        errors.append(f"category policy should rank first: {[x['doc_id'] for x in resolution['applicable']]}")
    explanation = explain_conflict(resolution)
    if not explanation["has_finer_scope"] or "品类" not in explanation["text"]:
        errors.append(f"explanation missing category priority: {explanation}")

    n_docs = len(load_corpus())
    if n_docs != 40:
        errors.append(f"corpus size changed: {n_docs}")

    report = {
        "ok": not errors,
        "errors": errors,
        "q041_gold_sentences": len(gold),
        "checks": {
            "scope_order": applicable_ids,
            "source_order": source_ids,
            "effective_date_order": date_ids,
            "q041_first": [x["doc_id"] for x in resolution["applicable"]],
        },
        "n_corpus": n_docs,
    }
    out = ROOT / "reports" / "stage5_units.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
