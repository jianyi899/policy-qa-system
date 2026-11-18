# 评测集说明（第二段）

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

本阶段不在测试集上跑评测，也不用测试集数字调参。若之后改动测试集，必须在提交说明中写明原因并更新哈希。

## 条款跨度映射

默认切分后运行 `scripts/map_annotations.py`。规则：切片与条款跨度在同一 `doc_id + section_path` 下的字符交集长度，除以条款跨度长度，大于 0.5 时切片继承 relevance。每次重切分都要先映射再评测。本阶段只做一次默认切分（章节内字符窗口，参数见 `app/chunking.py`），三种切分消融不在本阶段进行。
