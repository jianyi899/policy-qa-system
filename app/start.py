"""一条命令完成建库、写入语料（含 embedding），并启动 FastAPI 与 Streamlit。

用法（在仓库根目录）：

    python -m app.start
    python -m app.start --ingest-only

`--ingest-only` 只建库并按指纹写入语料，不启动 FastAPI / Streamlit，便于在测试目录验收入库。
切分策略以 reports/locked_retrieval.json 为准（尚未锁定时回退到 section_window_400）。第二次启动若语料、切分与 embedding 配置未变，不会重复写入，也不会再次调用 embedding 接口。
Streamlit 固定监听 127.0.0.1。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from app.config import clear_settings_cache, get_settings, repo_root
from app.db import init_db
from app.ingest import ingest_corpus, write_start_report
from app.seed import seed as seed_demo_data


STREAMLIT_BIND_HOST = "127.0.0.1"


def _load_env() -> None:
    """加载仓库和数据目录中的 .env，但不覆盖已经存在的进程环境变量。"""
    root = repo_root()
    load_dotenv(root / ".env", override=False)
    # home 可能来自环境或默认值，再尝试加载数据目录里的 .env。
    clear_settings_cache()
    home_env = get_settings().home_dir() / ".env"
    if home_env.is_file():
        load_dotenv(home_env, override=False)
        clear_settings_cache()


def _wait_http(url: str, timeout: float = 60.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if 200 <= getattr(resp, "status", 200) < 500:
                    return True
                last = f"status={getattr(resp, 'status', None)}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = type(exc).__name__
        time.sleep(0.25)
    print(f"等待服务超时: {url} last={last}", file=sys.stderr)
    return False


def _terminate(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.monotonic() + 8
    for proc in procs:
        remaining = max(0.05, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    unknown = [a for a in argv if a not in {"--ingest-only"}]
    if unknown:
        print(f"未知参数: {unknown}。可用：--ingest-only", file=sys.stderr)
        return 2
    ingest_only = "--ingest-only" in argv
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    _load_env()
    settings = get_settings()
    home = settings.home_dir()
    home.mkdir(parents=True, exist_ok=True)
    settings.data_dir().mkdir(parents=True, exist_ok=True)
    settings.log_dir().mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    db_path = init_db(settings)
    ingest = ingest_corpus(settings)
    seed = seed_demo_data(settings)
    elapsed_s = time.perf_counter() - started

    summary = {
        "ok": True,
        "db_path": str(db_path),
        "policy_qa_home": str(home),
        "init_elapsed_seconds": round(elapsed_s, 3),
        "api_url": f"http://{settings.app_host}:{settings.app_port}",
        "streamlit_url": f"http://{STREAMLIT_BIND_HOST}:{settings.streamlit_port}",
        "streamlit_bind": STREAMLIT_BIND_HOST,
        **ingest,
        "seed": {
            "ok": seed.get("ok"),
            "users": [u.get("external_id") for u in (seed.get("users") or [])],
            "orders": [o.get("order_no") for o in (seed.get("orders") or [])],
            "logistics": [x.get("tracking_no") for x in (seed.get("logistics") or [])],
        },
    }
    # 不把密钥写入报告。
    write_start_report(settings.log_dir() / "start-last.json", summary)
    summary["ingest_only"] = ingest_only
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    skipped = ingest.get("skipped")
    ingest_line = "已跳过语料写入与 embedding 调用" if skipped else "已写入语料并完成 embedding"
    seed_line = "已写入或刷新演示账号与冰箱订单（签收时间按当前往前约 20 天）"
    if ingest_only:
        print(f"数据库: {db_path}\n仅完成建库与入库，未启动服务。{ingest_line}。{seed_line}")
        return 0

    print(
        f"数据库: {db_path}\n"
        f"FastAPI: http://{settings.app_host}:{settings.app_port}/health\n"
        f"Streamlit: http://{STREAMLIT_BIND_HOST}:{settings.streamlit_port} （仅监听 {STREAMLIT_BIND_HOST}）\n"
        f"{ingest_line}。{seed_line}"
    )

    env = os.environ.copy()
    env["POLICY_QA_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo_root()) + os.pathsep + env.get("PYTHONPATH", "")
    streamlit_app = repo_root() / "app" / "streamlit_app.py"
    popen_kw: dict = {
        "cwd": str(repo_root()),
        "env": env,
    }

    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            settings.app_host,
            "--port",
            str(settings.app_port),
            "--log-level",
            "info",
        ],
        **popen_kw,
    )
    streamlit = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(streamlit_app),
            "--server.address",
            STREAMLIT_BIND_HOST,
            "--server.port",
            str(settings.streamlit_port),
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ],
        **popen_kw,
    )
    procs = [api, streamlit]

    def _on_signal(signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        health_url = f"http://{settings.app_host}:{settings.app_port}/health"
        st_url = f"http://{STREAMLIT_BIND_HOST}:{settings.streamlit_port}"
        if not _wait_http(health_url):
            print("FastAPI 未能在时限内开始响应 /health", file=sys.stderr)
            return 1
        if not _wait_http(st_url):
            print("Streamlit 未能在时限内开始响应", file=sys.stderr)
            return 1
        print("服务已就绪。按 Ctrl+C 停止。")
        while True:
            if api.poll() is not None:
                print(f"FastAPI 进程退出，code={api.returncode}", file=sys.stderr)
                return api.returncode or 1
            if streamlit.poll() is not None:
                print(f"Streamlit 进程退出，code={streamlit.returncode}", file=sys.stderr)
                return streamlit.returncode or 1
            time.sleep(0.5)
    finally:
        _terminate(procs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
