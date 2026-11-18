"""章节内切分。不跨 section_path。

第三段提供三种会得到不同切片集的策略：
- section_window_400：B0 基线，chunk_size=400 / overlap=80 / min_tail=40。
  因全部章节短于 400 字，实际效果是一节一块。
- section_window_120：更小字符窗口，chunk_size=120 / overlap=24 / min_tail=15，
  会在长于 120 字的章节内真正切开。
- sentence_pack_150：按句切分后在章节内打包，直到不超过 max_chars=150；
  单句超过上限则整句单独成块。不跨章节。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# B0 默认切分参数（保留作为消融基线）
DEFAULT_CHUNK_SIZE = 400
DEFAULT_CHUNK_OVERLAP = 80
DEFAULT_MIN_TAIL = 40
DEFAULT_STRATEGY = "section_window_chars"

SENTENCE_DELIMITERS = "。！？；!?;"
_SENT_RE = re.compile(r"[^。！？；!?;]+[。！？；!?;]*")


@dataclass(frozen=True)
class ChunkingParams:
    strategy: str = DEFAULT_STRATEGY
    chunk_size: int = DEFAULT_CHUNK_SIZE
    overlap: int = DEFAULT_CHUNK_OVERLAP
    min_tail: int = DEFAULT_MIN_TAIL
    unit: str = "python_unicode_chars"
    max_chars: int | None = None
    overlap_sentences: int = 0
    sentence_delimiters: str = SENTENCE_DELIMITERS


# 三种消融策略。名称稳定，供脚本与报告引用。
STRATEGY_SPECS: dict[str, ChunkingParams] = {
    "section_window_400": ChunkingParams(
        strategy="section_window_chars",
        chunk_size=400,
        overlap=80,
        min_tail=40,
    ),
    "section_window_120": ChunkingParams(
        strategy="section_window_chars",
        chunk_size=120,
        overlap=24,
        min_tail=15,
    ),
    "sentence_pack_150": ChunkingParams(
        strategy="sentence_pack",
        chunk_size=0,
        overlap=0,
        min_tail=0,
        max_chars=150,
        overlap_sentences=0,
        sentence_delimiters=SENTENCE_DELIMITERS,
    ),
}

ABLATION_STRATEGY_NAMES = (
    "section_window_400",
    "section_window_120",
    "sentence_pack_150",
)


def get_strategy(name: str) -> ChunkingParams:
    if name not in STRATEGY_SPECS:
        known = ", ".join(STRATEGY_SPECS)
        raise ValueError(f"未知切分策略 {name}，可选：{known}")
    return STRATEGY_SPECS[name]


def params_dict(params: ChunkingParams | None = None, *, name: str | None = None) -> dict:
    params = params or ChunkingParams()
    if params.strategy == "sentence_pack":
        return {
            "name": name,
            "strategy": "sentence_pack",
            "max_chars": params.max_chars,
            "overlap_sentences": params.overlap_sentences,
            "sentence_delimiters": params.sentence_delimiters,
            "unit": params.unit,
            "scope": "within_section_path",
        }
    payload = {
        "name": name,
        "strategy": params.strategy,
        "chunk_size": params.chunk_size,
        "overlap": params.overlap,
        "min_tail": params.min_tail,
        "unit": params.unit,
        "scope": "within_section_path",
    }
    return payload


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


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """相对 section.body 的句子区间，并补上前后未被句读覆盖的部分，保证并集等于整节。"""
    if not text:
        return []
    spans = [(m.start(), m.end()) for m in _SENT_RE.finditer(text) if text[m.start() : m.end()].strip()]
    if not spans:
        return [(0, len(text))]
    first_s, first_e = spans[0]
    last_s, last_e = spans[-1]
    spans[0] = (0, first_e)
    spans[-1] = (last_s if len(spans) > 1 else 0, len(text))
    if len(spans) == 1:
        spans[0] = (0, len(text))
    return spans


def pack_sentence_offsets(
    length: int,
    sent_spans: list[tuple[int, int]],
    max_chars: int,
    overlap_sentences: int = 0,
) -> list[tuple[int, int]]:
    if length <= 0:
        return []
    if not sent_spans:
        return [(0, length)]
    packed: list[tuple[int, int]] = []
    i = 0
    n = len(sent_spans)
    while i < n:
        start = sent_spans[i][0]
        end = sent_spans[i][1]
        j = i
        while j + 1 < n and (sent_spans[j + 1][1] - start) <= max_chars:
            j += 1
            end = sent_spans[j][1]
        packed.append((start, end))
        nxt = j + 1 - max(0, overlap_sentences)
        i = max(i + 1, nxt)
    return packed


def chunk_sections(
    docs: list[dict],
    params: ChunkingParams | None = None,
) -> list[dict]:
    """按文档章节切分。chunk 的 char_start/char_end 相对 documents.body。"""
    params = params or ChunkingParams()
    if params.strategy == "sentence_pack":
        return _chunk_sentence_pack(docs, params)
    if params.strategy != "section_window_chars":
        raise ValueError(f"不支持的切分策略: {params.strategy}")
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


def _chunk_sentence_pack(docs: list[dict], params: ChunkingParams) -> list[dict]:
    max_chars = params.max_chars
    if max_chars is None or max_chars <= 0:
        raise ValueError("sentence_pack 需要正整数 max_chars")
    chunks: list[dict] = []
    for doc in docs:
        for sec in doc["sections"]:
            body = sec["body"]
            rel_spans = pack_sentence_offsets(
                len(body),
                sentence_spans(body),
                max_chars,
                params.overlap_sentences,
            )
            for rel_start, rel_end in rel_spans:
                chunks.append(
                    {
                        "doc_id": doc["doc_id"],
                        "section_path": sec["section_path"],
                        "char_start": sec["char_start"] + rel_start,
                        "char_end": sec["char_start"] + rel_end,
                        "text": body[rel_start:rel_end],
                    }
                )
    return chunks


def chunk_key(ch: dict) -> tuple:
    return (ch["doc_id"], ch["section_path"], ch["char_start"], ch["char_end"])
