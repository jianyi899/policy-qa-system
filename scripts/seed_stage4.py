#!/usr/bin/env python3
"""写入或刷新第四段演示账号与冰箱订单。可重复执行。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import clear_settings_cache, get_settings  # noqa: E402
from app.db import init_db  # noqa: E402
from app.seed import seed  # noqa: E402


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    clear_settings_cache()
    settings = get_settings()
    init_db(settings)
    report = seed(settings)
    out = ROOT / "reports" / "stage4_seed.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
