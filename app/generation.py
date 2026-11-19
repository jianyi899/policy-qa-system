"""根据冲突解析后的证据作答。默认 qwen3.8-flash，级联达到阈值时升到 qwen-plus。不使用 LangChain。默认关闭思考模式。"""

from __future__ import annotations

import time

from app.citation import format_evidence_block, resolve_citations
from app.config import get_settings

DEFAULT_MODEL = "qwen3.8-flash"  # qwen-turbo 已于 2026-10-10 官方下线；按现型号替换
DEFAULT_MAX_RETRIES = 4


class GenerationError(RuntimeError):
    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__(error_type)


def _require_api_key() -> str:
    settings = get_settings()
    key = settings.dashscope_api_key or ""
    if not key:
        raise GenerationError("missing_DASHSCOPE_API_KEY")
    return key


def _format_order_facts(order_facts: dict | None) -> str:
    if not order_facts:
        return "本轮没有已核实的订单事实（这是政策咨询，不是针对某一已查订单的追问）。"
    if not (order_facts.get("order_no") or order_facts.get("sku") or order_facts.get("category")):
        return "本轮没有已核实的订单事实（这是政策咨询，不是针对某一已查订单的追问）。"
    if not order_facts.get("order_no"):
        return (
            "未绑定具体订单，仅根据问题识别到的范围：\n"
            f"- SKU：{order_facts.get('sku')}\n"
            f"- 类目：{order_facts.get('category')}"
        )
    return (
        "已核实的订单事实（来自查订单工具或本会话此前已查过并复用的结果，不是用户口述）：\n"
        f"- 订单号：{order_facts.get('order_no')}\n"
        f"- SKU：{order_facts.get('sku')}\n"
        f"- 类目：{order_facts.get('category')}\n"
        f"- 签收时间：{order_facts.get('received_at')}\n"
        f"- 距签收已过日历天数：{order_facts.get('days_since_receipt')}\n"
        f"- 是否仍在 7 天无理由窗口（按签收当日开算、含第 7 日 24:00）：{order_facts.get('within_7_day_no_reason')}\n"
        f"- 是否有效会员：{order_facts.get('is_member')}（档位 {order_facts.get('membership_tier')}）\n"
        f"- 是否购买运费险：{order_facts.get('shipping_insurance')}"
    )


