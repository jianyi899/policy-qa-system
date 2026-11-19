"""句级引用：把回答落到 doc_id + section_path + 句子跨度，并映射回条款标注的字符起止。

坐标与条款标注、切片一致，均相对 documents.body。
不对着检索结果改标注，也不直接使用 chunk_id 作为引用键。
句子切分与第三段 chunking.sentence_spans 使用同一套句读符。
"""

from __future__ import annotations

import re

from app.chunking import sentence_spans
from app.corpus_io import section_index

CITE_RE = re.compile(r"\[(S\d+)\]", re.IGNORECASE)
HEADING_RE = re.compile(r"^##\s+")


def trim_span(text: str, rel_start: int, rel_end: int) -> tuple[int, int, str]:
    """去掉首尾空白后返回相对 text 的新区间与正文。"""
    raw = text[rel_start:rel_end]
    lead = len(raw) - len(raw.lstrip())
    trail = len(raw) - len(raw.rstrip())
    if lead + trail >= len(raw):
        return rel_start, rel_start, ""
    a = rel_start + lead
    b = rel_end - trail
    return a, b, text[a:b]


def content_segments(text: str) -> list[tuple[int, int]]:
    """跳过 Markdown 二级标题行和空行，返回正文片段的相对区间。"""
    segments: list[tuple[int, int]] = []
    pos = 0
    cur: int | None = None
    n = len(text)
    while pos < n:
        nl = text.find("\n", pos)
        line_end = n if nl < 0 else nl + 1
        line = text[pos:line_end]
        skip = bool(HEADING_RE.match(line)) or (not line.strip())
        if skip:
            if cur is not None:
                segments.append((cur, pos))
                cur = None
        elif cur is None:
            cur = pos
        pos = line_end
    if cur is not None:
        segments.append((cur, n))
    return segments


def sentence_units(text: str, base_offset: int) -> list[dict]:
    """把一段文本切成句子，char_start/char_end 为 documents.body 坐标。"""
    units: list[dict] = []
    if not text:
        return units
    for seg_s, seg_e in content_segments(text):
        frag = text[seg_s:seg_e]
        for rel_s, rel_e in sentence_spans(frag):
            a, b, piece = trim_span(frag, rel_s, rel_e)
            if not piece:
                continue
            units.append(
                {
                    "char_start": base_offset + seg_s + a,
                    "char_end": base_offset + seg_s + b,
                    "text": piece,
                }
            )
    return units


def units_from_chunk(chunk: dict) -> list[dict]:
    """从检索/冲突切片切句。切片坐标已是文档级。"""
    text = chunk.get("text") or ""
    base = int(chunk.get("char_start") or 0)
    out = []
    for unit in sentence_units(text, base):
        item = dict(unit)
        item["doc_id"] = chunk.get("doc_id")
        item["section_path"] = chunk.get("section_path")
        item["source_type"] = chunk.get("source_type")
        item["scope_type"] = chunk.get("scope_type")
        item["scope_value"] = chunk.get("scope_value")
        item["scope_match"] = chunk.get("scope_match")
        item["conflict_rank"] = chunk.get("conflict_rank")
        item["title"] = chunk.get("title")
        out.append(item)
    return out


def evidence_sentences(chunks: list[dict], *, max_sentences: int = 24) -> list[dict]:
    """按冲突序展开证据句并编号 S1…，同一跨度只保留一次。"""
    seen: set[tuple] = set()
    sentences: list[dict] = []
    for ch in chunks:
        for unit in units_from_chunk(ch):
            key = (unit["doc_id"], unit["section_path"], unit["char_start"], unit["char_end"])
            if key in seen:
                continue
            seen.add(key)
            sentences.append(unit)
            if len(sentences) >= max_sentences:
                break
        if len(sentences) >= max_sentences:
            break
    for i, item in enumerate(sentences, start=1):
        item["sid"] = f"S{i}"
    return sentences


def format_evidence_block(sentences: list[dict]) -> str:
    lines = []
    for s in sentences:
        lines.append(
            f"[{s['sid']}] {s.get('doc_id')} / {s.get('section_path')} / "
            f"{s['char_start']}-{s['char_end']}"
        )
        lines.append(s.get("text") or "")
        lines.append("")
    return "\n".join(lines).strip()


