"""默认切分：在章节内部按字符窗口切分，不跨 section_path。

本阶段只做这一次默认切分。三种切分消融留给后续阶段，不在此实现。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# 默认切分参数（写入 B0 报告，不得在本阶段做多组消融）
DEFAULT_CHUNK_SIZE = 400
DEFAULT_CHUNK_OVERLAP = 80
DEFAULT_MIN_TAIL = 40
DEFAULT_STRATEGY = "section_window_chars"


@dataclass(frozen=True)
class ChunkingParams:
    strategy: str = DEFAULT_STRATEGY
    chunk_size: int = DEFAULT_CHUNK_SIZE
    overlap: int = DEFAULT_CHUNK_OVERLAP
    min_tail: int = DEFAULT_MIN_TAIL
    unit: str = "python_unicode_chars"


def window_offsets(length: int, size: int, overlap: int, min_tail: int) -> list[tuple[int, int]]:
    if length <= 0:
        return []
    if length <= size:
        return [(0, length)]
    step = size - overlap
    if step <= 0:
        raise ValueError("chunk_size 必须大于 overlap")
    spans: list[tuple[int, int]] = []
    start = 0
    while start < length:
        end = min(start + size, length)
        spans.append((start, end))
        if end == length:
            break
        start += step
    if len(spans) >= 2:
        last_s, last_e = spans[-1]
        if last_e - last_s < min_tail:
            prev_s, _ = spans[-2]
            spans[-2] = (prev_s, last_e)
            spans.pop()
    return spans


def chunk_sections(
    docs: list[dict],
    params: ChunkingParams | None = None,
) -> list[dict]:
    """按文档章节做窗口切分。chunk 的 char_start/char_end 相对 documents.body。"""
    params = params or ChunkingParams()
    chunks: list[dict] = []
    for doc in docs:
        for sec in doc["sections"]:
            rel_spans = window_offsets(
                len(sec["body"]),
                params.chunk_size,
                params.overlap,
                params.min_tail,
            )
            for rel_start, rel_end in rel_spans:
                abs_start = sec["char_start"] + rel_start
                abs_end = sec["char_start"] + rel_end
                text = sec["body"][rel_start:rel_end]
                chunks.append(
                    {
                        "doc_id": doc["doc_id"],
                        "section_path": sec["section_path"],
                        "char_start": abs_start,
                        "char_end": abs_end,
                        "text": text,
                    }
                )
    return chunks


def params_dict(params: ChunkingParams | None = None) -> dict:
    return asdict(params or ChunkingParams())
