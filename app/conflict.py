"""冲突解析：先范围（sku → 品类 → 通用），再来源（政策 → 手册 → FAQ），再生效日。

查订单返回的 sku、类目、签收时间、是否会员、运费险必须注入本模块，按范围套政策。
干扰语料（source_type=noise）不得进入比较，也不得覆盖正式政策。
范围与当前订单不匹配的 SKU / 品类文档视为不适用。
"""

from __future__ import annotations

import re

from datetime import date, datetime

from app.clock import (
    calendar_days_since,
    iso,
    no_reason_deadline,
    within_7_day_no_reason,
    within_warranty_months,
)

SCOPE_RANK = {"sku": 0, "category": 1, "general": 2}
SOURCE_RANK = {"policy": 0, "manual": 1, "faq": 2}
SCOPE_LABEL = {"sku": "sku", "category": "品类", "general": "通用"}
SOURCE_LABEL = {"policy": "政策", "manual": "手册", "faq": "FAQ"}

RULE_TEXT = (
    "先比较范围：sku → 品类 → 通用（更具体者优先）；"
    "范围相同再比较来源：政策 → 手册 → FAQ；"
    "范围与来源均相同再比较生效日：较晚者优先。"
    "noise 不得进入比较。"
)


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


def derive_order_facts(order: dict) -> dict:
    """把查订单字段整理为冲突解析与作答使用的事实。"""
    received_raw = order.get("received_at")
    received_at = None
    if isinstance(received_raw, datetime):
        received_at = received_raw
    elif isinstance(received_raw, str) and received_raw:
        received_at = datetime.fromisoformat(received_raw)

    facts = {
        "sku": order.get("sku"),
        "category": order.get("category"),
        "received_at": iso(received_at) if received_at else order.get("received_at"),
        "is_member": bool(order.get("is_member")),
        "membership_tier": order.get("membership_tier"),
        "shipping_insurance": bool(order.get("shipping_insurance")),
        "order_no": order.get("order_no"),
        "order_status": order.get("order_status"),
        "days_since_receipt": order.get("days_since_receipt"),
        "within_7_day_no_reason": order.get("within_7_day_no_reason"),
        "within_general_warranty_12m": order.get("within_general_warranty_12m"),
        "no_reason_deadline": None,
    }
    if received_at is not None:
        facts["days_since_receipt"] = calendar_days_since(received_at)
        facts["within_7_day_no_reason"] = within_7_day_no_reason(received_at)
        facts["within_general_warranty_12m"] = within_warranty_months(received_at, 12)
        facts["no_reason_deadline"] = iso(no_reason_deadline(received_at))
    return facts


def classify_scope(doc: dict, order: dict) -> tuple[str, str]:
    """返回 (applicability, scope_match)。"""
    source = doc.get("source_type")
    scope_type = doc.get("scope_type") or "general"
    scope_value = (doc.get("scope_value") or "").strip()
    if source == "noise":
        return "excluded_noise", "n/a"
    sku = order.get("sku")
    category = order.get("category")
    if scope_type == "sku":
        if sku and scope_value == sku:
            return "applicable", "sku"
        return "excluded_scope_mismatch", "sku"
    if scope_type == "category":
        if category and scope_value == category:
            return "applicable", "category"
        return "excluded_scope_mismatch", "category"
    if scope_type == "general":
        return "applicable", "general"
    return "excluded_unknown_scope", scope_type


def _sort_key(item: dict) -> tuple:
    scope = item.get("scope_match") or "general"
    source = item.get("source_type") or "faq"
    eff = _as_date(item.get("effective_date")) or date.min
    # 范围更具体优先，来源政策优先，生效日较晚优先，最后用检索分与 doc_id 稳定排序。
    return (
        SCOPE_RANK.get(scope, 99),
        SOURCE_RANK.get(source, 99),
        # date.max - eff 让较晚的排前面；转成 toordinal
        -eff.toordinal(),
        -(item.get("score") or 0.0),
        item.get("doc_id") or "",
        item.get("section_path") or "",
        item.get("char_start") or 0,
    )


def resolve(chunks: list[dict], order: dict) -> dict:
    facts = derive_order_facts(order)
    annotated = []
    for ch in chunks:
        applicability, scope_match = classify_scope(ch, order)
        item = dict(ch)
        item["applicability"] = applicability
        item["scope_match"] = scope_match
        item["scope_label"] = SCOPE_LABEL.get(scope_match, scope_match)
        item["source_label"] = SOURCE_LABEL.get(ch.get("source_type"), ch.get("source_type"))
        if ch.get("effective_date") is not None:
            eff = _as_date(ch.get("effective_date"))
            item["effective_date"] = eff.isoformat() if eff else None
        annotated.append(item)

    applicable = sorted(
        [x for x in annotated if x["applicability"] == "applicable"],
        key=_sort_key,
    )
    excluded = [x for x in annotated if x["applicability"] != "applicable"]
    for i, item in enumerate(applicable, start=1):
        item["conflict_rank"] = i

    return {
        "rule": RULE_TEXT,
        "order_fields_injected": facts,
        "applicable": applicable,
        "excluded": excluded,
        "n_applicable": len(applicable),
        "n_excluded": len(excluded),
    }