def generate_answer(
    *,
    question: str,
    order_facts: dict | None,
    applicable: list[dict],
    excluded: list[dict],
    conflict_rule: str,
    conflict_explanation: str | None = None,
    evidence_sentences: list[dict] | None = None,
    history: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
    max_evidence: int = 10,
    max_retries: int = DEFAULT_MAX_RETRIES,
    on_first_token=None,
) -> dict:
    """根据订单事实与适用政策生成带句级引用的回答。失败时抛出 GenerationError（不含密钥）。"""
    settings = get_settings()
    allowed = {
        DEFAULT_MODEL,
        settings.qwen_fast_model or DEFAULT_MODEL,
        settings.qwen_strong_model or "qwen-plus",
        "qwen-plus",
    }
    if model not in allowed:
        raise GenerationError("model_lock_violation")
    api_key = _require_api_key()
    sentences = list(evidence_sentences or [])
    if not sentences:
        # 兼容未预切句的调用：退回按条款块提供证据，仍要求尽量引用 doc_id。
        evidence_blocks = []
        for item in (applicable or [])[:max_evidence]:
            evidence_blocks.append(
                "\n".join(
                    [
                        f"[冲突序 {item.get('conflict_rank')} | {item.get('doc_id')} "
                        f"{item.get('section_path')} | 来源={item.get('source_label')} "
                        f"范围={item.get('scope_label')}"
                        f"{(':' + item['scope_value']) if item.get('scope_value') else ''} "
                        f"生效日={item.get('effective_date')}]",
                        item.get("text") or "",
                    ]
                )
            )
        evidence_text = "\n\n".join(evidence_blocks) if evidence_blocks else "（无适用条款）"
    else:
        evidence_text = format_evidence_block(sentences)

    excluded_brief = []
    for item in excluded or []:
        excluded_brief.append(
            f"{item.get('doc_id')} {item.get('section_path')} "
            f"({item.get('applicability')}, source={item.get('source_type')}, "
            f"scope={item.get('scope_type')}:{item.get('scope_value') or ''})"
        )

    explanation = (conflict_explanation or "").strip() or f"冲突排序口径：{conflict_rule}"

    system = (
        "你是星河优选的售后政策助手。必须依据给定订单事实（若有）和适用政策条款作答，"
        "不得编造语料中不存在的政策、期限或例外。"
        "若七天无理由已过期，要明确说明不能按无理由退货；"
        "若问题是质量问题，还要依据质量问题与质保条款判断是否仍可申请退换或维修。"
        "会员权益不延长七天无理由时限。质量问题退货运费由商家承担，不优先消耗运费险。"
        "干扰文档或与本订单/本问题范围不符的专项条款不得作为依据。"
        "若存在更细范围（SKU 或品类）覆盖更一般规则的情况，必须在回答里写清："
        "为何按更细范围作答，以及它覆盖了哪条更一般的规则。"
        "如果适用条款无法覆盖用户问题（例如账号注销、金融利率、气象、选购推荐、跨境关税、"
        "以旧换新补贴、商家结算、批次召回名单、企业合同条款等语料未收录事项），"
        "必须明确拒答或建议转人工/对应渠道，不得编造政策。"
        "历史对话只用于理解追问对象（例如「这单」「运费呢」），本轮以当前订单事实与适用条款为准。"
        "回答使用简体中文，先给结论，再简述依据，不要输出思维过程。"
        "凡是依据了某条证据句，必须在对应句子或分句末尾标注引用编号，格式严格为 [S1]、[S2]，"
        "只能使用下面提供的编号，不得自造编号，不得把已排除文档当作依据。"
        "拒答时可以不引用政策句。"
    )
    user = (
        f"用户问题：{question}\n\n"
        f"{_format_order_facts(order_facts)}\n\n"
        f"冲突解析结论：\n{explanation}\n\n"
        f"可引用的适用证据句（句级，编号如下）：\n"
        f"{evidence_text}\n\n"
        "检索到但已排除、不得作为依据的文档：\n"
        + ("\n".join(excluded_brief) if excluded_brief else "（无）")
    )

    # qwen3.8-flash 在 DashScope 原生 Generation.call（/api/v1）会返回 url error，
    # 改走官方兼容模式接口。默认关闭思考模式，避免非流式超时和额外 Token。
    import httpx

    settings = get_settings()
    url = settings.dashscope_base_url.rstrip("/") + "/chat/completions"
    use_stream = on_first_token is not None
    messages = [{"role": "system", "content": system}]
    for turn in (history or [])[-4:]:
        prev_q = (turn.get("question") or "").strip()
        prev_a = (turn.get("answer") or "").strip()
        if prev_q and prev_a:
            messages.append({"role": "user", "content": prev_q})
            messages.append({"role": "assistant", "content": prev_a})
    messages.append({"role": "user", "content": user})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "top_p": 0.8,
        "enable_thinking": False,
    }
    if use_stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_err: GenerationError | None = None
    retries = max(1, int(max_retries))
    for attempt in range(retries):
        try:
            if use_stream:
                text, usage_dict, status_code, data = _stream_completion(
                    url, headers, payload, on_first_token=on_first_token
                )
            else:
                with httpx.Client(timeout=60.0) as client:
                    response = client.post(url, headers=headers, json=payload)
                status_code = response.status_code
                try:
                    data = response.json()
                except Exception:  # noqa: BLE001
                    data = {}
                text = ""
                usage_dict = None
                if status_code == 200 and isinstance(data, dict):
                    choices = data.get("choices") or []
                    if choices:
                        message = (choices[0] or {}).get("message") or {}
                        text = message.get("content") or ""
                    usage = data.get("usage")
                    if isinstance(usage, dict):
                        usage_dict = {
                            "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                            "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
                            "total_tokens": usage.get("total_tokens"),
                        }
                    if str(text).strip() and on_first_token is not None:
                        on_first_token(str(text).strip()[:1])
        except Exception as exc:  # noqa: BLE001
            last_err = GenerationError(type(exc).__name__)
            time.sleep(min(2 ** (attempt + 1), 20))
            continue
        if status_code != 200:
            err = data.get("error") if isinstance(data, dict) else {}
            code = (err or {}).get("code") or (err or {}).get("type") or data.get("code")
            err_type = str(code) if code else f"http_{status_code}"
            retryable = status_code in {429, 500, 502, 503, 504} or str(code) in {
                "Throttling.RateQuota",
                "Throttling",
                "limit_requests",
                "ServiceUnavailable",
            }
            last_err = GenerationError(err_type)
            if retryable and attempt + 1 < retries:
                # 429 多等一会儿，避免连续打满。
                delay = 30 if status_code == 429 or str(code) in {"limit_requests", "Throttling.RateQuota", "Throttling"} else min(2 ** (attempt + 1), 20)
                time.sleep(delay)
                continue
            raise last_err
        if not str(text).strip():
            last_err = GenerationError("empty_generation")
            continue
        answer = str(text).strip()
        citations = resolve_citations(answer, sentences) if sentences else []
        return {
            "ok": True,
            "model": model,
            "answer": answer,
            "usage": usage_dict,
            "citations": citations,
        }
    raise last_err or GenerationError("generation_failed")


def _stream_completion(url: str, headers: dict, payload: dict, *, on_first_token) -> tuple[str, dict | None, int, dict]:
    """兼容模式 SSE 流式读取。首个非空 token 时回调 on_first_token。"""
    import json as json_lib

    import httpx

    text_parts: list[str] = []
    usage_dict = None
    first_sent = False
    data: dict = {}
    with httpx.Client(timeout=60.0) as client:
        with client.stream("POST", url, headers=headers, json=payload) as response:
            status_code = response.status_code
            if status_code != 200:
                try:
                    body = response.read().decode("utf-8", errors="replace")
                    data = json_lib.loads(body) if body else {}
                except Exception:  # noqa: BLE001
                    data = {}
                return "", None, status_code, data
            for line in response.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                else:
                    continue
                if chunk == "[DONE]":
                    break
                try:
                    event = json_lib.loads(chunk)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(event, dict):
                    continue
                usage = event.get("usage")
                if isinstance(usage, dict):
                    usage_dict = {
                        "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                        "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
                        "total_tokens": usage.get("total_tokens"),
                    }
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0] or {}).get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    if not first_sent:
                        first_sent = True
                        if on_first_token is not None:
                            on_first_token(piece)
                    text_parts.append(piece)
    return "".join(text_parts), usage_dict, 200, data
