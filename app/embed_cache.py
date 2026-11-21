"""按文本缓存锁定 embedding 的向量，避免重复调用接口。

缓存目录位于 POLICY_QA_HOME/data/embed_cache，以及仓库内已被 git 忽略的
data/embed_cache。缓存文件不含密钥，也不写入仓库。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.config import Settings, get_settings, repo_root
from app.embeddings import LOCKED_MODEL, EmbeddingError, embed_texts, resolve_model_dim


def _key(text: str, *, model: str, dim: int) -> str:
    payload = f"{model}|{dim}|".encode("utf-8") + text.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def cache_dirs(settings: Settings | None = None) -> list[Path]:
    """第一个目录用于写入；其余只读回退（例如仓库里已有的本地缓存）。"""
    if settings is None:
        settings = get_settings()
    dirs: list[Path] = [settings.data_dir() / "embed_cache"]
    repo_cache = repo_root() / "data" / "embed_cache"
    if repo_cache not in dirs:
        dirs.append(repo_cache)
    return dirs


def _path_in(directory: Path, text: str, *, model: str, dim: int) -> Path:
    return directory / f"{_key(text, model=model, dim=dim)}.json"


def load_one(
    text: str,
    *,
    model: str | None = None,
    dim: int | None = None,
    settings: Settings | None = None,
) -> list[float] | None:
    if settings is None:
        settings = get_settings()
    locked_model, locked_dim = resolve_model_dim(settings)
    model = model or locked_model
    dim = locked_dim if dim is None else int(dim)
    if model != locked_model or dim != locked_dim:
        raise EmbeddingError("embedding_lock_violation")
    for directory in cache_dirs(settings):
        path = _path_in(directory, text, model=model, dim=dim)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        vec = data.get("embedding")
        if (
            not isinstance(vec, list)
            or len(vec) != dim
            or data.get("model") != model
            or int(data.get("dim") or 0) != dim
        ):
            continue
        return [float(x) for x in vec]
    return None


def save_one(
    text: str,
    vec: list[float],
    *,
    model: str | None = None,
    dim: int | None = None,
    settings: Settings | None = None,
) -> None:
    if settings is None:
        settings = get_settings()
    locked_model, locked_dim = resolve_model_dim(settings)
    model = model or locked_model
    dim = locked_dim if dim is None else int(dim)
    if model != locked_model or dim != locked_dim:
        raise EmbeddingError("embedding_lock_violation")
    if len(vec) != dim:
        raise ValueError("cache vector dim mismatch")
    directory = cache_dirs(settings)[0]
    directory.mkdir(parents=True, exist_ok=True)
    path = _path_in(directory, text, model=model, dim=dim)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"model": model, "dim": dim, "embedding": vec},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def embed_texts_cached(
    texts: list[str],
    *,
    model: str | None = None,
    expected_dim: int | None = None,
    settings: Settings | None = None,
) -> list[list[float]]:
    """对未命中缓存的文本调用锁定 embedding，命中的直接返回。"""
    if settings is None:
        settings = get_settings()
    locked_model, locked_dim = resolve_model_dim(settings)
    model = model or locked_model
    expected_dim = locked_dim if expected_dim is None else int(expected_dim)
    if model != locked_model or expected_dim != locked_dim:
        raise EmbeddingError("embedding_lock_violation")
    vectors: list[list[float] | None] = [
        load_one(t, model=model, dim=expected_dim, settings=settings) for t in texts
    ]
    missing_idx = [i for i, v in enumerate(vectors) if v is None]
    if missing_idx:
        missing_texts = [texts[i] for i in missing_idx]
        unique: list[str] = []
        seen: dict[str, int] = {}
        for text in missing_texts:
            if text not in seen:
                seen[text] = len(unique)
                unique.append(text)
        fresh = embed_texts(unique, model=model, expected_dim=expected_dim, settings=settings)
        for text, vec in zip(unique, fresh, strict=True):
            save_one(text, vec, model=model, dim=expected_dim, settings=settings)
        for i in missing_idx:
            vectors[i] = load_one(texts[i], model=model, dim=expected_dim, settings=settings)
    if any(v is None for v in vectors):
        raise EmbeddingError("embedding_cache_incomplete")
    return vectors  # type: ignore[return-value]
