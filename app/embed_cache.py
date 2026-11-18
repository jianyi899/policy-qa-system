"""按文本缓存锁定 embedding 的向量，避免重复调用接口。

缓存目录位于 data/（已被 git 忽略）。缓存文件不含密钥，也不写入仓库。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.embeddings import LOCKED_DIM, LOCKED_MODEL, embed_texts

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "embed_cache"


def _key(text: str) -> str:
    payload = f"{LOCKED_MODEL}|{LOCKED_DIM}|".encode("utf-8") + text.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def load_one(text: str) -> list[float] | None:
    path = _path(_key(text))
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    vec = data.get("embedding")
    if (
        not isinstance(vec, list)
        or len(vec) != LOCKED_DIM
        or data.get("model") != LOCKED_MODEL
        or data.get("dim") != LOCKED_DIM
    ):
        return None
    return [float(x) for x in vec]


def save_one(text: str, vec: list[float]) -> None:
    if len(vec) != LOCKED_DIM:
        raise ValueError("cache vector dim mismatch")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(_key(text))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"model": LOCKED_MODEL, "dim": LOCKED_DIM, "embedding": vec},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def ingest_pairs(pairs: list[tuple[str, list[float]]]) -> int:
    """把已有 (text, vector) 写入缓存，已存在则跳过。返回新写入条数。"""
    written = 0
    for text, vec in pairs:
        if load_one(text) is None:
            save_one(text, vec)
            written += 1
    return written


def parse_vector_literal(lit: str) -> list[float]:
    s = lit.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if not s:
        return []
    return [float(x) for x in s.split(",")]


def embed_texts_cached(
    texts: list[str],
    *,
    model: str = LOCKED_MODEL,
    expected_dim: int = LOCKED_DIM,
) -> list[list[float]]:
    """对未命中缓存的文本调用锁定 embedding，命中的直接返回。"""
    vectors: list[list[float] | None] = [load_one(t) for t in texts]
    missing_idx = [i for i, v in enumerate(vectors) if v is None]
    if missing_idx:
        missing_texts = [texts[i] for i in missing_idx]
        # 去重后再请求，相同文本只算一次。
        unique: list[str] = []
        seen: dict[str, int] = {}
        for t in missing_texts:
            if t not in seen:
                seen[t] = len(unique)
                unique.append(t)
        fresh = embed_texts(unique, model=model, expected_dim=expected_dim)
        for t, vec in zip(unique, fresh, strict=True):
            save_one(t, vec)
        for i in missing_idx:
            vectors[i] = load_one(texts[i])
    if any(v is None for v in vectors):
        raise RuntimeError("embedding cache incomplete after fill")
    return vectors  # type: ignore[return-value]


