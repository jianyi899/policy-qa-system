"""本机演示界面：政策问答、句级引用、冲突消解、查订单后套政策、拒答与缓存命中。

仅绑定 127.0.0.1。演示用户由服务端注入，不在工具签名中传递 user_id。
启动：streamlit run app/streamlit_app.py --server.address 127.0.0.1 --server.port 8501
自动演示：打开 http://127.0.0.1:8501/?demo=1
"""

from __future__ import annotations

import time
from pathlib import Path

import sys

import psycopg
import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.config import get_settings  # noqa: E402
from app.context import SessionContext  # noqa: E402
from app.dialogue import DialogueSession  # noqa: E402
from app.runtime import handle_ask  # noqa: E402
from scripts.seed_stage4 import MEMBER_EXTERNAL_ID, seed  # noqa: E402

DEMO_DONE_FLAG = Path("/tmp/stage7/demo_done")

FRIDGE_QUESTION = "这单冰箱买了 20 天质量问题还能退吗"
CITATION_QUESTION = "手机数码商品还没有激活，签收才 5 天，包装和机身都完好，可以申请七天无理由退货吗？"
CONFLICT_QUESTION = "我买的星河手表 S3（SKU-HX-WATCH-S3，智能穿戴）质量保证期限是多久？不是还有通用 12 个月的规定吗，到底适用哪一个？"
REFUSAL_QUESTION = "明天北京会不会下雨？要不要改到货时间？"

SUGGESTIONS = [
    ("句级引用", CITATION_QUESTION),
    ("冲突消解（Q042）", CONFLICT_QUESTION),
    ("查订单后套政策", FRIDGE_QUESTION),
    ("应拒答的问题", REFUSAL_QUESTION),
]


def _ensure_demo_user() -> SessionContext:
    settings = get_settings()
    with psycopg.connect(settings.dsn()) as conn:
        seed(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, external_id, display_name FROM users WHERE external_id = %s",
                (MEMBER_EXTERNAL_ID,),
            )
            row = cur.fetchone()
    if not row:
        raise RuntimeError("未找到演示用户 u-stage4-member，请先运行 scripts/seed_stage4.py")
    return SessionContext(user_id=int(row[0]), external_id=row[1], display_name=row[2])


