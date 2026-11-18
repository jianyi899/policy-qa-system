#!/usr/bin/env python3
"""根据 question_bank 回填条款字符起止，写出评测集并封存测试集 sha256。"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.corpus_io import section_index  # noqa: E402
from eval.question_bank import QUESTIONS, validate_bank  # noqa: E402

OUT_DIR = ROOT / "eval"
QUESTIONS_PATH = OUT_DIR / "questions.jsonl"
SPLITS_PATH = OUT_DIR / "splits.json"
SEAL_PATH = OUT_DIR / "testset_seal.json"
HASH_PATH = OUT_DIR / "testset.sha256"


def stable_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_payload(questions: list[dict]) -> list[dict]:
    items = []
    for q in questions:
        if q["split"] != "test":
            continue
        spans = [
            {
                "char_end": s["char_end"],
                "char_start": s["char_start"],
                "doc_id": s["doc_id"],
                "relevance": s["relevance"],
                "section_path": s["section_path"],
            }
            for s in sorted(
                q["relevant_spans"],
                key=lambda s: (s["doc_id"], s["section_path"], s["char_start"], s["char_end"]),
            )
        ]
        items.append(
            {
                "answer_points": q["answer_points"],
                "cluster": q["cluster"],
                "id": q["id"],
                "question": q["question"],
                "relevant_spans": spans,
                "should_refuse": q["should_refuse"],
                "type": q["type"],
            }
        )
    items.sort(key=lambda x: x["id"])
    return items


def main() -> int:
    validate_bank()
    index = section_index()
    compiled = []
    for q in QUESTIONS:
        spans = []
        for spec in q["span_specs"]:
            key = (spec["doc_id"], spec["section_path"])
            if key not in index:
                known = [sp for (d, sp) in index if d == spec["doc_id"]]
                raise SystemExit(
                    f"{q['id']} 找不到条款 {spec['doc_id']} {spec['section_path']}; 已知: {known}"
                )
            sec = index[key]
            spans.append(
                {
                    "doc_id": sec["doc_id"],
                    "section_path": sec["section_path"],
                    "char_start": sec["char_start"],
                    "char_end": sec["char_end"],
                    "relevance": 1,
                }
            )
        item = {
            "id": q["id"],
            "type": q["type"],
            "cluster": q["cluster"],
            "split": q["split"],
            "question": q["question"],
            "answer_points": q["answer_points"],
            "should_refuse": q["should_refuse"],
            "relevant_spans": spans,
        }
        compiled.append(item)

    compiled.sort(key=lambda x: x["id"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with QUESTIONS_PATH.open("w", encoding="utf-8") as fh:
        for item in compiled:
            fh.write(stable_dumps(item) + "\n")

    dev_ids = [q["id"] for q in compiled if q["split"] == "dev"]
    test_ids = [q["id"] for q in compiled if q["split"] == "test"]
    clusters = {}
    for q in compiled:
        clusters.setdefault(q["cluster"], {"split": q["split"], "ids": []})
        clusters[q["cluster"]]["ids"].append(q["id"])
    type_counts = Counter(q["type"] for q in compiled)
    split_counts = Counter(q["split"] for q in compiled)
    splits = {
        "method": "按文档簇整簇划分，不按题号随机切分。Q041 所在手机数码簇归开发集，Q042 所在质保期限簇归测试集。",
        "dev_ids": dev_ids,
        "test_ids": test_ids,
        "n_dev": len(dev_ids),
        "n_test": len(test_ids),
        "type_counts": dict(type_counts),
        "split_counts": dict(split_counts),
        "clusters": clusters,
        "locked_ids": {
            "Q041": "dev",
            "Q042": "test",
            "Q039": "dev_answerable_negative_not_refusal",
            "Q025": "dev",
        },
    }
    SPLITS_PATH.write_text(json.dumps(splits, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    payload = test_payload(compiled)
    canonical = stable_dumps(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    seal = {
        "algorithm": "sha256",
        "encoding": "utf-8",
        "serialization": "json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))",
        "scope": "测试集 30 题的稳定字段：id,type,cluster,question,answer_points,should_refuse,relevant_spans。不含 split 以外的运行时字段。题目按 id 排序，跨度按 doc_id/section_path/起止排序。",
        "n_test_questions": len(payload),
        "sha256": digest,
        "note": "本阶段不在测试集上跑评测、不用测试集数字调参。若此后改动测试集，必须在提交说明中写明原因并更新本文件。",
    }
    SEAL_PATH.write_text(json.dumps(seal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    HASH_PATH.write_text(digest + "\n", encoding="utf-8")

    print(json.dumps(
        {
            "ok": True,
            "n": len(compiled),
            "type_counts": dict(type_counts),
            "split_counts": dict(split_counts),
            "testset_sha256": digest,
            "questions": str(QUESTIONS_PATH),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
