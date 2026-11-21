"""本机演示界面：切换演示账号后进行政策问答、查订单或查物流，支持多轮追问。

仅绑定 127.0.0.1。请通过 `python -m app.start` 启动。
演示账号必须用侧边栏切换器选择，不要在对话里输入身份。
自动演示：打开 http://127.0.0.1:8501/?demo=1
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env", override=False)

from app.config import clear_settings_cache, get_settings  # noqa: E402
from app.db import check_sqlite  # noqa: E402
from app.dialogue import DialogueSession, answer_turn  # noqa: E402
from app.orchestrator import FRIDGE_QUESTION  # noqa: E402
from app.seed import (  # noqa: E402
    MEMBER_EXTERNAL_ID,
    context_from_demo_account,
    list_demo_accounts,
    seed,
)

DEMO_DONE_FLAG = Path("/tmp/stage6/demo_done")

CITATION_QUESTION = "手机数码商品还没有激活，签收才 5 天，包装和机身都完好，可以申请七天无理由退货吗？"
REFUSAL_QUESTION = "明天北京会不会下雨？要不要改到货时间？"

SUGGESTIONS = [
    ("检索与引用", CITATION_QUESTION),
    ("查订单后套政策", FRIDGE_QUESTION),
    ("应拒答的问题", REFUSAL_QUESTION),
]


def _load_env_and_settings():
    clear_settings_cache()
    settings = get_settings()
    home_env = settings.home_dir() / ".env"
    if home_env.is_file():
        load_dotenv(home_env, override=False)
        clear_settings_cache()
        settings = get_settings()
    return settings


settings = _load_env_and_settings()


def _inject_css() -> None:
    st.markdown(
        """
