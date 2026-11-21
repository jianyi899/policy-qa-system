# 政策问答系统（轻量版）

面向政策类文档的企业问答系统，以电商售后为验证场景。当前 **main 已改为轻量栈**：在 Windows 上用 Python 直接运行，不使用 Docker、Postgres、Redis 或本地大模型。完整版保留在注释 tag **[v1-full-postgres](https://github.com/jianyi899/policy-qa-system/releases/tag/v1-full-postgres)**（对应提交 `9468ee9`）；完整版数字不能当作轻量版结果。

## 实测对比

下面是轻量版已经实测的关键指标，数字均来自当时报告，未改写、未隐藏偏低的结果。测试集只在三个冻结点各跑一次，数字不参与调参，也不据此回改链路。

| 指标 | 实测值 | 来源 |
| --- | --- | --- |
| B0 开发集检索 Recall@5 / Hit@5 | 0.6903 / 20/24 | `reports/b0_dev.json` |
| 最终检索开发集 Recall@5 / Hit@5 | 0.7493 / 21/24 | `reports/locked_retrieval.json`、`reports/hybrid_dev.json` |
| 最终检索相对 B0 | 由错变对 2 题（Q008、Q015），由对变错 1 题（Q013）；McNemar p=1.0，方向为正但未达显著 | `reports/hybrid_dev.json` |
| 第一次测试集检索 Recall@5 / Hit@5 | 0.8476 / 10/10 | `reports/test_run1.json` |
| 第二次测试集检索 Recall@5 / Hit@5 | 0.8476 / 10/10；相对第一次由错变对 0、由对变错 0，无法做 McNemar | `reports/test_run2.json` |
| 第三次测试集检索 Recall@5 / Hit@5（交付前） | 0.8476 / 10/10；相对第二次由错变对 0、由对变错 0，无法做 McNemar | `reports/test_run3.json` |
| 开发集答题质量 / 拒答 | 21/24 / 4/4 | `reports/stage5_dev_citation.json` |
| 第二次测试集答题质量 / 拒答 / 要点覆盖 | 9/10（Q076 要点全对，但被拒答启发式误判，未回改） / 2/2 / 1.0 | `reports/test_run2.json` |
| 第三次测试集答题质量 / 拒答 / 要点覆盖 | 7/10 / 2/2 / 1.0 | `reports/test_run3.json` |
| 第三次相对第二次的答题对错变化 | 由错变对 0，由对变错 2 题（Q073、Q075）；McNemar p=0.5，方向为负但未达显著。Q073、Q075、Q076 要点覆盖均为 1.0，均因可答否定表述触发拒答启发式而被计错，未据此回改规则 | `reports/test_run3.json`、`reports/test_run3_generation_vs_run2.json` |
| 开发集句级引用宏精度 / 微精度 | 0.4527 / 0.4162（72/173） | `reports/stage5_dev_citation.json` |
| 第二次测试集句级引用宏精度 / 微精度 | 0.2731 / 0.2063（13/63） | `reports/test_run2.json` |
| 第三次测试集句级引用宏精度 / 微精度 | 0.2621 / 0.2000（13/65） | `reports/test_run3.json` |
| Q041（开发集，品类专项覆盖通用规则） | 按手机数码专项答对，并说明专项覆盖通用规则 | `reports/stage5_q041.json` |
| Q042（测试集，SKU 24 个月覆盖通用 12 个月） | 第二次、第三次均通过 | `reports/test_run2.json`、`reports/test_run3.json` |
| 主路径：冰箱买了 20 天质量问题还能退吗 | 先查到订单 `SO-FRIDGE-20D`，再按范围套政策；结论为可走质量问题退货，七天无理由窗口已过 | `reports/stage4_fridge_path.json` |

三次测试集跑前均校验封存 sha256，期望值与实测值均为 `2e4aa054c7843879db1c2982a5482313017055b61c60b2064855d782df22cf3e`。引用按句子算精度。拒答只报对了几道 / 共几道，小样本不设 90% 目标。级联阈值尚未标定，三次生成均默认 `qwen3.8-flash`，没有升到 `qwen-plus`，不编造阈值。

### 为什么这样定

- **切分选 `section_window_120`**：三种切分都是先映射标注，再在开发集上评。`section_window_120` 的开发集纯密集检索 Recall@5 为 0.6972，Hit@5 为 21/24，高于窗口 400（0.6903 / 20/24）和 `sentence_pack_150`（与窗口 400 并列）。详见第三段。
- **纳入 BM25 + RRF**：jieba + `rank_bm25.BM25Okapi`（k1=1.5，b=0.75）与密集检索经 RRF（k=60）融合后，开发集 Recall@5 从 0.6972 升到 0.7493，因此写入主链路。Hit@5 仍是 21/24。
- **不加 gte-rerank**：第一道开发集题即返回 `AccessDenied`，没有完整指标，不编造结果，也不纳入主链路。
- **生成改用 `qwen3.8-flash` 级联 `qwen-plus`**：`qwen-turbo` 已于 2026-10-10 官方下线。模型名放在配置里。因级联阈值尚未标定，主路径和评测都走快速模型。
- **存储用 SQLite + sqlite-vec，而不是 Postgres**：完整版依赖本机或 Docker 中的 Postgres / Redis，不方便在普通 Windows 电脑上直接安装。sqlite-vec 0.1.9 提供 Windows x64 安装包，因此不退回 Chroma。第一版不做退款写操作、缓存和 Langfuse。

演示视频见 [`docs/demo/policy-qa-demo-90s.mp4`](docs/demo/policy-qa-demo-90s.mp4)（约 90 秒，五段均为本仓库轻量版真实界面与真实作答）。台本见 `docs/demo/SCRIPT.md`。不要把这台开发机当成公网演示环境；第一版用视频兜底。

## 本机运行（Windows）

安装和数据默认都放 **`D:/policy-qa`**，不要装到 C 盘。也可用 `POLICY_QA_HOME` 改到其他 D 盘子目录。需要已安装 Python 3.11、3.12 或 3.13（安装位置请选 D 盘）。

最短步骤（在**本仓库根目录**执行）：

```bat
python -m venv D:/policy-qa/.venv
D:/policy-qa/.venv/Scripts/activate
pip install -r requirements.txt
copy .env.example .env
```

用文本编辑器打开 `.env`，填写 `DASHSCOPE_API_KEY`，确认 `POLICY_QA_HOME=D:/policy-qa`。不要把真实密钥发给任何人，也不要写入仓库。

然后一条命令启动：

```bat
python -m app.start
```

该命令会建库、按需写入语料并调用 embedding，写入演示账号与冰箱订单，然后启动 FastAPI（`http://127.0.0.1:8000/health`）和 Streamlit（`http://127.0.0.1:8501`，只监听 `127.0.0.1`）。第二次只需再运行同一条命令：语料与配置未变时不会重复调用 embedding，但会刷新演示订单的签收时间。

打开页面后，在左侧切换器选择「主路径用户（含冰箱订单 SO-FRIDGE-20D）」，即可演示「这单冰箱买了 20 天质量问题还能退吗」。不要在对话里输入身份。引用按句子展示。自动演示可打开 `http://127.0.0.1:8501/?demo=1`。

若使用已有 conda：`conda create --prefix D:/policy-qa/conda-env python=3.12`，不要装进当前的 `(base)`，也不要默认写到 C 盘。

| 内容 | 默认位置 |
| --- | --- |
| 虚拟环境 | `D:/policy-qa/.venv` |
| 数据库 | `D:/policy-qa/data/policy_qa.sqlite` |
| 启动日志 | `D:/policy-qa/logs/start-last.json`（不含密钥） |
| 密钥 | 仓库根目录或 `POLICY_QA_HOME` 下的本地 `.env`，不进仓库 |
| 代码与语料 | 本仓库（例如桌面上的项目目录） |

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

本仓库评测目录是 `/tmp/policy-qa-stageN-test`（第六段为 `/tmp/policy-qa-stage6-test`），不会改你电脑上的 `D:/policy-qa`，也不会连接本机已有的 Postgres / Redis。

## 技术栈

- Python 3.11–3.13，FastAPI，官方 SDK 加自写编排；不要使用 LangChain
- 存储：SQLite；向量：sqlite-vec（2026-11-21 查 [PyPI 文件列表](https://pypi.org/pypi/sqlite-vec/0.1.9/json)，最新版 0.1.9 提供 `sqlite_vec-0.1.9-py3-none-win_amd64.whl`，因此不退回 Chroma）
- Embedding：沿用 E0 锁定的 `text-embedding-v4`，维度 1024 **从配置读取**，见 `docs/embedding-lock.md`
- 检索（第三段锁定）：`section_window_120` 切分，jieba + `rank_bm25.BM25Okapi`（k1=1.5，b=0.75）与密集检索经 RRF（k=60）融合；gte-rerank 因 API `AccessDenied` 未纳入
- 生成：`QWEN_FAST_MODEL=qwen3.8-flash`（compatible-mode，`enable_thinking=False`）级联 `qwen-plus`。级联阈值尚未标定，评测与主路径均未升级
- 页面：Streamlit，仅监听 `127.0.0.1`
- 依赖与 Windows 安装依据：`requirements.txt`、`docs/windows-packages.md`

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

## 各段进度

- 第一段地基：SQLite 建表、锁定 `text-embedding-v4`（1024 维从配置读取）、一条命令启动。
- 第二段语料与评测集：语料补齐至 40 篇（含 N07 投毒）、40 题拆为开发集 28 / 测试集 12，条款跨度标注与映射脚本，以及开发集 B0 密集检索基线。
- 第三段检索优化：三种切分消融、BM25+RRF、gte-rerank 实测，本地回归脚本，以及第一次测试集评测。最终检索链路已写入 `reports/locked_retrieval.json`。
- 第四段查询工具与主路径：查订单、查物流；订单字段注入冲突解析；实测跑通「这单冰箱买了 20 天质量问题还能退吗」。
- 第五段多轮、引用与冲突消解：句级引用、冲突总序落地，用演示账号实测多轮上下文，并第二次跑测试集。
- **第六段交付（本段）**：README 对比表置顶、约 90 秒演示视频、交付前最后一次测试集。申请退款等写操作、缓存与 Langfuse 留给第二版。

## 第六段实测（交付前最后一次测试集）

在测试目录 `/tmp/policy-qa-stage6-test` 进行。安装与数据位置是该目录下的数据库 `/tmp/policy-qa-stage6-test/data/policy_qa.sqlite` 和日志；代码仍在本仓库。未默认写入 C 盘，也未改动 Windows 上的 `D:/policy-qa`，未连接 Postgres / Redis。测量时间：2026-11-21 16:08 至 16:10（Asia/Shanghai）。数字来源见 `reports/test_run3.json`、`reports/test_run3_retrieval_eval.json`、`reports/test_run3_generation.json`、`reports/test_run3_generation_vs_run2.json`。

命令：

```bash
export POLICY_QA_HOME=/tmp/policy-qa-stage6-test
python -m app.start --ingest-only
python scripts/run_stage6_test.py
```

跑前校验封存 sha256，期望值与实测值均为 `2e4aa054c7843879db1c2982a5482313017055b61c60b2064855d782df22cf3e`，12 题一致。入库认出指纹未变，跳过 embedding 调用。这些数字不参与调参，也不据此回改链路或拒答启发式。级联阈值仍未标定，12 题全部由 `qwen3.8-flash` 作答，0 题升级到 `qwen-plus`。

### 检索（与第一、二次同一口径：Hit@5 视为对/错）

| 指标 | 第三次实测 | 相对第二次 |
| --- | --- | --- |
| Recall@5 | 0.8476（可答题 10 道宏平均） | 相同 |
| Hit@5 | 10 / 10 | 由错变对 0，由对变错 0 |
| McNemar | 无分歧题目，无法检验 | 与第一次对照同样无法检验 |
| 拒答 | 检索不评拒答；生成为 2/2 | 与第二次相同 |

### 生成、引用与拒答（与第二次同一口径，不能和第一次只评检索的数字硬比）

| 指标 | 第二次 | 第三次 |
| --- | --- | --- |
| 答题质量 | 9/10 | 7/10 |
| 要点覆盖 | 1.0 | 1.0 |
| 拒答 | 2/2 | 2/2 |
| 句级引用宏精度 | 0.2731 | 0.2621 |
| 句级引用微精度 | 0.2063（13/63） | 0.2000（13/65） |
| Q042 | 通过 | 通过 |

由错变对 0 题，由对变错 2 题（Q073、Q075）。McNemar n=2，p=0.5，方向为负但未达显著。Q073、Q075 与第二次已误判的 Q076 一样：要点全对（覆盖率为 1.0），但回答里出现「不属于」等字样，被拒答启发式判成拒答。未为此回改规则。Q042 三项必查全部命中。

## 第五段实测（多轮、句级引用与冲突消解）

在测试目录 `/tmp/policy-qa-stage5-test` 进行。测量时间：2026-11-21 15:57 至 16:03（Asia/Shanghai）。数字与回答来源见 `reports/stage5_units.json`、`reports/stage5_conflict.json`、`reports/stage5_q041.json`、`reports/stage5_dev_citation.json`、`reports/stage5_citation_audit.json`、`reports/stage5_llm_judge.json`、`reports/stage5_multiturn.json`、`reports/test_run2.json`。

本段不改检索链路，也不标定级联阈值。生成模型仍是 `qwen3.8-flash`，`reason=no_threshold_always_fast`。测试集数字不参与调参。

开发集 24 道可答题：答题质量 21/24，拒答 4/4；句级引用宏精度 0.4527，微精度 0.4162（72/173）。第二次测试集检索与第一次相同：Recall@5 为 0.8476，Hit@5 为 10/10，由错变对 0、由对变错 0，无法做 McNemar。生成和引用是第五段首次实测：答题质量 9/10（Q076 要点全对，但被拒答启发式误判，没有为此改规则），要点覆盖 1.0，拒答 2/2；句级引用宏精度 0.2731，微精度 0.2063（13/63）。Q041、Q042 均按冲突总序答对。人工抽查 7 题与自动口径一致，同义 FAQ 句常被计错，会压低精度。

## 第三段检索优化与第一次测试集

三种切分都是先映射标注，再在开发集上评。

| 切分 | 切片数 | Recall@5 | Hit@5 | 未命中 | 相对 section_window_400 |
| --- | --- | --- | --- | --- | --- |
| section_window_400（B0） | 130 | 0.6903 | 20 / 24（0.8333） | Q008、Q015、Q017、Q039 | 基线 |
| **section_window_120（选定）** | 157 | **0.6972** | **21 / 24（0.8750）** | Q008、Q013、Q017 | 由错变对 2 题（Q015、Q039），由对变错 1 题（Q013）；McNemar p=1.0，方向为正但未达显著 |
| sentence_pack_150 | 139 | 0.6903 | 20 / 24（0.8333） | Q008、Q015、Q017、Q039 | 与 B0 切分 Hit@5 / Recall@5 完全一致，无分歧题目 |

加上 jieba BM25 和 RRF 后，开发集 Recall@5 升到 0.7493，Hit@5 仍是 21/24（由错变对 1 题 Q008，由对变错 1 题 Q039，McNemar p=1.0，无方向差异），所以写入主链路。gte-rerank 在第一道开发集题就返回 AccessDenied，没有完整指标，不纳入主链路。最终链路未命中的是 Q013、Q017、Q039。相对 B0：由错变对 2 题（Q008、Q015），由对变错 1 题（Q013），McNemar p=1.0，方向为正但未达显著。

第一次测试集（不参与调参）可答题 10 道，Hit@5 为 10/10，Recall@5 为 0.8476；拒答是「未测 / 2」。报告见 `reports/test_run1.json`。

本地回归脚本（不必上 CI）：

```bash
python scripts/check_regression.py --current reports/hybrid_dev_eval.json
```

与上次通过的开发集逐题结果比，Hit@5 或 Recall@5 净减少超过 1 题则失败。不设绝对 90%，延迟不进硬门禁。

## B0 实测（开发集，只评检索）

在测试目录 `/tmp/policy-qa-stage2-test` 入库后，使用当时默认切分 `section_window_400`（130 个切片）和纯密集检索实测。不加 BM25、RRF、rerank，不跑生成，不跑测试集。数字来源见 `reports/b0_dev.json`、`reports/b0_dev_eval.json`。测量时间：2026-11-21 15:32:45（Asia/Shanghai）。

| 指标 | 实测结果 | 说明 |
| --- | --- | --- |
| Recall@5 | 0.6903 | 可答题 24 道宏平均；拒答 4 道不进分母 |
| Hit@5 | 20 / 24（0.8333） | 前 5 条至少命中 1 个金标切片 |
| MRR@5 | 0.5917 | |
| nDCG@5 | 0.5805 | 相关性按二元 |
| Hit@5 未命中 | Q008、Q015、Q017、Q039 | 高基线予以保留，不刻意压低 |
| 拒答 | 未测 / 4 | 本段不跑生成，按「对了几道 / 共几道」预留，不设 90% 目标 |

## 第四段主路径

主路径提问是「这单冰箱买了 20 天质量问题还能退吗」。系统先查了订单，再按范围套政策。订单号 `SO-FRIDGE-20D`，SKU `SKU-HX-FRIDGE-BCD216`，类目「大家电」，签收时间按当时时钟往前约 20 个日历日，是有效会员，已购运费险。查订单的入参不含用户 ID；sku、类目、签收时间、是否会员、运费险都进了冲突解析。实测结论：可以申请退货，依据是质量问题，而不是七天无理由；超过 7 天不能走无理由，质量问题成立则运费由商家承担。用的是 `qwen3.8-flash`。对抗用例 5/5 通过。详见 `reports/stage4_fridge_path.json`。