def _inject_css() -> None:
    st.markdown(
        """
<style>
    .block-container { padding-top: 1.1rem; padding-bottom: 2rem; max-width: 1400px; }
    h1 { font-size: 2.1rem !important; letter-spacing: -0.02em; }
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
    .badge-cache { background: #d1fae5; color: #065f46; }
    .badge-miss { background: #e5e7eb; color: #374151; }
    .badge-model { background: #dbeafe; color: #1e40af; }
    .badge-up { background: #fef3c7; color: #92400e; }
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
    .conflict {
        background: #fff7ed; border: 1px solid #fdba74; border-radius: 12px;
        padding: 0.7rem 0.9rem; margin: 0.5rem 0; white-space: pre-wrap; font-size: 0.98rem;
    }
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


def init_state() -> None:
    if st.session_state.get("_ready"):
        return
    ctx = _ensure_demo_user()
    st.session_state.ctx = ctx
    st.session_state.dialogue = DialogueSession(ctx=ctx)
    st.session_state.messages: list[dict] = []
    st.session_state.demo_ran = False
    st.session_state._ready = True


def _badges(result: dict) -> str:
    parts = []
    if result.get("cache_hit"):
        parts.append('<span class="badge badge-cache">缓存命中</span>')
    elif result.get("cache_bypassed"):
        parts.append('<span class="badge badge-miss">工具/订单路径，已绕过缓存</span>')
    elif result.get("cacheable") is False:
        parts.append('<span class="badge badge-miss">本问不缓存</span>')
    else:
        parts.append('<span class="badge badge-miss">缓存未命中</span>')
    model = result.get("model")
    if model:
        parts.append(f'<span class="badge badge-model">模型 {model}</span>')
    cascade = result.get("cascade") or {}
    if cascade.get("upgraded"):
        parts.append('<span class="badge badge-up">已级联升至 qwen-plus</span>')
    facts = result.get("order_fields_injected") or {}
    if result.get("tool_calls") or facts.get("order_no"):
        reused = "复用已查订单" if result.get("order_reused") else "已查订单"
        order_no = facts.get("order_no") or ""
        parts.append(f'<span class="badge badge-order">{reused} {order_no}</span>')
    first_ms = result.get("first_token_ms")
    if first_ms is not None:
        parts.append(f'<span class="badge badge-model">首 token {first_ms:.1f} ms</span>')
    return '<div class="badge-row">' + "".join(parts) + "</div>"


def _facts_html(result: dict) -> str:
    facts = result.get("order_fields_injected") or {}
    if not facts.get("order_no"):
        return ""
    days = facts.get("days_since_receipt")
    member = "是" if facts.get("is_member") else "否"
    insurance = "有" if facts.get("shipping_insurance") else "无"
    return (
        '<div class="facts">'
        f"<b>按订单范围套政策</b>　订单 {facts.get('order_no')}　SKU {facts.get('sku')}　"
        f"类目 {facts.get('category')}　签收已过 {days} 天　会员 {member}　运费险 {insurance}"
        "</div>"
    )


def _citations_html(result: dict, *, limit: int = 6) -> str:
    citations = result.get("citations") or []
    if not citations:
        return ""
    blocks = [f"<div><b>句级引用</b>（展示前 {min(limit, len(citations))} / {len(citations)} 条）</div>"]
    for c in citations[:limit]:
        text = (c.get("text") or "").strip()
        blocks.append(
            '<div class="cite">'
            f"<b>[{c.get('sid')}]</b> {text}"
            f"<div class='meta'>{c.get('doc_id')}　{c.get('section_path')}　"
            f"{c.get('char_start')}–{c.get('char_end')}</div>"
            "</div>"
        )
    return "".join(blocks)


def render_result(result: dict, *, highlight: str | None = None) -> None:
    st.markdown(_badges(result), unsafe_allow_html=True)
    if highlight in {"order", "all"} or (result.get("order_fields_injected") or {}).get("order_no"):
        html = _facts_html(result)
        if html:
            st.markdown(html, unsafe_allow_html=True)
    answer = result.get("answer") or result.get("error_type") or "（无回答）"
    st.markdown(answer)
    explanation = (result.get("conflict_resolution") or {}).get("explanation")
    if explanation and highlight in {"conflict", "all", "order", "citations"}:
        st.markdown(
            f'<div class="conflict"><b>冲突消解</b>\n{explanation}</div>',
            unsafe_allow_html=True,
        )
    cite = _citations_html(result)
    if cite and highlight in {"citations", "conflict", "order", "all", None}:
        st.markdown(cite, unsafe_allow_html=True)


def _call_handle(question: str, *, use_cache: bool, use_cascade: bool) -> dict:
    return handle_ask(
        question,
        ctx=st.session_state.ctx,
        session=st.session_state.dialogue,
        use_cache=use_cache,
        use_cascade=use_cascade,
    )


def ask(question: str, *, use_cache: bool = True, use_cascade: bool = True, highlight: str | None = None) -> dict:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("正在查询（如需）、检索条款并生成回答…"):
            result = _call_handle(question, use_cache=use_cache, use_cascade=use_cascade)
        render_result(result, highlight=highlight)
    st.session_state.messages.append({"role": "assistant", "content": result.get("answer") or "", "result": result, "highlight": highlight})
    return result


def _render_turns(stage, turns: list[dict]) -> None:
    """在占位容器里只画出当前段落的问答，避免长页滚不到最新回答。"""
    stage.empty()
    with stage.container():
        for turn in turns:
            if turn["role"] == "hero":
                st.markdown(turn["html"], unsafe_allow_html=True)
                continue
            with st.chat_message(turn["role"]):
                if turn["role"] == "user":
                    st.markdown(turn["content"])
                else:
                    render_result(turn["result"], highlight=turn.get("highlight"))


def demo_ask(question: str, stage, *, highlight: str, prior: list[dict] | None = None, use_cache: bool = True) -> dict:
    waiting = list(prior or [])
    waiting.append({"role": "user", "content": question})
    waiting.append({"role": "assistant_pending", "content": question})
    stage.empty()
    with stage.container():
        for turn in prior or []:
            if turn["role"] == "hero":
                st.markdown(turn["html"], unsafe_allow_html=True)
            elif turn["role"] == "user":
                with st.chat_message("user"):
                    st.markdown(turn["content"])
            else:
                with st.chat_message("assistant"):
                    render_result(turn["result"], highlight=turn.get("highlight"))
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("正在查询（如需）、检索条款并生成回答…"):
                result = _call_handle(question, use_cache=use_cache, use_cascade=True)
    final = list(prior or [])
    final.append({"role": "user", "content": question})
    final.append({"role": "assistant", "result": result, "highlight": highlight})
    _render_turns(stage, final)
    st.session_state.messages.extend(final[-2:])
    return result


def render_history() -> None:
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            else:
                result = msg.get("result") or {"answer": msg.get("content")}
                render_result(result, highlight=msg.get("highlight"))


def _banner(slot, index: int, title: str) -> None:
    slot.markdown(f'<div class="segment-banner">演示第 {index}/5 段　{title}</div>', unsafe_allow_html=True)


HERO_HTML = """
<div class="hero">
  <h2>按范围套政策的售后助手</h2>
  <p>检索条款并给出句级引用；遇到冲突时说明更细范围为何覆盖更一般规则；查到订单后再按该单的 SKU、品类与签收时间作答。语料未覆盖的问题会拒答，不会编造条款。</p>
</div>
"""


def run_auto_demo(banner) -> None:
    """五段自动演示，答案全部来自本机真实系统。每段只保留当前问答，便于在 90 秒内看清。"""
    from app.cache import flush_namespace

    flush_namespace()
    st.session_state.dialogue = DialogueSession(ctx=st.session_state.ctx)
    st.session_state.messages = []
    stage = st.empty()

    _banner(banner, 1, "问题与系统定位")
    _render_turns(stage, [{"role": "hero", "html": HERO_HTML}])
    time.sleep(11)

    _banner(banner, 2, "检索 + 句级引用")
    time.sleep(1.0)
    demo_ask(CITATION_QUESTION, stage, highlight="citations")
    time.sleep(9)

    _banner(banner, 3, "冲突消解：SKU 专项覆盖通用 12 个月")
    time.sleep(1.0)
    demo_ask(CONFLICT_QUESTION, stage, highlight="conflict")
    time.sleep(10)

    _banner(banner, 4, "查订单后按范围套政策")
    time.sleep(1.0)
    demo_ask(FRIDGE_QUESTION, stage, highlight="order")
    time.sleep(10)

    _banner(banner, 5, "拒答与缓存命中")
    # 新开会话，避免把天气拒答误接到上一单冰箱追问；问答缓存仍在 Redis，原句可命中。
    st.session_state.dialogue = DialogueSession(ctx=st.session_state.ctx)
    time.sleep(1.0)
    demo_ask(REFUSAL_QUESTION, stage, highlight="all")
    time.sleep(7)
    demo_ask(CITATION_QUESTION, stage, highlight="citations")
    time.sleep(8)

    banner.markdown('<div class="segment-banner">五段演示结束（真实系统作答，非幻灯片）</div>', unsafe_allow_html=True)
    DEMO_DONE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    DEMO_DONE_FLAG.write_text("ok\n", encoding="utf-8")
    st.session_state.demo_ran = True


def main() -> None:
    st.set_page_config(page_title="政策问答系统", layout="wide", initial_sidebar_state="expanded")
    _inject_css()
    init_state()

    params = st.query_params
    auto = str(params.get("demo", "")).lower() in {"1", "true", "yes"}

    with st.sidebar:
        st.subheader("系统定位")
        st.markdown(
            "电商售后政策问答助手。先检索条款，再按 "
            "**SKU → 品类 → 通用** 的冲突排序套政策；"
            "涉及具体订单时，会先查订单再按该单范围作答，回答带来源句子。"
        )
        st.markdown(
            "检索链路：`section_window_400` + 密集检索与 BM25 的 RRF。"
            "生成默认 `qwen3.8-flash`，达到阈值升至 `qwen-plus`。"
        )
        ctx = st.session_state.ctx
        st.subheader("当前会话")
        st.markdown(
            f"演示用户：**{ctx.display_name or ctx.external_id}**  \n"
            "服务端注入，含冰箱订单 `SO-FRIDGE-20D`（签收约 20 天）。"
            "工具签名不含 user_id。"
        )
        st.subheader("试试这些问题")
        for label, question in SUGGESTIONS:
            if st.button(label, use_container_width=True):
                st.session_state["_pending_question"] = question
                st.session_state["_pending_highlight"] = (
                    "order" if question == FRIDGE_QUESTION else "conflict" if "质保" in question else "citations"
                )
                st.rerun()
        st.caption("本界面只监听 127.0.0.1，不会对外网开放。密钥只放服务端环境变量。")

    st.title("政策问答系统")
    banner = st.empty()
    if not auto:
        st.markdown(
            """
<div class="hero">
  <h2>按范围套政策的售后助手</h2>
  <p>检索条款并给出句级引用；遇到冲突时说明更细范围为何覆盖更一般规则；查到订单后再按该单的 SKU、品类与签收时间作答。语料未覆盖的问题会拒答，不会编造条款。</p>
</div>
            """,
            unsafe_allow_html=True,
        )

    if auto and not st.session_state.demo_ran:
        run_auto_demo(banner)
        return

    render_history()
    pending = st.session_state.pop("_pending_question", None)
    highlight = st.session_state.pop("_pending_highlight", None)
    typed = st.chat_input("输入售后政策问题，例如：这单冰箱买了 20 天质量问题还能退吗")
    question = pending or typed
    if question:
        ask(question, highlight=highlight or "all")


if __name__ == "__main__":
    main()
