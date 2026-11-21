# 政策问答系统（轻量版）

面向政策类文档的企业问答系统，以电商售后为验证场景。当前仓库的 **main 已改为轻量栈**：在 Windows 上用 Python 直接运行，不使用 Docker、Postgres、Redis 或本地大模型。

完整版（Postgres + pgvector + Redis + Docker Compose，含 70 篇语料与当时评测报告）保留在注释 tag **[v1-full-postgres](https://github.com/jianyi899/policy-qa-system/releases/tag/v1-full-postgres)**（对应提交 `9468ee9`）。完整版评测数字以该 tag 中的 README 与 `reports/` 为准；那些数字不能当作轻量版的结果，也不用于本段调参。

改成轻量栈的原因：完整版依赖本机或 Docker 中的 Postgres / Redis，不方便在普通 Windows 电脑上直接安装和启动。生成模型方面，`qwen-turbo` 已于 2026-10-10 官方下线，配置改为 `qwen3.8-flash`（DashScope compatible-mode，`enable_thinking=False`）级联 `qwen-plus`。

## 当前进度

- 第一段地基：SQLite 建表、锁定 `text-embedding-v4`（1024 维从配置读取）、一条命令启动。
- 第二段语料与评测集：语料补齐至 40 篇（含 N07 投毒）、40 题拆为开发集 28 / 测试集 12，条款跨度标注与映射脚本，以及开发集 B0 密集检索基线。
- **第三段检索优化（本段）**：三种切分消融、BM25+RRF、gte-rerank 实测，本地回归脚本，以及第一次测试集评测。最终检索链路已写入 `reports/locked_retrieval.json`。
- 后续才做：查订单 / 查物流、句级引用与冲突消解、演示视频。申请退款等写操作、缓存与 Langfuse 留给第二版。不要使用 LangChain。

## 第三段实测（检索优化）

在测试目录 `/tmp/policy-qa-stage3-test` 入库并评测。安装与数据位置是该目录下的 embedding 缓存、数据库 `/tmp/policy-qa-stage3-test/data/policy_qa.sqlite` 和日志；代码仍在本仓库。未默认写入 C 盘，也未改动 Windows 上的 `D:/policy-qa`。测量时间：2026-11-21 15:40:49（Asia/Shanghai）。数字来源见 `reports/phase3_summary.json`、`reports/chunking_ablation.json`、`reports/hybrid_dev.json`、`reports/rerank_dev.json`、`reports/test_run1.json`。

命令：

```bash
export POLICY_QA_HOME=/tmp/policy-qa-stage3-test
python scripts/run_stage3.py
```

选型与调参只用开发集。测试集只在锁定链路之后跑了一次，数字不参与调参。本段不跑生成。

### 三种切分消融（开发集，纯密集检索）

每次先映射条款跨度再评测。三种策略切片集互不相同，均可在 Windows / SQLite 上使用，没有发明第四种。

| 切分 | 切片数 | Recall@5 | Hit@5 | 未命中 | 相对 section_window_400 |
| --- | --- | --- | --- | --- | --- |
| section_window_400（B0） | 130 | 0.6903 | 20 / 24（0.8333） | Q008、Q015、Q017、Q039 | 基线 |
| **section_window_120（选定）** | 157 | **0.6972** | **21 / 24（0.8750）** | Q008、Q013、Q017 | 由错变对 2 题（Q015、Q039），由对变错 1 题（Q013）；McNemar p=1.0，方向为正但未达显著 |
| sentence_pack_150 | 139 | 0.6903 | 20 / 24（0.8333） | Q008、Q015、Q017、Q039 | 与 B0 切分 Hit@5 / Recall@5 完全一致，无分歧题目 |

选定理由：开发集纯密集检索 Recall@5 最高。`sentence_pack_150` 与 B0 并列但未更高，故不选。

### BM25 + RRF（在选定切分上，对比纯向量）

实现为 jieba + `rank_bm25.BM25Okapi`（k1=1.5，b=0.75），与密集检索经 RRF（k=60）融合。开发集 Recall@5 从 0.6972 升到 0.7493，因此纳入主链路。Hit@5 仍为 21 / 24。

| 对照 | 结果 |
| --- | --- |
| 由错变对 | 1 题：Q008 |
| 由对变错 | 1 题：Q039 |
| McNemar | n=2，p=1.0，无方向差异 |
| Recall@5 升降 | 上升 3 题（Q005、Q008、Q010），下降 1 题（Q039） |

不为了必须提升而改 BM25 / RRF 参数。

### gte-rerank

对融合后的前 20 条调用 DashScope `gte-rerank` API。第一道开发集题 Q001 即返回 `AccessDenied`，没有完整开发集指标。不编造结果，本步不纳入主链路。详见 `reports/rerank_dev.json`。

### 最终检索链路（开发集）

**切分 `section_window_120` + 密集检索与 BM25 的 RRF 融合，不加 rerank。** 配置见 `reports/locked_retrieval.json`。`python -m app.start` 会按该切分入库。

| 指标 | 最终链路 | 说明 |
| --- | --- | --- |
| Recall@5 | 0.7493 | 可答题 24 道宏平均 |
| Hit@5 | 21 / 24（0.8750） | 未命中 Q013、Q017、Q039 |
| MRR@5 | 0.6056 | |
| nDCG@5 | 0.6014 | |
| Recall@10 | 0.8410 | 对照指标，不是门禁 |
| Hit@10 | 23 / 24（0.9583） | |
| 单跳 Hit@5 | 15 / 17 | 单跳 Recall@5 为 0.8235 |
| 多跳 Hit@5 | 6 / 7 | 多跳 Recall@5 为 0.5690 |
| 拒答 | 未测 / 4 | 本段不跑生成 |
| 相对 B0 | 由错变对 2 题（Q008、Q015），由对变错 1 题（Q013） | McNemar p=1.0，方向为正但未达显著。Recall@5 上升 5 题、下降 2 题 |

相对 B0 的回归通过，并已将 `reports/last_passed_dev_recall.json` 更新为该链路。本地回归脚本（不必上 CI）：

```bash
python scripts/check_regression.py --current reports/hybrid_dev_eval.json
```

与上次通过的开发集逐题结果比，Hit@5 或 Recall@5 净减少超过 1 题则失败。不设绝对 90%，延迟不进硬门禁。

### 第一次测试集（不参与调参）

跑前校验封存 sha256，期望值与实测值均为 `2e4aa054c7843879db1c2982a5482313017055b61c60b2064855d782df22cf3e`，12 题一致。报告见 `reports/test_run1.json`。这些数字不回写到切分或检索选型。

| 指标 | 实测结果 | 说明 |
| --- | --- | --- |
| Recall@5 | 0.8476 | 可答题 10 道宏平均 |
| Hit@5 | 10 / 10（1.0000） | 前 5 条均至少命中 1 个金标切片 |
| MRR@5 | 0.5483 | |
| nDCG@5 | 0.6096 | |
| Recall@10 | 0.8810 | |
| Hit@10 | 10 / 10 | |
| 单跳 Hit@5 | 7 / 7 | 单跳 Recall@5 为 1.0000 |
| 多跳 Hit@5 | 3 / 3 | 多跳 Recall@5 为 0.4921（部分金标未进前 5） |
| 拒答 | 未测 / 2 | 本段只评检索，不跑生成 |


## B0 实测（开发集，只评检索）

在测试目录 `/tmp/policy-qa-stage2-test` 入库后，使用当前默认切分 `section_window_400`（130 个切片，一节一块）和纯密集检索（sqlite-vec 余弦距离）实测。不加 BM25、RRF、rerank，不跑生成，不跑测试集。数字来源见 `reports/b0_dev.json`、`reports/b0_dev_eval.json`。测量时间：2026-11-21 15:32:45（Asia/Shanghai）。

命令：

```bash
export POLICY_QA_HOME=/tmp/policy-qa-stage2-test
python -m app.start --ingest-only
python scripts/run_b0_dev.py
```

| 指标 | 实测结果 | 说明 |
| --- | --- | --- |
| Recall@5 | 0.6903 | 可答题 24 道宏平均；拒答 4 道不进分母 |
| Hit@5 | 20 / 24（0.8333） | 前 5 条至少命中 1 个金标切片 |
| MRR@5 | 0.5917 | |
| nDCG@5 | 0.5805 | 相关性按二元 |
| Recall@10 | 0.8271 | 对照指标，不是本段门禁 |
| Hit@10 | 22 / 24（0.9167） | |
| 单跳 Hit@5 | 13 / 17 | |
| 多跳 Hit@5 | 7 / 7 | 多跳 Recall@5 为 0.5095（部分金标未进前 5） |
| Hit@5 未命中 | Q008、Q015、Q017、Q039 | 高基线予以保留，不刻意压低 |
| 拒答 | 未测 / 4 | 本段不跑生成，按「对了几道 / 共几道」预留，不设 90% 目标 |
| 回归 | 无上次通过结果 | 当时将 B0 写入 `reports/last_passed_dev_recall.json`；第三段已把该文件更新为锁定链路。B0 原始数字仍见本表与 `reports/b0_dev.json` |

测试集 12 题本段未跑，封存 sha256 为 `2e4aa054c7843879db1c2982a5482313017055b61c60b2064855d782df22cf3e`。不得用测试集数字调参。若改动测试集，必须在提交说明中写明原因并更新哈希。

入库也在同一测试目录实测（报告：`reports/stage2_ingest.json`）。目录从第一段 15 篇库复制而来：第一次识别指纹变化并重新写入，13.209 秒，40 篇 / 130 切片，13 次 embedding 调用、发送 130 条文本，指纹 `2c4329130cba3de3a9ecd85913766cacb9032f8c1ed433eaf93214b02bd3e743`（2026-11-21 15:32:33，Asia/Shanghai）。第二次认出内容未变，0.014 秒跳过，0 次调用。未改动第一段测试目录，也未连接 Postgres / Redis。本段将向量表距离改为余弦（`distance_metric=cosine`）。

## 技术栈

- Python 3.11–3.13，FastAPI，官方 SDK 加自写编排
- 存储：SQLite；向量：sqlite-vec（2026-11-21 查 [PyPI 文件列表](https://pypi.org/pypi/sqlite-vec/0.1.9/json)，最新版 0.1.9 提供 `sqlite_vec-0.1.9-py3-none-win_amd64.whl`，因此不退回 Chroma）
- Embedding：沿用 E0 锁定的 `text-embedding-v4`，维度 1024 **从配置读取**，见 `docs/embedding-lock.md`
- 检索（第三段锁定）：`section_window_120` 切分，jieba + `rank_bm25.BM25Okapi`（k1=1.5，b=0.75）与密集检索经 RRF（k=60）融合；gte-rerank 因 API `AccessDenied` 未纳入
- 生成配置（本段不调用生成）：`QWEN_FAST_MODEL=qwen3.8-flash`，`QWEN_STRONG_MODEL=qwen-plus`
- 页面：Streamlit，仅监听 `127.0.0.1`
- 依赖与 Windows 安装依据：`requirements.txt`、`docs/windows-packages.md`

## 安装位置与数据位置

默认根目录是 **`D:/policy-qa`**（可用环境变量 `POLICY_QA_HOME` 改到其他 D 盘子目录，不要默认装到 C 盘）。

| 内容 | 默认位置 |
| --- | --- |
| 虚拟环境 | `D:/policy-qa/.venv` |
| 若使用已有 conda | `conda create --prefix D:/policy-qa/conda-env python=3.12`，不要装进 `(base)`，也不要默认写到 C 盘 |
| 数据库 | `D:/policy-qa/data/policy_qa.sqlite` |
| 启动日志 | `D:/policy-qa/logs/start-last.json`（不含密钥） |
| 密钥 | 仓库根目录或 `POLICY_QA_HOME` 下的本地 `.env`，不进仓库 |
| 代码与语料 | 本仓库（例如桌面上的项目目录）；40 篇语料在 `corpus/`，不会安装到 C 盘 |

在 Linux 开发机上把 `POLICY_QA_HOME` 换成对应路径即可，例如 `$HOME/policy-qa`。第二段评测目录是 `/tmp/policy-qa-stage2-test`；第三段是 `/tmp/policy-qa-stage3-test`。你自己电脑上的安装和数据默认仍在 `D:/policy-qa`。拉取本段之后首次启动会按锁定切分 `section_window_120` 重新切分并入库（157 个切片）；若本地还没有对应 embedding 缓存，会再次调用 embedding 接口。

## 本机运行（Windows）

需要已安装 Python 3.11、3.12 或 3.13（可用官方安装包或 conda，安装位置请选 D 盘）。然后在**本仓库根目录**执行：

```bat
python -m venv D:/policy-qa/.venv
D:/policy-qa/.venv/Scripts/activate
pip install -r requirements.txt
copy .env.example .env
```

用文本编辑器打开 `.env`，填写 `DASHSCOPE_API_KEY`，确认 `POLICY_QA_HOME=D:/policy-qa`。不要把真实密钥发给任何人。

```bat
python -m app.start
```

该命令会建库、按需写入语料并调用 embedding，然后启动 FastAPI（默认 `http://127.0.0.1:8000/health`）和 Streamlit（`http://127.0.0.1:8501`）。第二次只需再运行 `python -m app.start`：语料与配置未变时不会重复写入，也不会再次消耗 embedding 额度。若只想建库入库、不启动页面，使用 `python -m app.start --ingest-only`。

## 本机运行（Linux）

```bash
export POLICY_QA_HOME="$HOME/policy-qa"
python3 -m venv "$POLICY_QA_HOME/.venv"
source "$POLICY_QA_HOME/.venv/bin/activate"
pip install -r requirements.txt
cp .env.example .env
# 在 .env 中填写 DASHSCOPE_API_KEY，并把 POLICY_QA_HOME 改成上面的路径
python -m app.start
```

## 40 篇语料与 40 题评测集

从完整版 70 篇中选取 40 篇，口径不变，不把 70 篇全部搬入。清单见 `corpus/INDEX.md`。构成：政策 15 + 手册 10 + FAQ 9 + 干扰 6。干扰含 **N07 投毒样本**（错误声称星河手表 S3 仅 6 个月质保、且通用优先于 SKU）。

评测集 40 题：开发集 28、测试集 12；单跳 24、多跳 10、拒答 6（约 60% / 25% / 15%）。按文档簇整簇划分，不按题号随机切，也不对着检索结果标注。题号沿用完整版，题干均被这 40 篇覆盖。详情见 `eval/README.md`。

| 锁定题目 | 归属 | 口径 |
| --- | --- | --- |
| Q041 | 开发集，多跳，手机数码簇 | 按品类专项规定作答并说明依据 |
| Q042 | 测试集，多跳，质保期限簇 | 按 sku 优先答 24 个月，并说明它覆盖了通用 12 个月 |
| Q025 | 开发集，多跳，定制退货簇 | 定制类不支持七天无理由；质量问题退货运费由商家承担；无理由运费规则不适用 |
| Q039 | 开发集，单跳，时限簇 | 「已超 7 天、不能按无理由退」是可答的否定题，不进拒答集 |

标注使用 `doc_id + section_path + 字符起止`。每次切分后运行 `python scripts/map_annotations.py`，重叠超过 50% 的切片继承 relevance。第三段锁定切分 `section_window_120` 后重新映射：40 题 54 条条款跨度全部成功映射（开发集可答题映射出 43 对题-切片，测试集可答题 19 对），无未映射跨度，也没有对着检索结果改标注。
