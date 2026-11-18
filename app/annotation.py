"""条款跨度到切片的映射。不对着检索结果标注，不直接使用 chunk_id。

映射规则（锁定）：
切片 C 与条款跨度 S 的字符交集长度 / len(S) > 0.5 时，C 继承 S 的 relevance。
长度为 0 的跨度不产生映射。每次重切分都必须先运行本映射再评测。
"""

from __future__ import annotations

OVERLAP_THRESHOLD = 0.5


def overlap_len(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def inherits_relevance(
    chunk_start: int,
    chunk_end: int,
    span_start: int,
    span_end: int,
    threshold: float = OVERLAP_THRESHOLD,
) -> bool:
    span_len = span_end - span_start
    if span_len <= 0:
        return False
    inter = overlap_len(chunk_start, chunk_end, span_start, span_end)
    return (inter / span_len) > threshold


def map_spans_to_chunks(
    questions: list[dict],
    chunks: list[dict],
    threshold: float = OVERLAP_THRESHOLD,
) -> dict:
    """为每道题计算继承了 relevance 的切片。

    chunks 元素需含 doc_id, section_path, char_start, char_end，以及可选的 id。
    返回 mapping 与统计，不修改标注源。
    """
    by_doc: dict[str, list[dict]] = {}
    for ch in chunks:
        by_doc.setdefault(ch["doc_id"], []).append(ch)

    mapped: list[dict] = []
    n_gold_spans = 0
    n_gold_chunks = 0
    questions_with_gold = 0
    unmapped_spans = []

    for q in questions:
        spans = q.get("relevant_spans") or []
        n_gold_spans += len(spans)
        seen: dict[tuple, dict] = {}
        for span in spans:
            doc_id = span["doc_id"]
            matched = []
            for ch in by_doc.get(doc_id, []):
                if ch["section_path"] != span["section_path"]:
                    # 条款跨度带有 section_path；只在同一章节内映射。
                    continue
                if inherits_relevance(
                    ch["char_start"],
                    ch["char_end"],
                    span["char_start"],
                    span["char_end"],
                    threshold,
                ):
                    key = (
                        ch["doc_id"],
                        ch["section_path"],
                        ch["char_start"],
                        ch["char_end"],
                    )
                    rec = {
                        "doc_id": ch["doc_id"],
                        "section_path": ch["section_path"],
                        "char_start": ch["char_start"],
                        "char_end": ch["char_end"],
                        "chunk_id": ch.get("id"),
                        "relevance": span.get("relevance", 1),
                    }
                    # 同一切片被多条跨度命中时保留较高 relevance
                    prev = seen.get(key)
                    if prev is None or rec["relevance"] > prev["relevance"]:
                        seen[key] = rec
                    matched.append(rec)
            if not matched:
                unmapped_spans.append(
                    {
                        "question_id": q["id"],
                        "doc_id": span["doc_id"],
                        "section_path": span["section_path"],
                        "char_start": span["char_start"],
                        "char_end": span["char_end"],
                    }
                )
        gold = list(seen.values())
        if gold:
            questions_with_gold += 1
        n_gold_chunks += len(gold)
        mapped.append(
            {
                "question_id": q["id"],
                "split": q.get("split"),
                "type": q.get("type"),
                "n_spans": len(spans),
                "gold_chunks": gold,
            }
        )

    unique_chunk_keys = {
        (c["doc_id"], c["section_path"], c["char_start"], c["char_end"])
        for item in mapped
        for c in item["gold_chunks"]
    }
    return {
        "rule": "overlap_len / span_len > 0.5；同一文档同一 section_path 内比较字符区间",
        "threshold": threshold,
        "n_questions": len(questions),
        "n_gold_spans": n_gold_spans,
        "n_mapped_question_chunk_pairs": n_gold_chunks,
        "n_unique_relevant_chunks": len(unique_chunk_keys),
        "n_questions_with_gold_chunks": questions_with_gold,
        "n_unmapped_spans": len(unmapped_spans),
        "unmapped_spans": unmapped_spans,
        "items": mapped,
    }
