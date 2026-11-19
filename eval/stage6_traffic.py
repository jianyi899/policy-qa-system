"""第六段缓存与回放用的固定流量。不改正式 100 题评测集。"""

from __future__ import annotations

# 开发集纯政策题的同义变体：只用于确认不得误命中，不进入命中率分母。
SYNONYM_VARIANTS = [
    {
        "id": "SYN-Q001",
        "source_id": "Q001",
        "question": "签收刚满五天的未激活手机数码，包装和机身都完好，还能走七天无理由吗？",
    },
    {
        "id": "SYN-Q002",
        "source_id": "Q002",
        "question": "已经激活的手机出现质量问题，申请退换货最晚可以到什么时候？",
    },
    {
        "id": "SYN-Q003",
        "source_id": "Q003",
        "question": "激活后的手机数码签收已经超过十五天，还在质保里，质量问题该怎么处理？",
    },
    {
        "id": "SYN-Q004",
        "source_id": "Q004",
        "question": "手机数码条款里讲的激活，具体包含哪些情况？",
    },
    {
        "id": "SYN-Q006",
        "source_id": "Q006",
        "question": "一件商品要被认定为定制类，需要满足什么条件？",
    },
    {
        "id": "SYN-Q011",
        "source_id": "Q011",
        "question": "无理由退货的七天期限，是从哪一天起算的？",
    },
    {
        "id": "SYN-Q041",
        "source_id": "Q041",
        "question": "手机已经激活了，还能不能按七天无理由退，质量问题退换又是几天？",
    },
    {
        "id": "SYN-Q062",
        "source_id": "Q062",
        "question": "我想把星河优选账号永久删掉并清除名下全部个人信息，流程和保留多久分别是什么？",
    },
]

# 工具类问句：路由层必须绕过缓存。默认用查询类，避免评测误写退款。
TOOL_QUESTIONS = [
    {"id": "TOOL-ORDER-1", "question": "查一下订单 SO-FRIDGE-20D 现在的状态", "expect_tool": "query_order"},
    {"id": "TOOL-ORDER-2", "question": "看看我的订单 SO-FRIDGE-20D", "expect_tool": "query_order"},
    {"id": "TOOL-LOG-1", "question": "订单 SO-FRIDGE-20D 的物流到哪了？", "expect_tool": "query_logistics"},
    {"id": "TOOL-LOG-2", "question": "帮我查一下 SO-FRIDGE-20D 的快递轨迹", "expect_tool": "query_logistics"},
    {"id": "TOOL-REFUND-1", "question": "查一下 SO-FRIDGE-20D 的退款进度", "expect_tool": "refund"},
    {"id": "TOOL-REPORT-1", "question": "查询今天的售后日报", "expect_tool": "daily_report"},
    {"id": "TOOL-REPORT-2", "question": "生成昨天的日报", "expect_tool": "daily_report"},
]

# 只用于路由单测，默认不在回放里真实申请退款。
TOOL_APPLY_SAMPLES = [
    {"id": "TOOL-REFUND-APPLY", "question": "帮我申请退款，订单 SO-FRIDGE-20D，原因是质量问题", "expect_tool": "refund"},
]
