"""测试集封存哈希的校验。不改写题集，只读 questions.jsonl 与 testset_seal.json。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "eval"
QUESTIONS_PATH = EVAL_DIR / "questions.jsonl"
SEAL_PATH = EVAL_DIR / "testset_seal.json"
HASH_PATH = EVAL_DIR / "testset.sha256"
EXPECTED_SHA256 = "092576646b41d9c35a3390e330ea62c2e62b0f00ab4ab78a32cb0220b3fc6a59"

STABLE_FIELDS = (
    "id",
    "type",
    "cluster",
    "question",
    "answer_points",
    "should_refuse",
    "relevant_spans",
)


def stable_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_jsonl(path: Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(json.loads(line))
    return items


def test_payload(questions: list[dict]) -> list[dict]:
    items = []
    for q in questions:
        if q.get("split") != "test":
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
                q.get("relevant_spans") or [],
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


def compute_sha256(questions: list[dict] | None = None) -> str:
    if questions is None:
        questions = load_jsonl(QUESTIONS_PATH)
    canonical = stable_dumps(test_payload(questions))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_testset_seal() -> dict:
    observed = compute_sha256()
    seal = json.loads(SEAL_PATH.read_text(encoding="utf-8"))
    sealed = seal.get("sha256")
    hash_file = HASH_PATH.read_text(encoding="utf-8").strip().split()[0]
    match = observed == EXPECTED_SHA256 and sealed == EXPECTED_SHA256 and hash_file == EXPECTED_SHA256
    return {
        "ok": match,
        "expected_sha256": EXPECTED_SHA256,
        "observed_sha256": observed,
        "seal_file_sha256": sealed,
        "hash_file_sha256": hash_file,
        "n_test_questions": seal.get("n_test_questions"),
        "match": match,
    }
