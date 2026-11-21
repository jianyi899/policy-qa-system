"""本机演示界面：切换演示账号后进行单轮政策问答、查订单或查物流。

仅绑定 127.0.0.1。请通过 `python -m app.start` 启动。
演示账号必须用侧边栏切换器选择，不要在对话里输入身份。
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
from app.orchestrator import FRIDGE_QUESTION, handle_question  # noqa: E402
from app.seed import context_from_demo_account, list_demo_accounts, seed  # noqa: E402

clear_settings_cache()
settings = get_settings()
home_env = settings.home_dir() / ".env"
if home_env.is_file():
    load_dotenv(home_env, override=False)
    clear_settings_cache()
    settings = get_settings()

st.set_page_config(page_title="政策问答系统", layout="centered")
st.title("政策问答系统（轻量版）")
st.caption("第四段：查订单、查物流，并按订单范围套政策。页面只监听 127.0.0.1。")

# 确保演示数据已写入当前 POLICY_QA_HOME 下的数据库。
try:
    seed(settings)
except Exception as exc:  # noqa: BLE001
    st.error(f"写入演示数据失败：{type(exc).__name__}")

accounts = list_demo_accounts(settings)
labels = [a["label"] for a in accounts]
label_to_ext = {a["label"]: a["external_id"] for a in accounts}

with st.sidebar:
    st.header("演示账号")
    st.write("用下面的切换器选择身份。不要在对话框里输入 user_id 或他人账号，系统不会据此切换身份。")
    selected_label = st.selectbox("当前账号", labels, index=0, key="demo_account_label")
    selected_ext = label_to_ext.get(selected_label)
    if selected_ext:
        st.success(f"已选择：{selected_label}")
        st.caption(f"external_id = `{selected_ext}`（仅展示，不会作为工具参数传递）")
    else:
        st.warning("当前未登录。查询订单或物流前请先选择演示账号。")
    if st.button("主路径示例：冰箱买了 20 天还能退吗", use_container_width=True):
        st.session_state["_pending_question"] = FRIDGE_QUESTION
    st.divider()
    sqlite = check_sqlite(settings)
    st.caption(
        f"数据库：{'正常' if sqlite.get('ok') else '异常'}  \n"
        f"POLICY_QA_HOME：{settings.home_dir()}  \n"
        f"生成：{settings.qwen_fast_model} → {settings.qwen_strong_model}"
    )

try:
    ctx = context_from_demo_account(selected_ext, settings=settings)
except ValueError:
    ctx = None
    st.error("所选演示账号无效或尚未写入数据库，请重新启动 `python -m app.start`。")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        meta = msg.get("meta") or {}
        if meta.get("order_fields_injected") and meta["order_fields_injected"].get("order_no"):
            facts = meta["order_fields_injected"]
            st.info(
                f"已查订单 {facts.get('order_no')}：SKU={facts.get('sku')}，"
                f"类目={facts.get('category')}，签收={facts.get('received_at')}，"
                f"会员={facts.get('is_member')}，运费险={facts.get('shipping_insurance')}"
            )
        if meta.get("cited_policies"):
            with st.expander("引用的政策"):
                for item in meta["cited_policies"]:
                    st.write(
                        f"- {item.get('doc_id')} {item.get('title') or ''} "
                        f"（{item.get('section_path') or ''}）"
                    )
        if meta.get("model"):
            cascaded = "已级联到更强模型" if meta.get("cascaded") else "未级联，使用默认快速模型"
            st.caption(f"模型：{meta.get('model')}（{cascaded}）")

pending = st.session_state.pop("_pending_question", None)
typed = st.chat_input("输入问题。例如：这单冰箱买了 20 天质量问题还能退吗")
question = pending or typed
if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("正在作答…"):
            result = handle_question(question, ctx)
        answer = result.get("answer") or result.get("error_type") or "没有得到回答。"
        st.markdown(answer)
        facts = result.get("order_fields_injected") or {}
        if facts.get("order_no"):
            st.info(
                f"已查订单 {facts.get('order_no')}：SKU={facts.get('sku')}，"
                f"类目={facts.get('category')}，签收={facts.get('received_at')}，"
                f"会员={facts.get('is_member')}，运费险={facts.get('shipping_insurance')}"
            )
        if result.get("cited_policies"):
            with st.expander("引用的政策"):
                for item in result["cited_policies"]:
                    st.write(
                        f"- {item.get('doc_id')} {item.get('title') or ''} "
                        f"（{item.get('section_path') or ''}）"
                    )
        if result.get("model"):
            cascaded = "已级联到更强模型" if result.get("cascaded") else "未级联，使用默认快速模型"
            st.caption(f"模型：{result.get('model')}（{cascaded}）  耗时 {result.get('elapsed_ms')} ms")
        elif result.get("error_type"):
            st.caption(f"错误：{result.get('error_type')}")
    st.session_state.messages.append({"role": "assistant", "content": answer, "meta": result})
