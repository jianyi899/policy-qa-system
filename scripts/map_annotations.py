#!/usr/bin/env python3
"""将条款跨度标注映射到当前切分后的切片。每次重切分后必须先运行本脚本再评测。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.annotation import map_spans_to_chunks  # noqa: E402
from app.config import get_settings  # noqa: E402

OUT_PATH = ROOT / "eval" / "annotation_mapping.json"


def load_jsonl(path: Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(json.loads(line))
    return items


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=OUT_PATH, help="映射结果输出路径")
    args = parser.parse_args()
    out_path = args.out
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    questions = load_jsonl(ROOT / "eval" / "questions.jsonl")
    with psycopg.connect(settings.dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, doc_id, section_path, char_start, char_end, text
                FROM chunks
                ORDER BY doc_id, section_path, char_start
                """
            )
            chunks = [
                {
                    "id": r[0],
                    "doc_id": r[1],
                    "section_path": r[2],
                    "char_start": r[3],
                    "char_end": r[4],
                    "text": r[5],
                }
                for r in cur.fetchall()
            ]
    if not chunks:
        print(json.dumps({"ok": False, "error_type": "no_chunks"}, ensure_ascii=False))
        return 3
    mapping = map_spans_to_chunks(questions, chunks)
    mapping["n_chunks"] = len(chunks)
    # 开发集单独统计，便于 B0 报告引用
    dev_items = [it for it in mapping["items"] if it.get("split") == "dev" and it["type"] != "refusal"]
    mapping["dev_answerable"] = {
        "n_questions": len(dev_items),
        "n_gold_spans": sum(it["n_spans"] for it in dev_items),
        "n_mapped_question_chunk_pairs": sum(len(it["gold_chunks"]) for it in dev_items),
        "n_questions_missing_gold": sum(1 for it in dev_items if not it["gold_chunks"]),
    }
    test_items = [it for it in mapping["items"] if it.get("split") == "test" and it["type"] != "refusal"]
    mapping["test_answerable"] = {
        "n_questions": len(test_items),
        "n_gold_spans": sum(it["n_spans"] for it in test_items),
        "n_mapped_question_chunk_pairs": sum(len(it["gold_chunks"]) for it in test_items),
        "n_questions_missing_gold": sum(1 for it in test_items if not it["gold_chunks"]),
    }
    mapping["ok"] = mapping["n_unmapped_spans"] == 0 and mapping["dev_answerable"]["n_questions_missing_gold"] == 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {k: mapping[k] for k in mapping if k != "items"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if mapping["ok"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
