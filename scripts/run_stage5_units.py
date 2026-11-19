#!/usr/bin/env python3
"""第五段离线自检：句级坐标、冲突推断、引用精度口径。不调用生成接口。"""

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


def main() -> int:
    errors: list[str] = []
    index = section_index()
    q041 = load_question("Q041")
    # 坐标必须与条款标注同一套 documents.body 坐标系。
    for span in q041["relevant_spans"]:
        sec = index[(span["doc_id"], span["section_path"])]
        if span["char_start"] != sec["char_start"] or span["char_end"] != sec["char_end"]:
            # Q041 的三条跨度正好是整节；若不是也不算失败，但要能重叠。
            pass
        units = sentence_units(sec["body"], sec["char_start"])
        if not units:
            errors.append(f"no sentences in {span['doc_id']} {span['section_path']}")
            continue
        if not any(overlaps_span(u | {"doc_id": span["doc_id"], "section_path": span["section_path"]}, span) for u in units):
            errors.append(f"sentences miss gold span {span}")
        # 句子不得越出章节
        for u in units:
            if u["char_start"] < sec["char_start"] or u["char_end"] > sec["char_end"]:
                errors.append(f"sentence out of section: {u}")

    gold = gold_sentences_for_spans(q041["relevant_spans"], index=index)
    if len(gold) < 3:
        errors.append(f"Q041 gold sentences too few: {len(gold)}")

    # 精度口径：命中金标节的句子算对，编造跨度算错。
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

    # 用假切片验证品类优先说明。
    fake_hits = [
        {
            "doc_id": "P15",
            "section_path": "2.激活后的特殊退货规则",
            "char_start": 101,
            "char_end": 235,
            "text": "激活后不再支持七天无理由退货。",
            "title": "手机数码品类专项规定",
            "source_type": "policy",
            "scope_type": "category",
            "scope_value": "手机数码",
            "effective_date": "2025-04-01",
            "score": 1.0,
        },
        {
            "doc_id": "P02",
            "section_path": "2.时限计算",
            "char_start": 97,
            "char_end": 315,
            "text": "七天无理由时限从签收之日起算。",
            "title": "七天无理由退货总则",
            "source_type": "policy",
            "scope_type": "general",
            "scope_value": None,
            "effective_date": "2025-01-01",
            "score": 0.8,
        },
        {
            "doc_id": "N05",
            "section_path": "1.误导",
            "char_start": 0,
            "char_end": 10,
            "text": "干扰",
            "title": "干扰",
            "source_type": "noise",
            "scope_type": "general",
            "scope_value": None,
            "effective_date": "2025-01-01",
            "score": 0.9,
        },
    ]
    resolution = resolve(fake_hits, {"sku": None, "category": "手机数码"})
    if any(x["doc_id"] == "N05" and x["applicability"] == "applicable" for x in resolution["applicable"]):
        errors.append("noise should not be applicable")
    if resolution["applicable"][0]["doc_id"] != "P15":
        errors.append(f"category policy should rank first: {[x['doc_id'] for x in resolution['applicable']]}")
    explanation = explain_conflict(resolution)
    if not explanation["has_finer_scope"] or "品类" not in explanation["text"]:
        errors.append(f"explanation missing category priority: {explanation}")

    # 语料规模未改
    if len(load_corpus()) != 70:
        errors.append("corpus size changed")

    report = {"ok": not errors, "errors": errors, "q041_gold_sentences": len(gold)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