def parse_citation_ids(answer: str) -> list[str]:
    """从回答中解析 [S1] 形式的引用编号，去重且保持首次出现顺序。"""
    seen: set[str] = set()
    ids: list[str] = []
    for match in CITE_RE.finditer(answer or ""):
        sid = match.group(1).upper()
        if sid not in seen:
            seen.add(sid)
            ids.append(sid)
    return ids


def resolve_citations(answer: str, sentences: list[dict]) -> list[dict]:
    """把回答里的编号映射为可核对的句级跨度。未知编号记为 invalid。"""
    by_sid = {s["sid"]: s for s in sentences}
    cited = []
    for sid in parse_citation_ids(answer):
        src = by_sid.get(sid)
        if src is None:
            cited.append(
                {
                    "sid": sid,
                    "valid_evidence": False,
                    "doc_id": None,
                    "section_path": None,
                    "char_start": None,
                    "char_end": None,
                    "text": None,
                }
            )
            continue
        cited.append(
            {
                "sid": sid,
                "valid_evidence": True,
                "doc_id": src["doc_id"],
                "section_path": src["section_path"],
                "char_start": src["char_start"],
                "char_end": src["char_end"],
                "text": src["text"],
            }
        )
    return cited


def overlaps_span(sent: dict, span: dict) -> bool:
    if sent.get("doc_id") != span.get("doc_id"):
        return False
    if sent.get("section_path") != span.get("section_path"):
        return False
    return max(0, min(sent["char_end"], span["char_end"]) - max(sent["char_start"], span["char_start"])) > 0


def gold_sentences_for_spans(spans: list[dict], *, index: dict | None = None) -> list[dict]:
    """把条款跨度展开为金标句子。标注本身仍是条款跨度，不在这里改标注。"""
    index = index if index is not None else section_index()
    gold: list[dict] = []
    seen: set[tuple] = set()
    for span in spans or []:
        sec = index.get((span["doc_id"], span["section_path"]))
        if not sec:
            continue
        for unit in sentence_units(sec["body"], sec["char_start"]):
            sent = {
                "doc_id": span["doc_id"],
                "section_path": span["section_path"],
                "char_start": unit["char_start"],
                "char_end": unit["char_end"],
                "text": unit["text"],
            }
            if not overlaps_span(sent, span):
                continue
            key = (sent["doc_id"], sent["section_path"], sent["char_start"], sent["char_end"])
            if key in seen:
                continue
            seen.add(key)
            gold.append(sent)
    return gold


def score_citations(cited: list[dict], gold_spans: list[dict]) -> dict:
    """按句子计算引用精度。

    一条系统引用计为正确，当且仅当：编号能映射到证据句，且该句与任一条金标条款跨度
    在同一 doc_id + section_path 下字符交集长度大于 0。无效编号计入分母、不计正确。
    精度 = 正确引用句数 / 系统引用句数（按编号去重）。0 条引用时该题精度记为缺失，不进宏平均分子，
    同时另报把缺引用视为 0 的口径。
    """
    gold_keys = {
        (g["doc_id"], g["section_path"], g["char_start"], g["char_end"])
        for g in gold_sentences_for_spans(gold_spans)
    }
    n_cited = len(cited)
    correct = []
    incorrect = []
    for item in cited:
        ok = bool(item.get("valid_evidence")) and any(overlaps_span(item, span) for span in (gold_spans or []))
        rec = dict(item)
        rec["correct"] = ok
        if ok:
            correct.append(rec)
        else:
            incorrect.append(rec)
    precision = (len(correct) / n_cited) if n_cited else None
    # 句级召回：金标句中被引用命中的比例（按跨度键）。
    cited_keys = {
        (c["doc_id"], c["section_path"], c["char_start"], c["char_end"])
        for c in cited
        if c.get("valid_evidence")
    }
    if gold_keys:
        n_gold_hit = len(gold_keys & cited_keys)
        recall = n_gold_hit / len(gold_keys)
    else:
        n_gold_hit = 0
        recall = None
    return {
        "n_cited": n_cited,
        "n_correct": len(correct),
        "n_incorrect": len(incorrect),
        "precision": precision,
        "n_gold_sentences": len(gold_keys),
        "n_gold_hit": n_gold_hit,
        "recall": recall,
        "correct": correct,
        "incorrect": incorrect,
        "definition": (
            "引用精度按句子算：系统引用的句子中，与金标条款跨度在同一文档章节下"
            "字符交集长度>0 的比例。金标仍是条款跨度，不改为 chunk_id，也不对着检索改标注。"
        ),
    }
