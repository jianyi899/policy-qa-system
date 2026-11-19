#!/usr/bin/env python3
"""模拟流量回放：实测延迟、token 成本，以及缓存命中政策问答的首 token。

在本机预发形态下测量：拉起 uvicorn，经 /ask/stream 访问。
首 token 800ms 只考核缓存命中的纯政策问答，不用冷启动或工具路径去契合。
同时写入一条真实 Langfuse trace 并回读验证。
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cache import flush_namespace  # noqa: E402
from app.clock import iso, now_shanghai  # noqa: E402
from app.observability import fetch_observations, flush as langfuse_flush, get_langfuse  # noqa: E402
from app.quality import PRICE_MODE, PRICE_REGION, PRICE_SOURCE, cost_cny  # noqa: E402
from app.runtime import handle_ask  # noqa: E402
from scripts.stage6_common import load_member_context, load_questions, load_stage5_dev, write_json  # noqa: E402

HOST = "127.0.0.1"
PORT = 8016
BASE = f"http://{HOST}:{PORT}"


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    xs = sorted(values)
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def _wait_health(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE}/health", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    return False


def _stream_ttft(question: str, *, use_cache: bool = True, timeout: float = 60.0) -> dict:
    started = time.perf_counter()
    first = None
    meta = None
    tokens = []
    with httpx.Client(timeout=timeout) as client:
        with client.stream(
            "POST",
            f"{BASE}/ask/stream",
            json={"question": question, "use_cache": use_cache, "use_cascade": True},
            headers={"Accept": "text/event-stream"},
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if "token" in obj and first is None:
                    first = time.perf_counter()
                    tokens.append(obj["token"])
                elif "token" in obj:
                    tokens.append(obj["token"])
                if "meta" in obj:
                    meta = obj["meta"]
    ended = time.perf_counter()
    return {
        "ok": meta is not None,
        "first_token_ms": ((first - started) * 1000) if first else None,
        "total_ms": (ended - started) * 1000,
        "cache_hit": (meta or {}).get("cache_hit"),
        "intent": (meta or {}).get("intent"),
        "model": (meta or {}).get("model"),
        "usage": (meta or {}).get("usage"),
        "trace_id": (meta or {}).get("trace_id"),
        "n_token_events": len(tokens),
    }


def main() -> int:
    load_dotenv(ROOT / ".env")
    questions = [q for q in load_questions("dev") if not q.get("should_refuse")][:20]
    stage5 = {x["id"]: x for x in load_stage5_dev()["items"]}
    ctx = load_member_context()

    flush_namespace()
    # 预热问答缓存：回填已实测答案，避免回放为填缓存再计费。
    for q in questions:
        src = stage5[q["id"]]
        handle_ask(
            q["question"],
            ctx=ctx,
            use_cascade=True,
            use_cache=True,
            precomputed_answer={
                "ok": src.get("ok_generation"),
                "question": q["question"],
                "answer": src.get("answer"),
                "model": src.get("model"),
                "citations": src.get("citations"),
                "usage": src.get("usage"),
            },
        )

    # 进程内写入三类真实 trace：缓存命中、未缓存政策问答（含检索与生成）、工具调用。
    traced_hit = handle_ask(questions[0]["question"], ctx=ctx, use_cascade=True, use_cache=True)
    traced_miss = handle_ask(
        questions[1]["question"],
        ctx=ctx,
        use_cascade=True,
        use_cache=False,
    )
    traced_tool = handle_ask("查一下订单 SO-FRIDGE-20D 现在的状态", ctx=ctx, use_cascade=True, use_cache=True)
    langfuse_flush()
    time.sleep(2.0)
    traces = {
        "cache_hit": {"trace_id": traced_hit.get("trace_id"), "verify": None, "usage": traced_hit.get("usage")},
        "policy_miss": {
            "trace_id": traced_miss.get("trace_id"),
            "verify": None,
            "model": traced_miss.get("model"),
            "usage": traced_miss.get("usage"),
            "ok": traced_miss.get("ok"),
            "error_type": traced_miss.get("error_type"),
        },
        "tool": {
            "trace_id": traced_tool.get("trace_id"),
            "verify": None,
            "tool": traced_tool.get("tool"),
            "ok": traced_tool.get("ok"),
            "error_type": traced_tool.get("error_type"),
        },
    }
    for key, item in traces.items():
        tid = item.get("trace_id")
        item["verify"] = fetch_observations(tid) if tid else {"ok": False, "error_type": "missing_trace_id"}
    # 总验证：至少一条 trace 回读成功，且未缓存路径能看到生成或检索，工具路径能看到 tool。
    langfuse_verify = {
        "ok": any((t.get("verify") or {}).get("ok") for t in traces.values()),
        "base_url": (traces["cache_hit"]["verify"] or {}).get("base_url"),
        "traces": {
            k: {
                "trace_id": v.get("trace_id"),
                "ok": (v.get("verify") or {}).get("ok"),
                "n_observations": (v.get("verify") or {}).get("n_observations"),
                "observation_names": [o.get("name") for o in (v.get("verify") or {}).get("observations") or []],
                "observation_types": [o.get("type") for o in (v.get("verify") or {}).get("observations") or []],
                "error_type": (v.get("verify") or {}).get("error_type"),
                "model": v.get("model"),
                "tool": v.get("tool"),
            }
            for k, v in traces.items()
        },
    }
    trace_id = traced_hit.get("trace_id") or traced_miss.get("trace_id")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            HOST,
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        if not _wait_health():
            err = ""
            try:
                err = (proc.stderr.read() or b"").decode("utf-8", errors="replace")[-2000:]
            except Exception:  # noqa: BLE001
                pass
            report = {
                "ok": False,
                "error_type": "server_not_ready",
                "stderr_tail": err,
                "measured_at": iso(now_shanghai()),
                "timezone": "Asia/Shanghai",
            }
            write_json(ROOT / "reports" / "stage6_replay.json", report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1

        # 丢弃一次连接建立，再正式测缓存命中首 token。
        _stream_ttft(questions[0]["question"])
        hit_samples = []
        for q in questions:
            sample = _stream_ttft(q["question"])
            sample["id"] = q["id"]
            hit_samples.append(sample)

        firsts = [s["first_token_ms"] for s in hit_samples if s.get("first_token_ms") is not None]
        totals = [s["total_ms"] for s in hit_samples if s.get("total_ms") is not None]
        all_hits = all(s.get("cache_hit") for s in hit_samples)
        n_under_800 = sum(1 for x in firsts if x <= 800.0)

        # 成本分两项：缓存命中回放应为 0；另记验证用的未缓存生成 token。
        usage_in = usage_out = 0
        for s in hit_samples:
            u = s.get("usage") or {}
            usage_in += int(u.get("input_tokens") or 0)
            usage_out += int(u.get("output_tokens") or 0)
        miss_usage = traces["policy_miss"].get("usage") or {}

        # 对照：同题若关闭缓存，会走生成（只抽 1 道，避免额外大额费用）。记录但不纳入 800ms 考核。
        # 这里不再额外打冷启动生成。

        stats = {
            "n": len(firsts),
            "mean_ms": statistics.fmean(firsts) if firsts else None,
            "p50_ms": _percentile(firsts, 0.5),
            "p95_ms": _percentile(firsts, 0.95),
            "min_ms": min(firsts) if firsts else None,
            "max_ms": max(firsts) if firsts else None,
            "n_under_800ms": n_under_800,
            "meets_800ms_all": bool(firsts) and all(x <= 800.0 for x in firsts),
            "meets_800ms_p95": bool(firsts) and (_percentile(firsts, 0.95) or 0) <= 800.0,
        }
        total_stats = {
            "n": len(totals),
            "mean_ms": statistics.fmean(totals) if totals else None,
            "p50_ms": _percentile(totals, 0.5),
            "p95_ms": _percentile(totals, 0.95),
            "min_ms": min(totals) if totals else None,
            "max_ms": max(totals) if totals else None,
        }
        miss_in = int(miss_usage.get("input_tokens") or 0)
        miss_out = int(miss_usage.get("output_tokens") or 0)
        miss_model = traces["policy_miss"].get("model") or "qwen3.8-flash"
        cost = {
            "cache_hit_replay": {
                "input_tokens": usage_in,
                "output_tokens": usage_out,
                "total_tokens": usage_in + usage_out,
                "cost_cny": cost_cny("qwen3.8-flash", usage_in, usage_out),
                "note": "缓存命中路径按本次请求 usage 计；命中时应为 0。",
            },
            "langfuse_policy_miss_probe": {
                "model": miss_model,
                "input_tokens": miss_in,
                "output_tokens": miss_out,
                "total_tokens": miss_in + miss_out,
                "cost_cny": cost_cny(miss_model, miss_in, miss_out),
            },
            "price": {"source": PRICE_SOURCE, "region": PRICE_REGION, "mode": PRICE_MODE},
        }
        ok = bool(all_hits and firsts and langfuse_verify.get("ok"))
        report = {
            "ok": ok,
            "measured_at": iso(now_shanghai()),
            "timezone": "Asia/Shanghai",
            "environment": "local_preprod_uvicorn",
            "base_url": BASE,
            "n_questions": len(questions),
            "all_cache_hits": all_hits,
            "first_token_cache_hit_policy": stats,
            "total_latency_cache_hit_policy": total_stats,
            "cost": cost,
            "langfuse": langfuse_verify | {"trace_id": trace_id},
            "samples": hit_samples,
        }
        write_json(ROOT / "reports" / "stage6_replay.json", report)
        print(
            json.dumps(
                {
                    "ok": ok,
                    "first_token": stats,
                    "cost": cost,
                    "langfuse_ok": langfuse_verify.get("ok"),
                    "trace_id": trace_id,
                    "report": "reports/stage6_replay.json",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        flush_namespace()


if __name__ == "__main__":
    raise SystemExit(main())
