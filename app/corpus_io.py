"""语料 Markdown 解析与章节切分（与导入、标注共用，保证偏移一致）。"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.config import repo_root

ROOT = repo_root()
CORPUS_DIRS = [
    ROOT / "corpus" / "policies",
    ROOT / "corpus" / "manuals",
    ROOT / "corpus" / "faqs",
    ROOT / "corpus" / "noise",
]
# 第二段 40 篇：政策 15 + 手册 10 + FAQ 9 + 干扰 6（含 N07 投毒）。
EXPECTED_DOCS = (
    "P01",
    "P02",
    "P03",
    "P04",
    "P05",
    "P06",
    "P07",
    "P08",
    "P10",
    "P12",
    "P14",
    "P15",
    "P16",
    "P17",
    "P19",
    "M01",
    "M02",
    "M03",
    "M04",
    "M05",
    "M09",
    "M13",
    "M14",
    "M16",
    "M23",
    "F01",
    "F02",
    "F03",
    "F04",
    "F05",
    "F06",
    "F07",
    "F10",
    "F12",
    "N01",
    "N02",
    "N03",
    "N04",
    "N05",
    "N07",
)
HEADING_RE = re.compile(r"^## (\d+)[ \t]+(.+?)\s*$", re.MULTILINE)
FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def normalize_body(body: str) -> str:
    if body.startswith("\n"):
        body = body[1:]
    body = body.replace("\r\n", "\n")
    if not body.endswith("\n"):
        body += "\n"
    return body


def parse_markdown(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    match = FRONT_MATTER_RE.match(raw)
    if not match:
        raise ValueError(f"{path} 缺少 YAML front matter")
    meta = yaml.safe_load(match.group(1)) or {}
    body = normalize_body(match.group(2))
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


def iter_corpus_files() -> list[Path]:
    files: list[Path] = []
    for folder in CORPUS_DIRS:
        if folder.is_dir():
            files.extend(sorted(folder.glob("*.md")))
    return sorted(files)


def load_corpus() -> list[dict]:
    files = iter_corpus_files()
    docs = [parse_markdown(p) for p in files]
    ids = [d["doc_id"] for d in docs]
    if sorted(ids) != sorted(EXPECTED_DOCS):
        missing = [i for i in EXPECTED_DOCS if i not in ids]
        extra = [i for i in ids if i not in EXPECTED_DOCS]
        raise ValueError(f"doc_id 不完整 missing={missing} extra={extra}")
    for doc in docs:
        doc["sections"] = split_sections(doc["body"])
    return docs


def section_index(docs: list[dict] | None = None) -> dict[tuple[str, str], dict]:
    docs = docs if docs is not None else load_corpus()
    index: dict[tuple[str, str], dict] = {}
    for doc in docs:
        for sec in doc["sections"]:
            index[(doc["doc_id"], sec["section_path"])] = {
                "doc_id": doc["doc_id"],
                "section_path": sec["section_path"],
                "char_start": sec["char_start"],
                "char_end": sec["char_end"],
                "body": sec["body"],
            }
    return index
