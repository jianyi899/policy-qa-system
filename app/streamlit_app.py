"""本机演示界面。第一段只展示地基状态，问答主路径在后续阶段接入。

仅绑定 127.0.0.1。请通过 `python -m app.start` 启动。
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env", override=False)

from app.config import clear_settings_cache, get_settings  # noqa: E402
from app.db import check_sqlite  # noqa: E402

clear_settings_cache()
settings = get_settings()
home_env = settings.home_dir() / ".env"
if home_env.is_file():
    load_dotenv(home_env, override=False)
    clear_settings_cache()
    settings = get_settings()

st.set_page_config(page_title="政策问答系统", layout="centered")
st.title("政策问答系统（轻量版）")
st.caption("第一段地基：SQLite 建库、15 篇语料与 embedding。问答、查订单和评测在后续阶段接入。")

sqlite = check_sqlite(settings)
status = "正常" if sqlite.get("ok") else "异常"
st.metric("数据库状态", status)
st.write(
    {
        "POLICY_QA_HOME": str(settings.home_dir()),
        "数据库文件": sqlite.get("path") or str(settings.db_path()),
        "embedding 模型": settings.embedding_model,
        "embedding 维度（来自配置）": settings.embedding_dim,
        "生成配置": f"{settings.qwen_fast_model} → {settings.qwen_strong_model}",
        "文档数": sqlite.get("n_docs"),
        "切片数": sqlite.get("n_chunks"),
        "向量数": sqlite.get("n_embeddings"),
        "sqlite-vec": sqlite.get("vec_version"),
        "表": sqlite.get("tables"),
    }
)
if not sqlite.get("ok"):
    st.error(f"数据库检查未通过：{sqlite.get('detail')}")
else:
    st.success("地基已就绪。请不要把密钥贴到聊天或提交到仓库。")
