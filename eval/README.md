# 评测集说明

本目录存放 100 道评测题、70/30 拆分记录，以及测试集封存哈希。标注使用 `doc_id + section_path + 字符起止`，不直接标注 `chunk_id`，也不对着检索结果标注。

## 题型与拆分

- 100 题：单跳 60、多跳 25、拒答 15。
- 按文档簇整簇划分为开发集 70 / 测试集 30，不按题号随机切分。同一簇的题目留在同一边。
- Q041（手机数码品类簇）在开发集；Q042（质保期限簇，至少依赖 P07/P08）在测试集。
- Q039 是可答否定题（已超 7 天不能按无理由退），不进入拒答 15 题。
- 拒答题是语料确实覆盖不了、应拒答或转人工的问题，而不是“政策明确规定不能退”的可答否定。

## 测试集封存

`scripts/build_evalset.py` 在建好题集后立即对测试集稳定序列化内容计算 sha256，写入 `testset.sha256` 与 `testset_seal.json`。

稳定序列化规则：

- 只纳入测试集 30 题的 `id,type,cluster,question,answer_points,should_refuse,relevant_spans`。
- 题目按 `id` 排序，条款跨度按 `doc_id, section_path, char_start, char_end` 排序。
- `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`，再对 UTF-8 字节做 sha256。

第三段已在锁定检索链路后第一次跑测试集，数字不参与调参；封存 sha256 仍为 `092576646b41d9c35a3390e330ea62c2e62b0f00ab4ab78a32cb0220b3fc6a59`。若之后改动测试集，必须在提交说明中写明原因并更新哈希。跑测试集前由 `eval/seal.py` / `scripts/eval_retrieval.py --allow-test` 核对封存值。

## 条款跨度映射

每次重切分后运行 `scripts/map_annotations.py`。规则：切片与条款跨度在同一 `doc_id + section_path` 下的字符交集长度，除以条款跨度长度，大于 0.5 时切片继承 relevance。不对着检索结果标注，不直接使用 `chunk_id`。

第三段比较了三种不同切片集的切分（`section_window_400`、`section_window_120`、`sentence_pack_150`），按开发集密集检索 Recall@5 选定 `section_window_400`（B0 切分，221 切片）。映射结果见 `eval/annotation_mapping.json`，各策略对照见 `reports/mapping_*.json`。