SKU_RE = re.compile(r"SKU-[A-Z0-9-]+", re.IGNORECASE)


def known_scopes(docs: list[dict] | None = None) -> dict:
    """从语料 front matter 收集已知 SKU / 品类，供问题侧范围推断。"""
    if docs is None:
        from app.corpus_io import load_corpus

        docs = load_corpus()
    skus = sorted({d.get("scope_value") for d in docs if d.get("scope_type") == "sku" and d.get("scope_value")})
    categories = sorted(
        {d.get("scope_value") for d in docs if d.get("scope_type") == "category" and d.get("scope_value")},
        key=len,
        reverse=True,
    )
    return {"skus": skus, "categories": categories}


def infer_scope_from_question(question: str, *, scopes: dict | None = None) -> dict:
    """从问句中识别 SKU / 品类，供无订单时的冲突套用。订单事实优先于推断。"""
    scopes = scopes if scopes is not None else known_scopes()
    text = question or ""
    sku = None
    for known in scopes.get("skus") or []:
        if known and known in text:
            sku = known
            break
    if sku is None:
        match = SKU_RE.search(text)
        if match:
            sku = match.group(0).upper()
    category = None
    for cat in scopes.get("categories") or []:
        if cat and cat in text:
            category = cat
            break
    return {
        "sku": sku,
        "category": category,
        "inferred": bool(sku or category),
        "source": "question_inferred" if (sku or category) else "none",
    }


def context_for_resolve(order: dict | None, question: str | None = None) -> dict:
    """构造传入 resolve 的范围上下文：真实订单优先，否则用问句推断的 SKU/品类。"""
    ctx = dict(order or {})
    if question and not (ctx.get("sku") or ctx.get("category")):
        inferred = infer_scope_from_question(question)
        if inferred.get("sku") and not ctx.get("sku"):
            ctx["sku"] = inferred["sku"]
        if inferred.get("category") and not ctx.get("category"):
            ctx["category"] = inferred["category"]
        if inferred.get("inferred"):
            ctx["scope_source"] = inferred["source"]
    return ctx


def explain_conflict(resolution: dict) -> dict:
    """把冲突排序结果写成可注入提示词、也可展示给用户的说明。"""
    facts = resolution.get("order_fields_injected") or {}
    applicable = resolution.get("applicable") or []
    by_scope: dict[str, list[dict]] = {"sku": [], "category": [], "general": []}
    seen_docs: set[tuple] = set()
    for item in applicable:
        scope = item.get("scope_match") or "general"
        doc_key = (item.get("doc_id"), scope)
        if scope not in by_scope or doc_key in seen_docs:
            continue
        seen_docs.add(doc_key)
        by_scope[scope].append(item)

    def _fmt(item: dict) -> str:
        value = item.get("scope_value") or ""
        title = item.get("title") or ""
        title_part = f"《{title}》" if title else ""
        scope_part = f"{item.get('scope_label')}"
        if value:
            scope_part += f"：{value}"
        return (
            f"{item.get('doc_id')}{title_part}"
            f"（来源={item.get('source_label')}，范围={scope_part}，生效日={item.get('effective_date')}）"
        )

    lines: list[str] = []
    sku = facts.get("sku")
    category = facts.get("category")
    if facts.get("order_no"):
        lines.append(f"已绑定订单 {facts.get('order_no')}：SKU={sku or '（无）'}，品类={category or '（无）'}。")
    elif sku or category:
        lines.append(f"未绑定具体订单，按问题中的范围套用：SKU={sku or '（未指定）'}，品类={category or '（未指定）'}。")
    else:
        lines.append("未绑定具体订单，也未从问题中识别到 SKU 或品类，仅适用通用范围条款。")
    lines.append("冲突排序口径：" + RULE_TEXT)

    has_fine = bool(by_scope["sku"] or by_scope["category"])
    if by_scope["sku"]:
        lines.append("SKU 级规定优先于品类与通用，适用：" + "、".join(_fmt(x) for x in by_scope["sku"]) + "。")
        if by_scope["category"] or by_scope["general"]:
            lines.append(
                "因此更细的 SKU 规定覆盖（优先于并取代其适用位置上的）更一般的品类或通用规定，"
                "作答须采用 SKU 专项结论，并说明它覆盖了更一般规则，不得只按通用规则作答。"
            )
    elif by_scope["category"]:
        lines.append("品类级规定优先于通用，适用：" + "、".join(_fmt(x) for x in by_scope["category"]) + "。")
        lines.append(
            "因此本品类专项覆盖（优先于并取代其适用位置上的）通用规定，"
            "作答须采用专类结论并说明依据，不得用通用规则替代或缩减专项时限。"
        )
    else:
        lines.append("未匹配到 SKU 或品类专项条款，适用文档均为通用范围。不要编造专项规则。")

    return {
        "has_finer_scope": has_fine,
        "sku": sku,
        "category": category,
        "by_scope_doc_ids": {k: [x.get("doc_id") for x in v] for k, v in by_scope.items()},
        "text": "\n".join(lines),
    }
