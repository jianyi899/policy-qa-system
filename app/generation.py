"""用 qwen3.8-flash 根据冲突解析后的证据作答。不使用 LangChain，不升级到 qwen-plus。默认关闭思考模式。"""

from __future__ import annotations

import time

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


def generate_answer(
    *,
    question: str,
    order_facts: dict,
    applicable: list[dict],
    excluded: list[dict],
    conflict_rule: str,
    model: str = DEFAULT_MODEL,
    max_evidence: int = 10,
) -> dict:
    """根据订单事实与适用政策生成回答。失败时抛出 GenerationError（不含密钥）。"""
    if model != DEFAULT_MODEL:
        # 第四段只用 qwen3.8-flash，级联到 qwen-plus 留到第六段。
        raise GenerationError("model_lock_violation")
    api_key = _require_api_key()
    evidence_blocks = []
    for item in applicable[:max_evidence]:
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
    excluded_brief = []
    for item in excluded:
        excluded_brief.append(
            f"{item.get('doc_id')} {item.get('section_path')} "
            f"({item.get('applicability')}, source={item.get('source_type')}, "
            f"scope={item.get('scope_type')}:{item.get('scope_value') or ''})"
        )

    system = (
        "你是星河优选的售后政策助手。必须依据给定订单事实和适用政策条款作答，"
        "不得编造语料中不存在的政策、期限或例外。"
        "若七天无理由已过期，要明确说明不能按无理由退货；"
        "若问题是质量问题，还要依据质量问题与质保条款判断是否仍可申请退换或维修。"
        "会员权益不延长七天无理由时限。质量问题退货运费由商家承担，不优先消耗运费险。"
        "干扰文档或与本订单范围不符的专项条款不得作为依据。"
        "回答使用简体中文，先给结论，再简述依据（可引用 doc_id），不要输出思维过程。"
    )
    user = (
        f"用户问题：{question}\n\n"
        f"已核实的订单事实（来自查订单工具，不是用户口述）：\n"
        f"- 订单号：{order_facts.get('order_no')}\n"
        f"- SKU：{order_facts.get('sku')}\n"
        f"- 类目：{order_facts.get('category')}\n"
        f"- 签收时间：{order_facts.get('received_at')}\n"
        f"- 距签收已过日历天数：{order_facts.get('days_since_receipt')}\n"
        f"- 是否仍在 7 天无理由窗口（按签收当日开算、含第 7 日 24:00）：{order_facts.get('within_7_day_no_reason')}\n"
        f"- 是否有效会员：{order_facts.get('is_member')}（档位 {order_facts.get('membership_tier')}）\n"
        f"- 是否购买运费险：{order_facts.get('shipping_insurance')}\n"
        f"- 冲突解析：本单未匹配到 SKU 或品类专项条款，适用文档均为通用范围。质保期限以适用政策为准，不要编造专项规则。\n\n"
        f"冲突排序口径：{conflict_rule}\n\n"
        f"按冲突排序后的适用条款：\n"
        + ("\n\n".join(evidence_blocks) if evidence_blocks else "（无适用条款）")
        + "\n\n检索到但已排除、不得作为依据的文档：\n"
        + ("\n".join(excluded_brief) if excluded_brief else "（无）")
    )

    # qwen3.8-flash 在 DashScope 原生 Generation.call（/api/v1）会返回 url error，
    # 改走官方兼容模式接口。默认关闭思考模式，避免非流式超时和额外 Token。
    import httpx

    settings = get_settings()
    url = settings.dashscope_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
        "top_p": 0.8,
        "enable_thinking": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_err: GenerationError | None = None
    for attempt in range(DEFAULT_MAX_RETRIES):
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(url, headers=headers, json=payload)
        except Exception as exc:  # noqa: BLE001
            last_err = GenerationError(type(exc).__name__)
            time.sleep(min(2 ** (attempt + 1), 20))
            continue
        status_code = response.status_code
        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            data = {}
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
            if retryable and attempt + 1 < DEFAULT_MAX_RETRIES:
                time.sleep(min(2 ** (attempt + 1), 20))
                continue
            raise last_err
        text = ""
        choices = data.get("choices") if isinstance(data, dict) else None
        if choices:
            message = (choices[0] or {}).get("message") or {}
            text = message.get("content") or ""
        if not str(text).strip():
            last_err = GenerationError("empty_generation")
            continue
        usage = data.get("usage") if isinstance(data, dict) else None
        usage_dict = None
        if isinstance(usage, dict):
            usage_dict = {
                "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
                "total_tokens": usage.get("total_tokens"),
            }
        return {
            "ok": True,
            "model": model,
            "answer": str(text).strip(),
            "usage": usage_dict,
        }
    raise last_err or GenerationError("generation_failed")