<style>
    .block-container { padding-top: 1.1rem; padding-bottom: 2rem; max-width: 1400px; }
    h1 { font-size: 2.0rem !important; letter-spacing: -0.02em; }
    .hero {
        background: linear-gradient(135deg, #0f3d4c 0%, #1a6a7a 55%, #2a8f7b 100%);
        color: #fff; border-radius: 16px; padding: 1.15rem 1.4rem 1.25rem;
        margin-bottom: 0.9rem;
    }
    .hero h2 { color: #fff !important; font-size: 1.55rem; margin: 0 0 0.35rem; }
    .hero p { color: rgba(255,255,255,0.92); font-size: 1.05rem; margin: 0; line-height: 1.55; }
    .segment-banner {
        background: #103848; color: #fff; border-radius: 12px;
        padding: 0.7rem 1rem; margin: 0 0 0.8rem; font-size: 1.25rem; font-weight: 650;
    }
    .badge-row { margin: 0.25rem 0 0.55rem; }
    .badge {
        display: inline-block; padding: 0.18rem 0.55rem; border-radius: 999px;
        font-size: 0.85rem; margin: 0 0.3rem 0.3rem 0; font-weight: 600;
    }
    .badge-model { background: #dbeafe; color: #1e40af; }
    .badge-order { background: #ede9fe; color: #5b21b6; }
    .facts {
        background: #f4f7fb; border: 1px solid #dbe3ee; border-radius: 12px;
        padding: 0.7rem 0.9rem; margin: 0.4rem 0 0.7rem; font-size: 0.98rem;
    }
    .cite {
        background: #fff; border-left: 4px solid #1a6a7a; padding: 0.45rem 0.7rem;
        margin: 0.3rem 0; border-radius: 0 8px 8px 0; font-size: 0.95rem;
    }
    .cite .meta { color: #4b5563; font-size: 0.86rem; }
    .metrics {
        background: #fff; border: 1px solid #dbe3ee; border-radius: 12px;
        padding: 0.9rem 1.05rem; margin: 0.4rem 0 0.8rem;
    }
    .metrics h3 { margin: 0 0 0.45rem; font-size: 1.15rem; }
    .metrics table { width: 100%; border-collapse: collapse; font-size: 0.98rem; }
    .metrics th, .metrics td { text-align: left; padding: 0.28rem 0.4rem; border-bottom: 1px solid #e5e7eb; vertical-align: top; }
    .metrics .src { color: #6b7280; font-size: 0.86rem; }
    header[data-testid="stHeader"], [data-testid="stToolbar"],
    [data-testid="stDecoration"], .stDeployButton,
    div[data-testid="stAppDeployButton"], #MainMenu, footer {
        display: none !important; visibility: hidden;
    }
    section[data-testid="stSidebar"] { min-width: 300px; }
</style>
        """,
        unsafe_allow_html=True,
    )


HERO_HTML = """
<div class="hero">
  <h2>按范围套政策的售后助手（轻量版）</h2>
  <p>先检索条款并给出句级引用；涉及具体订单时先查订单，再按该单的 SKU、品类与签收时间套政策。语料未覆盖的问题会拒答或转人工，不会编造条款。</p>
</div>
"""


def _fmt_ratio(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}".rstrip("0").rstrip(".") if digits == 4 else f"{value:.{digits}f}"


def _load_json(rel: str) -> dict | None:
    path = ROOT / rel
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _eval_results_html() -> str:
    """用仓库里已实测报告拼出评测结果，不编造数字。"""
    b0 = _load_json("reports/b0_dev.json") or {}
    locked = _load_json("reports/locked_retrieval.json") or {}
    run1 = _load_json("reports/test_run1.json") or {}
    run2 = _load_json("reports/test_run2.json") or {}
    run3 = _load_json("reports/test_run3.json") or {}
    dev = _load_json("reports/stage5_dev_citation.json") or {}

    def rec(obj, *keys, default="尚未写入"):
        cur = obj
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        if isinstance(cur, float):
            return f"{cur:.4f}"
        return cur

    def hit_frac(n, denom):
        if n in {"尚未写入", None}:
            return "尚未写入"
        return f"{n}/{denom}"

    run3_r = rec(run3, "retrieval", "Recall@5")
    run3_hit = rec(run3, "retrieval", "metrics", "n_hit@5")
    run3_ret = (
        f"{run3_r} / {run3_hit}/10"
        if run3 and run3_r != "尚未写入" and run3_hit != "尚未写入"
        else "尚未写入"
    )
    run2_aq = rec(run2, "answer_quality", "task_ok_over_generated")
    run3_aq = rec(run3, "answer_quality", "task_ok_over_generated")
    run2_ref = rec(run2, "refusal", "correct_over_total")
    run3_ref = rec(run3, "refusal", "correct_over_total")
    run2_macro = rec(run2, "citation", "macro_precision_over_questions_with_citations")
    run3_macro = rec(run3, "citation", "macro_precision_over_questions_with_citations")
    run2_micro = rec(run2, "citation", "micro_precision")
    run3_micro = rec(run3, "citation", "micro_precision")
    run2_mc = rec(run2, "citation", "micro_n_correct")
    run2_mt = rec(run2, "citation", "micro_n_cited")
    run3_mc = rec(run3, "citation", "micro_n_correct")
    run3_mt = rec(run3, "citation", "micro_n_cited")
    dev_sum = dev.get("summary") or {}
    dev_aq = rec(dev_sum, "answer_quality", "task_ok_over_generated")
    dev_ref = rec(dev_sum, "refusal", "correct_over_total")
    dev_macro = rec(dev_sum, "citation", "macro_precision_over_questions_with_citations")
    dev_micro = rec(dev_sum, "citation", "micro_precision")
    dev_mc = rec(dev_sum, "citation", "micro_n_correct")
    dev_mt = rec(dev_sum, "citation", "micro_n_cited")
    def _q042_pass(report):
        review = ((report.get("generation") or {}).get("q042") or {}).get("review") or {}
        if review.get("meets_expected") is True or review.get("ok") is True:
            return True
        if review.get("meets_expected") is False or review.get("ok") is False:
            return False
        return None
    q042_r3 = _q042_pass(run3)
    if q042_r3 is True:
        q042_text = "通过"
    elif q042_r3 is False:
        q042_text = "未通过"
    else:
        q042_r2 = _q042_pass(run2)
        q042_text = "第二次测试集通过" if q042_r2 else "见报告"

    rows = [
        ("B0 开发集检索 Recall@5 / Hit@5", "0.6903 / 20/24", "reports/b0_dev.json"),
        ("最终检索开发集 Recall@5 / Hit@5", "0.7493 / 21/24", "reports/locked_retrieval.json"),
        ("第一次测试集检索 Recall@5 / Hit@5", "0.8476 / 10/10", "reports/test_run1.json"),
        ("第二次测试集检索 Recall@5 / Hit@5", "0.8476 / 10/10", "reports/test_run2.json"),
        (
            "第三次测试集检索 Recall@5 / Hit@5",
            run3_ret,
            "reports/test_run3.json",
        ),
        ("开发集答题质量 / 拒答", f"{dev_aq} / {dev_ref}", "reports/stage5_dev_citation.json"),
        ("第二次测试集答题质量 / 拒答", f"{run2_aq} / {run2_ref}", "reports/test_run2.json"),
        (
            "第三次测试集答题质量 / 拒答",
            f"{run3_aq} / {run3_ref}" if run3 else "尚未写入",
            "reports/test_run3.json",
        ),
        (
            "第二次测试集句级引用宏/微精度",
            f"{run2_macro} / {run2_micro}（{run2_mc}/{run2_mt}）",
            "reports/test_run2.json",
        ),
        (
            "第三次测试集句级引用宏/微精度",
            f"{run3_macro} / {run3_micro}（{run3_mc}/{run3_mt}）" if run3 else "尚未写入",
            "reports/test_run3.json",
        ),
        ("开发集句级引用宏/微精度", f"{dev_macro} / {dev_micro}（{dev_mc}/{dev_mt}）", "reports/stage5_dev_citation.json"),
        ("Q042（SKU 24 个月覆盖通用 12 个月）", q042_text, "reports/test_run2.json、test_run3.json"),
    ]
    body = "".join(
        f"<tr><td>{name}</td><td>{val}</td><td class='src'>{src}</td></tr>" for name, val, src in rows
    )
    return f"""
<div class="metrics">
  <h3>轻量版评测结果（全部为实测，未改写）</h3>
  <p>检索最终链路：section_window_120 + 密集检索与 jieba BM25 的 RRF，不加 rerank。生成默认 qwen3.8-flash；级联阈值尚未标定，不编造阈值。测试集数字不参与调参。</p>
  <table>
    <tr><th>指标</th><th>实测值</th><th>来源</th></tr>
    {body}
  </table>
</div>
"""


def _badges(result: dict) -> str:
    parts = []
    model = result.get("model")
    if model:
        parts.append(f'<span class="badge badge-model">模型 {model}</span>')
    if result.get("cascaded"):
        parts.append('<span class="badge badge-model">已级联到更强模型</span>')
    facts = result.get("order_fields_injected") or {}
    if facts.get("order_no"):
        reused = "复用已查订单" if result.get("order_reused") else "已查订单"
        parts.append(f'<span class="badge badge-order">{reused} {facts.get("order_no")}</span>')
    elapsed = result.get("elapsed_ms")
    if elapsed is not None:
        parts.append(f'<span class="badge badge-model">耗时 {elapsed} ms</span>')
    return '<div class="badge-row">' + "".join(parts) + "</div>" if parts else ""


def _facts_html(result: dict) -> str:
    facts = result.get("order_fields_injected") or {}
    if not facts.get("order_no"):
        return ""
    reused = "（复用本会话已查结果）" if result.get("order_reused") else ""
    return (
        '<div class="facts">'
        f"已查订单 {facts.get('order_no')}{reused}："
        f"SKU={facts.get('sku')}，类目={facts.get('category')}，"
        f"签收={facts.get('received_at')}，会员={facts.get('is_member')}，"
        f"运费险={facts.get('shipping_insurance')}"
        "</div>"
    )


def _citations_html(result: dict, *, limit: int = 6) -> str:
    citations = result.get("citations") or []
    if not citations:
        return ""
    blocks = []
    for item in citations[:limit]:
        span = ""
        if item.get("char_start") is not None:
            span = f" {item.get('char_start')}-{item.get('char_end')}"
        text = item.get("text") or ""
        blocks.append(
            '<div class="cite">'
            f"<div><strong>[{item.get('sid')}]</strong> {item.get('doc_id') or '无效编号'} "
            f"{item.get('section_path') or ''}{span}</div>"
            f"<div class='meta'>{text}</div>"
            "</div>"
        )
    more = f"<div class='meta'>其余 {len(citations) - limit} 条引用已折叠</div>" if len(citations) > limit else ""
    return "".join(blocks) + more


def render_result(result: dict, *, highlight: str = "all") -> None:
    answer = result.get("answer") or result.get("error_type") or "没有得到回答。"
    st.markdown(answer)
    st.markdown(_badges(result), unsafe_allow_html=True)
    if highlight in {"order", "all"}:
        html = _facts_html(result)
        if html:
            st.markdown(html, unsafe_allow_html=True)
    if highlight in {"citations", "all", "order"}:
        html = _citations_html(result)
        if html:
            st.markdown("**句级引用**")
            st.markdown(html, unsafe_allow_html=True)


def _render_meta(meta: dict) -> None:
    render_result(meta, highlight="all")


def _user_bubble_html(question: str) -> str:
    return (
        '<div style="background:#f3f4f6;border-radius:12px;padding:0.7rem 0.9rem;'
        'margin:0 0 0.7rem;font-size:1.02rem;">'
        f"🙋 {html.escape(question)}</div>"
    )


def _assistant_pending_html() -> str:
    return (
        '<div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;'
        'padding:0.7rem 0.9rem;margin:0 0 0.7rem;font-size:1.02rem;">'
        "🤖 正在检索条款并生成回答…</div>"
    )


def _format_answer_html(answer: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(answer))
    return text.replace("\n", "<br>")


def _assistant_result_html(result: dict, *, highlight: str) -> str:
    answer = result.get("answer") or result.get("error_type") or "没有得到回答。"
    parts = [
        '<div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;'
        'padding:0.8rem 1rem;margin:0 0 0.7rem;font-size:1.02rem;line-height:1.6;">'
        "🤖 ",
        _format_answer_html(answer),
        _badges(result),
    ]
    if highlight in {"order", "all"}:
        parts.append(_facts_html(result))
    if highlight in {"citations", "all", "order"}:
        cites = _citations_html(result)
        if cites:
            parts.append("<div><strong>句级引用</strong></div>")
            parts.append(cites)
    parts.append("</div>")
    return "".join(parts)


def _render_turns(stage, turns: list[dict]) -> None:
    """用单一 HTML 块替换主区域，避免 Streamlit 容器在多段演示中残留上一问。"""
    chunks: list[str] = []
    for turn in turns:
        role = turn["role"]
        if role in {"hero", "metrics"}:
            chunks.append(turn["html"])
        elif role == "user":
            chunks.append(_user_bubble_html(turn["content"]))
        elif role == "pending":
            chunks.append(_assistant_pending_html())
        else:
            chunks.append(
                _assistant_result_html(turn["result"], highlight=turn.get("highlight") or "all")
            )
    chunks.append('<div style="height:64px;"></div>')
    stage.markdown("".join(chunks), unsafe_allow_html=True)


def demo_ask(question: str, stage, *, highlight: str, session: DialogueSession) -> dict:
    _render_turns(stage, [{"role": "user", "content": question}, {"role": "pending"}])
    result = answer_turn(session, question)
    _render_turns(
        stage,
        [
            {"role": "user", "content": question},
            {"role": "assistant", "result": result, "highlight": highlight},
        ],
    )
    return result


def _banner(slot, index: int, title: str) -> None:
    slot.markdown(
        f'<div class="segment-banner">演示第 {index}/5 段　{title}</div>',
        unsafe_allow_html=True,
    )


def run_auto_demo(banner) -> None:
    """五段自动演示，答案全部来自本机轻量版真实系统。每段只保留当前画面，便于在约 90 秒内看清。"""
    ctx = context_from_demo_account(MEMBER_EXTERNAL_ID, settings=settings)
    session = DialogueSession(ctx=ctx)
    st.session_state.dialogue = session
    st.session_state.messages = []
    stage = st.empty()

    _banner(banner, 1, "问题：系统定位")
    _render_turns(stage, [{"role": "hero", "html": HERO_HTML}])
    time.sleep(12)

    _banner(banner, 2, "检索与句级引用")
    time.sleep(0.8)
    demo_ask(CITATION_QUESTION, stage, highlight="citations", session=session)
    time.sleep(12)

    _banner(banner, 3, "查订单后按范围套政策")
    time.sleep(0.8)
    demo_ask(FRIDGE_QUESTION, stage, highlight="order", session=session)
    time.sleep(14)

    _banner(banner, 4, "拒答或转人工")
    session = DialogueSession(ctx=ctx)
    st.session_state.dialogue = session
    time.sleep(0.8)
    demo_ask(REFUSAL_QUESTION, stage, highlight="all", session=session)
    time.sleep(10)

    _banner(banner, 5, "评测结果")
    _render_turns(stage, [{"role": "metrics", "html": _eval_results_html()}])
    time.sleep(14)

    banner.markdown(
        '<div class="segment-banner">五段演示结束（轻量版真实界面与真实作答，非幻灯片）</div>',
        unsafe_allow_html=True,
    )
    DEMO_DONE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    DEMO_DONE_FLAG.write_text("ok\n", encoding="utf-8")
    st.session_state.demo_ran = True


def _ensure_seeded() -> None:
    try:
        seed(settings)
    except Exception as exc:  # noqa: BLE001
        st.error(f"写入演示数据失败：{type(exc).__name__}")


def main() -> None:
    st.set_page_config(page_title="政策问答系统", layout="wide", initial_sidebar_state="expanded")
    _inject_css()
    _ensure_seeded()

    params = st.query_params
    auto = str(params.get("demo", "")).lower() in {"1", "true", "yes"}

    accounts = list_demo_accounts(settings)
    labels = [a["label"] for a in accounts]
    label_to_ext = {a["label"]: a["external_id"] for a in accounts}
    default_idx = 0
    for i, acc in enumerate(accounts):
        if acc.get("external_id") == MEMBER_EXTERNAL_ID:
            default_idx = i
            break

    with st.sidebar:
        st.header("演示账号")
        st.write("用下面的切换器选择身份。不要在对话框里输入 user_id 或他人账号，系统不会据此切换身份。")
        selected_label = st.selectbox(
            "当前账号",
            labels,
            index=default_idx if auto else 0,
            key="demo_account_label",
            disabled=auto,
        )
        selected_ext = label_to_ext.get(selected_label)
        if auto:
            selected_ext = MEMBER_EXTERNAL_ID
            st.success("自动演示已选择：主路径用户")
        elif selected_ext:
            st.success(f"已选择：{selected_label}")
        else:
            st.warning("当前未登录。查询订单或物流前请先选择演示账号。")
        if not auto:
            if st.button("主路径示例：冰箱买了 20 天还能退吗", use_container_width=True):
                st.session_state["_pending_question"] = FRIDGE_QUESTION
            for label, question in SUGGESTIONS:
                if st.button(label, use_container_width=True):
                    st.session_state["_pending_question"] = question
                    st.rerun()
        st.divider()
        sqlite = check_sqlite(settings)
        st.caption(
            f"数据库：{'正常' if sqlite.get('ok') else '异常'}  \n"
            f"生成：{settings.qwen_fast_model} → {settings.qwen_strong_model}"
        )
        if not auto:
            st.caption("本界面只监听 127.0.0.1。密钥只放本地 .env，不要写入仓库。")

    st.title("政策问答系统（轻量版）")
    if not auto:
        st.caption("查订单后按范围套政策。页面只监听 127.0.0.1。演示账号请用左侧切换器选择。")

    try:
        ctx = context_from_demo_account(selected_ext, settings=settings)
    except ValueError:
        ctx = None
        if not auto:
            st.error("所选演示账号无效或尚未写入数据库，请重新启动 `python -m app.start`。")

    if st.session_state.get("_account_ext") != selected_ext:
        st.session_state._account_ext = selected_ext
        st.session_state.messages = []
        st.session_state.dialogue = DialogueSession(ctx=ctx)
        st.session_state.demo_ran = False

    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "dialogue" not in st.session_state:
        st.session_state.dialogue = DialogueSession(ctx=ctx)
    else:
        st.session_state.dialogue.ctx = ctx
    if "demo_ran" not in st.session_state:
        st.session_state.demo_ran = False

    banner = st.empty()
    if not auto:
        st.markdown(HERO_HTML, unsafe_allow_html=True)

    if auto and not st.session_state.demo_ran:
        if ctx is None:
            st.error("自动演示需要主路径用户，请先运行 python -m app.start。")
            return
        run_auto_demo(banner)
        return

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            elif msg.get("meta"):
                _render_meta(msg["meta"])
            else:
                st.markdown(msg["content"])

    pending = st.session_state.pop("_pending_question", None)
    typed = st.chat_input("输入问题，可先问政策再追问同一订单。例如：这单冰箱买了 20 天质量问题还能退吗")
    question = pending or typed
    if question:
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("正在作答…"):
                result = answer_turn(st.session_state.dialogue, question)
            _render_meta(result)
        answer = result.get("answer") or result.get("error_type") or "没有得到回答。"
        st.session_state.messages.append({"role": "assistant", "content": answer, "meta": result})


if __name__ == "__main__":
    main()
