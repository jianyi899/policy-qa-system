#!/usr/bin/env python3
"""导入 25 篇语料，并按稳定二级标题切分为 document_sections / chunks。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import psycopg
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402

CORPUS_DIRS = [
    ROOT / "corpus" / "policies",
    ROOT / "corpus" / "manuals",
]
HEADING_RE = re.compile(r"^## (\d+)[ \t]+(.+?)\s*$", re.MULTILINE)
FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_markdown(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    match = FRONT_MATTER_RE.match(raw)
    if not match:
        raise ValueError(f"{path} 缺少 YAML front matter")
    meta = yaml.safe_load(match.group(1)) or {}
    body = match.group(2)
    if body.startswith("\n"):
        body = body[1:]
    # 统一换行，保证偏移稳定
    body = body.replace("\r\n", "\n")
    if not body.endswith("\n"):
        body += "\n"
    required = ["doc_id", "title", "source_type", "scope_type", "effective_date"]
    missing = [k for k in required if not meta.get(k)]
    if missing:
        raise ValueError(f"{path} 缺少字段: {missing}")
    scope_value = meta.get("scope_value") or None
    expire_date = meta.get("expire_date") or None
    return {
        "doc_id": str(meta["doc_id"]),
        "title": str(meta["title"]),
        "source_type": str(meta["source_type"]),
        "scope_type": str(meta["scope_type"]),
        "scope_value": scope_value,
        "effective_date": str(meta["effective_date"]),
        "expire_date": str(expire_date) if expire_date else None,
        "body": body,
        "path": path,
    }


def split_sections(body: str) -> list[dict]:
    matches = list(HEADING_RE.finditer(body))
    if not matches:
        raise ValueError("文档缺少二级标题，无法生成稳定 section_path")
    sections: list[dict] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        number, heading = match.group(1), match.group(2).strip()
        section_path = f"{number}.{heading}"
        sections.append(
            {
                "section_path": section_path,
                "heading": heading,
                "char_start": start,
                "char_end": end,
                "body": body[start:end],
            }
        )
    return sections


def import_corpus() -> None:
    load_dotenv(ROOT / ".env")
    settings = get_settings()
    files = sorted(p for d in CORPUS_DIRS for p in d.glob("*.md"))
    if len(files) != 25:
        raise SystemExit(f"期望 25 篇语料，实际找到 {len(files)} 篇")

    docs = [parse_markdown(p) for p in files]
    ids = [d["doc_id"] for d in docs]
    expected = [f"P{i:02d}" for i in range(1, 21)] + [f"M{i:02d}" for i in range(1, 6)]
    if sorted(ids) != sorted(expected):
        raise SystemExit(f"doc_id 不完整: {sorted(ids)}")

    with psycopg.connect(settings.dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chunks")
            cur.execute("DELETE FROM document_sections")
            cur.execute("DELETE FROM documents")
            for doc in docs:
                sections = split_sections(doc["body"])
                cur.execute(
                    """
                    INSERT INTO documents (
                        doc_id, title, source_type, scope_type, scope_value,
                        effective_date, expire_date, body
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        doc["doc_id"],
                        doc["title"],
                        doc["source_type"],
                        doc["scope_type"],
                        doc["scope_value"],
                        doc["effective_date"],
                        doc["expire_date"],
                        doc["body"],
                    ),
                )
                for sec in sections:
                    cur.execute(
                        """
                        INSERT INTO document_sections (
                            doc_id, section_path, heading, char_start, char_end, body
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            doc["doc_id"],
                            sec["section_path"],
                            sec["heading"],
                            sec["char_start"],
                            sec["char_end"],
                            sec["body"],
                        ),
                    )
                    # 第一段按章节切分，一块对应一个稳定 section_path，便于后续按字符起止标注。
                    cur.execute(
                        """
                        INSERT INTO chunks (
                            doc_id, section_path, char_start, char_end, text
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            doc["doc_id"],
                            sec["section_path"],
                            sec["char_start"],
                            sec["char_end"],
                            sec["body"],
                        ),
                    )
        conn.commit()

    print(f"imported {len(docs)} documents")
    for doc in docs:
        print(f"  {doc['doc_id']} {doc['title']}")


if __name__ == "__main__":
    import_corpus()
