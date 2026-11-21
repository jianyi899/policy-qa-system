# 评测集说明（轻量版）

本目录存放约 40 道评测题、开发集 / 测试集拆分记录，以及测试集封存哈希。标注使用 `doc_id + section_path + 字符起止`，不直接标注 `chunk_id`，也不对着检索结果标注。题目从完整版 100 题中选取并校对口径，题干均被轻量版 40 篇语料覆盖。

## 题型与拆分

- 40 题：单跳 24、多跳 10、拒答 6（约 60% / 25% / 15%）。
- 开发集 28、测试集 12。按文档簇整簇划分，不按题号随机切分。同一簇的题目留在同一边。
- Q041（手机数码品类簇）在开发集；Q042（质保期限簇，至少依赖 P07/P08）在测试集。
- Q025（定制退货与运费）在开发集。
- Q039 是可答否定题（已超 7 天不能按无理由退），不进入拒答集。
- 拒答题是语料确实覆盖不了、应拒答或转人工的问题，而不是“政策明确规定不能退”的可答否定。
- 服装鞋帽簇整簇归测试集；本段从中选取 Q085、Q086，未把同簇题目放入开发集。

## 测试集封存

`scripts/build_evalset.py` 在建好题集后立即对测试集稳定序列化内容计算 sha256，写入 `testset.sha256`、`testset_seal.json` 与 `eval/seal.py` 中的 `EXPECTED_SHA256`。

稳定序列化规则：

- 只纳入测试集 12 题的 `id,type,cluster,question,answer_points,should_refuse,relevant_spans`。
- 题目按 `id` 排序，条款跨度按 `doc_id, section_path, char_start, char_end` 排序。
- `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`，再对 UTF-8 字节做 sha256。

本阶段不在测试集上跑评测、不用测试集数字调参。若之后改动测试集，必须在提交说明中写明原因并更新哈希。跑测试集前由 `eval/seal.py` / `scripts/eval_retrieval.py --allow-test` 核对封存值。

## 条款跨度映射

每次重切分后运行 `scripts/map_annotations.py`。规则：切片与条款跨度在同一 `doc_id + section_path` 下的字符交集长度，除以条款跨度长度，大于 0.5 时切片继承 relevance。不对着检索结果标注，不直接使用 `chunk_id`。

## 检索评测与回归

```bash
python scripts/map_annotations.py
python scripts/eval_retrieval.py --split dev --out reports/b0_dev_eval.json
# 或：python scripts/run_b0_dev.py
```

至少报告 Recall@5（以及 Hit@5、MRR@5、nDCG@5）。拒答用「对了几道 / 共几道」汇报，不设 90% 目标；本段 B0 只评检索，不跑生成。回归规则：与 `reports/last_passed_dev_recall.json` 中上次通过的 Hit@5 逐题比较，净减少（由对变错减去由错变对）超过 1 题则失败。本段不上 CI 门禁。
