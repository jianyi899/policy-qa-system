"""第五段评测的公共准备：固定测试目录，避免写到用户电脑的 D:/policy-qa。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path("/tmp/policy-qa-stage5-test")


def prepare_stage5_home(home: Path | None = None) -> Path:
    """设置 POLICY_QA_HOME 并加载密钥。不覆盖已有的进程环境变量中的密钥。"""
    load_dotenv(ROOT / ".env", override=False)
    target = Path(os.environ["POLICY_QA_HOME"]) if os.environ.get("POLICY_QA_HOME") else (home or DEFAULT_HOME)
    # 若调用方未指定，强制使用本段测试目录，避免 pydantic 再去读默认的 D:/policy-qa 或 ~/policy-qa。
    if "POLICY_QA_HOME" not in os.environ:
        os.environ["POLICY_QA_HOME"] = str(target)
    else:
        target = Path(os.environ["POLICY_QA_HOME"])
    target.mkdir(parents=True, exist_ok=True)
    from app.config import clear_settings_cache

    clear_settings_cache()
    return target
