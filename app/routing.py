"""路由层：区分纯政策问答与查询工具请求。

分类只看问题文本，不做语义检索。第一版不提供申请退款、日报等写操作。
不从问题文本解析用户身份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

def keyword_for_question(question: str) -> str | None:
    if "冰箱" in question:
        return "冰箱"
    return None


ORDER_NO_RE = re.compile(r"SO-[A-Z0-9-]+", re.IGNORECASE)

# 明确的工具意图。先匹配这些，避免「申请退款，原因是质量问题」被当成政策问答。
DAILY_REPORT_CUES = ("日报", "每日报表", "每日报告", "售后日报", "售后报表", "运营日报")
# 只匹配明确的轨迹查询，避免「以物流妥投为准」「快递被代收算不算签收」等政策题被误判。
LOGISTICS_CUES = (
    "查物流",
    "查快递",
    "查一下物流",
    "查一下快递",
    "物流到哪",
    "快递到哪",
    "物流在哪",
    "快递在哪",
    "到哪了",
    "到哪儿了",
    "发到哪",
    "送到哪",
    "快递轨迹",
    "物流轨迹",
    "运单轨迹",
    "查运单",
    "派件到哪",
)
REFUND_TOOL_CUES = (
    "申请退款",
    "帮我退款",
    "替我退款",
    "提交退款",
    "发起退款",
    "退款进度",
    "退款单",
    "查退款",
    "查询退款",
    "看看退款",
)
ORDER_STATUS_CUES = (
    "查一下订单",
    "查一下我的订单",
    "查询订单",
    "查询我的订单",
    "查订单",
    "看看订单",
    "看看我的订单",
    "我的订单状态",
    "订单状态",
    "订单现在怎样",
    "订单现在怎么样",
    "订单到哪一步",
)

# 出现这些时，即使带了订单号，也按政策问答走（可能需要先查订单再作答），不得当成纯工具。
POLICY_CUES = (
    "七天无理由",
    "无理由退",
    "能不能退",
    "还能退",
    "可以退",
    "可否退",
    "是否可退",
    "退货",
    "退换",
    "换货",
    "维修",
    "质保",
    "保修",
    "保修期",
    "申请时限",
    "运费谁",
    "运费由",
    "谁承担运费",
    "运费呢",
    "会员能延长",
    "延长七天",
    "政策",
    "规定",
    "条款",
    "如何处理",
    "怎么处理",
    "适用",
    "激活",
    "完好",
)


@dataclass(frozen=True)
class Route:
    intent: str  # "tool" | "policy" | "unsupported_write"
    tool: str | None = None
    arguments: dict | None = None
    cacheable: bool = False
    reason: str = ""


def _has_any(text: str, cues: tuple[str, ...]) -> bool:
    return any(c in text for c in cues)


def extract_order_no(question: str) -> str | None:
    match = ORDER_NO_RE.search(question or "")
    return match.group(0).upper() if match else None


def _extract_tracking_no(question: str) -> str | None:
    match = re.search(r"(?:运单号|快递单号|物流单号)\s*[:：]?\s*([A-Z0-9-]{6,})", question or "", re.I)
    return match.group(1).upper() if match else None


def _extract_report_date(question: str) -> str | None:
    match = re.search(r"(20\d{2}-\d{2}-\d{2})", question or "")
    return match.group(1) if match else None


def wants_order(question: str) -> bool:
    return bool(extract_order_no(question) or keyword_for_question(question))


def classify(question: str) -> Route:
    """根据问题文本分类。工具类优先；带政策要点的订单题仍走政策路径。"""
    text = (question or "").strip()
    if not text:
        return Route(intent="policy", cacheable=True, reason="empty_question")

    if _has_any(text, REFUND_TOOL_CUES) or _has_any(text, DAILY_REPORT_CUES):
        return Route(intent="unsupported_write", cacheable=False, reason="write_not_in_v1")

    if _has_any(text, LOGISTICS_CUES) and not _has_any(text, POLICY_CUES):
        args = {}
        order_no = extract_order_no(text)
        tracking = _extract_tracking_no(text)
        if order_no:
            args["order_no"] = order_no
        if tracking:
            args["tracking_no"] = tracking
        return Route(intent="tool", tool="query_logistics", arguments=args, cacheable=False, reason="logistics")

    policy_like = _has_any(text, POLICY_CUES)
    if _has_any(text, ORDER_STATUS_CUES) and not policy_like:
        args = {}
        order_no = extract_order_no(text)
        if order_no:
            args["order_no"] = order_no
        return Route(intent="tool", tool="query_order", arguments=args, cacheable=False, reason="order_status")

    # 纯政策问答：不触发查订单（没有订单号、也没有会走查单工具的商品关键词）。
    # 带订单的政策题仍走政策路径，但答案依赖订单事实，不得写入问答缓存。
    cacheable = not wants_order(text)
    return Route(
        intent="policy",
        cacheable=cacheable,
        reason="policy_pure" if cacheable else "policy_with_order",
    )
