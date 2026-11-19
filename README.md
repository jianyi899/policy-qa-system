# 政策问答系统

电商售后政策问答系统。下面是各阶段已经实测的关键指标，数字均来自当时报告，未改写。演示用视频（见文末第七段），不要把本开发机当成公网演示环境；若部署到自有云主机，密钥只放服务端。

## 实测对比

| 指标 | 实测值 | 来源 |
| --- | --- | --- |
| 开发集检索 Recall@5 / Hit@5（锁定 hybrid） | 0.8469 / 0.9333（n=60） | `reports/hybrid_dev.json`、`reports/locked_retrieval.json` |
| 测试集检索 Recall@5 / Hit@5 | 0.9213 / 1.0（n=25） | 三次测试集相同：`reports/test_run1.json`、`test_run2.json`、`test_run3.json` |
| 开发集句级引用宏平均精度 / 微平均 / 宏平均召回 | 0.4268 / 0.3810（144/378） / 0.6066 | `reports/stage5_dev_citation.json` |
| 测试集句级引用宏平均精度 / 微平均（第二次） | 0.4252 / 0.3171（39/123） | `reports/test_run2.json` |
| 测试集句级引用宏平均精度 / 微平均 / 宏平均召回（第三次，最终） | 0.4291 / 0.3190（37/116） / 0.5651 | `reports/test_run3.json` |
| 拒答 | 开发集 10/10；测试集第二次 5/5、第三次 5/5 | `reports/stage5_dev_citation.json`、`test_run2.json`、`test_run3.json` |
| 缓存命中率 | 0.5（70/140；冷启动 70 次未命中，原句重复 70 次命中） | `reports/stage6_cache.json` |
| 同义变体误命中 | 0/8（不计入命中率分母） | `reports/stage6_cache.json` |
| 工具题绕过缓存 | 7/7 | `reports/stage6_cache.json` |
| 缓存命中首 token | 均值 10.62 ms，P50 10.39 ms，P95 12.47 ms，全部低于 800 ms；该路径本次请求 token 为 0 | `reports/stage6_replay.json` |
| 级联阈值（top-1 RRF）与开发集 task_ok | 阈值 0.03225806；task_ok 64/70，与全走 flash 相同 | `reports/stage6_cascade.json` |
| Q041（开发集，品类专项） | 4 项必查全部命中 | `reports/stage5_q041.json` |
| Q042（测试集，SKU 24 个月覆盖通用 12 个月） | 第二次、第三次均答对，三项必查全部命中 | `reports/test_run2.json`、`reports/test_run3.json` |

检索链路已锁定为 `section_window_400` + 密集检索与 BM25 的 RRF 融合，不采纳 rerank。生成默认 `qwen3.8-flash`（兼容模式，`enable_thinking=False`），达到阈值后升至 `qwen-plus`。测试集数字不参与调参。

第一段交付了项目骨架、数据库表、语料导入和已锁定的 embedding；第二段补齐 70 篇语料、100 道评测题、条款跨度标注与映射，并在开发集上实测了 B0 基线；第三段完成了三种切分消融、BM25+RRF、gte-rerank 尝试、开发集 Recall@5 CI 门禁，以及测试集第一次评测；第四段接上查订单、查物流、退款和日报工具，跑通冰箱质量问题主路径，并实测对抗用例无错误副作用；第五段补上多轮对话、句级引用与冲突消解收尾，并完成测试集第二次评测；第六段补上纯政策问答与检索缓存、Langfuse Cloud（日本区域）追踪，以及在开发集上标定的 qwen3.8-flash 升 qwen-plus 级联，并用开发集做了可复现流量回放；第七段把对比表放在开篇，完成测试集最终一次评测，并用本机 Streamlit 真实界面录制约 90 秒五段演示视频。

技术栈（已锁定，后续阶段沿用）：Python、FastAPI、官方 SDK 加自写编排、Postgres + pgvector、应用层 `rank_bm25.BM25Okapi`（jieba，k1=1.5，b=0.75）、RRF 融合、gte-rerank API、qwen3.8-flash 级联升到 qwen-plus（原锁定 qwen-turbo，已于 2026-10-10 官方下线；DeepSeek 只作行判或备用）、Langfuse Cloud、Streamlit、Docker Compose。不使用 LangChain 全家桶，不在本地跑 reranker，不自建 Langfuse。

## 本机拉起（当前开发机）

本仓库附带 `docker-compose.yml`（Postgres + pgvector、Redis），便于在其他环境部署。当前开发机未使用 Docker，而是本机安装了 PostgreSQL 17 与 Redis 8，并用 `pg_ctlcluster` / `redis-server` 直接启动（没有 systemd）。

```bash
# 1. 启动本机服务
bash scripts/start_local_services.sh

# 2. 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock

# 3. 配置环境变量（不要把真实密钥写入仓库）
cp .env.example .env
# 本机默认可通过 Unix socket、用户 box、数据库 policy_qa 连接，可不填密码。
# 检索评测需要环境中的 DASHSCOPE_API_KEY，不要把密钥写进仓库或提交。

# 4. 建表（E0 锁定后按实测维度 1024 创建 chunk_embeddings）
python db/migrate.py

# 5. 导入 70 篇语料（documents + sections）
python scripts/import_corpus.py

# 6. 按已锁定切分写入 embedding
python scripts/chunk_and_embed.py --strategy locked

# 7. 条款跨度映射与检索评测（开发集）
python scripts/map_annotations.py
python scripts/eval_retrieval.py --split dev --retriever hybrid --out reports/hybrid_dev_eval.json

# 8. 本机 Recall@5 门禁（对比 reports/ci_baseline.json，不跑测试集）
python scripts/ci_check_recall.py --current reports/hybrid_dev_eval.json --baseline reports/ci_baseline.json

# 9. 地基验证与健康检查
python scripts/verify_foundation.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
# 另开终端：curl http://127.0.0.1:8000/health

# 10. 第四段：写入可复现种子，跑冰箱主路径与对抗用例
python db/migrate.py
python scripts/seed_stage4.py
python scripts/run_stage4_fridge_path.py
python scripts/run_stage4_adversarial.py

# 11. 第五段：多轮、句级引用、Q041 与开发集引用精度，以及测试集第二次评测
python scripts/run_stage5_units.py
python scripts/run_stage5_multiturn.py
python scripts/run_stage5_dev.py
python scripts/run_stage5_test.py   # 仅在引用与冲突模块收尾后跑一次，数字不参与调参

# 12. 第六段：缓存规则、级联阈值标定、Langfuse 验证与流量回放（不跑测试集调参）
# 需要环境中的 DASHSCOPE_API_KEY、LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY
# 以及 LANGFUSE_BASE_URL=https://jp.cloud.langfuse.com（不要把密钥写入仓库）
python scripts/run_stage6_units.py
python scripts/run_stage6_cache.py
python scripts/calibrate_cascade.py
python scripts/run_stage6_replay.py

# 13. 第七段：最终测试集（只跑一次，数字不参与调参）与本机演示
# 测试集数字不得用于调参。演示界面只监听 127.0.0.1，不要把本机暴露到公网。
python scripts/run_stage7_test.py
streamlit run app/streamlit_app.py --server.address 127.0.0.1 --server.port 8501
# 浏览器打开 http://127.0.0.1:8501/ ，或 http://127.0.0.1:8501/?demo=1 自动连播五段。
# 本机录制约 90 秒演示视频：
# bash scripts/record_stage7_demo.sh
```

## 使用 Docker Compose 的环境

```bash
export POSTGRES_PASSWORD=...   # 在 shell 环境中提供，不要写入仓库
docker compose up -d
# 将 POSTGRES_HOST=127.0.0.1、POSTGRES_USER、POSTGRES_PASSWORD、POSTGRES_DB 写入本地 .env
python db/migrate.py
python scripts/import_corpus.py
```

## 语料与口径

70 篇合成语料位于 `corpus/policies`（P01–P20）、`corpus/manuals`（M01–M25）、`corpus/faqs`（F01–F15）和 `corpus/noise`（N01–N10，干扰语料；其中 N07 为投毒样本）。冲突排序口径与 Q042 / Q025 / Q039 / Q041 的预埋说明见 `corpus/INDEX.md`。干扰文档可以进入检索，但不得被当成正式政策覆盖正确答案。

评测标注使用 `doc_id + section_path + 字符起止`，不直接使用 `chunk_id`。题集、拆分与测试集 sha256 见 `eval/`。

## E0 embedding

已于 2026-11-18（Asia/Shanghai）用同一组 3 条中文政策句子，对 DashScope 候选模型做小规模实测并锁定，选定后不要更换。实测记录见 `reports/e0_result.json`。

- `text-embedding-v3`：成功，维度 1024，延迟 1763.82 ms
- `text-embedding-v4`：成功，维度 1024，延迟 497.44 ms（锁定）
- `text-embedding-v2`：成功，维度 1536，延迟 586.82 ms

按既定规则在成功结果中选择延迟最低者，锁定 `EMBEDDING_MODEL=text-embedding-v4`，并以实测维度 `EMBEDDING_DIM=1024` 建立 `chunk_embeddings`。该维度来自接口返回，不是预先写死。更换 embedding 会推翻后续全部实验，因此不再改动。

## 评测、B0 与第三段检索链路

- 100 题按文档簇拆成开发集 70 / 测试集 30；Q041 在开发集，Q042 在测试集。测试集 sha256 见 `eval/testset.sha256`，封存值 `092576646b41d9c35a3390e330ea62c2e62b0f00ab4ab78a32cb0220b3fc6a59`。
- 映射规则：切片与条款跨度在同一 `doc_id + section_path` 下的字符交集长度，除以条款跨度长度，大于 0.5 时切片继承 relevance。每次重切分都先映射再评测。
- B0（开发集、纯密集检索，切分 `section_window_chars` 400/80/40，221 切片）：Recall@5=0.7844，Hit@5=0.8500，分母为 60 道有金标切片的可答题。见 `reports/b0_dev.json`。
- 第三段在开发集上比较了三种会得到不同切片集的切分（详见 `reports/chunking_ablation.json`），并在选定切分上实测了 BM25+RRF 与 gte-rerank。测试集数字不参与选型或调参。
  - `section_window_400`（B0）：221 切片，开发集密集检索 Recall@5=0.7844
  - `section_window_120`（chunk_size=120，overlap=24，min_tail=15）：255 切片，Recall@5=0.7428；相对 B0 错→对 2、对→错 3，McNemar p=1.0，方向为负但未达显著
  - `sentence_pack_150`（按句打包，max_chars=150）：232 切片，Recall@5=0.7844，与 B0 逐题结果相同
  - 选定 `section_window_400`：Recall@5 最高（与按句打包并列），按规则保留 B0 切分
  - BM25+RRF（jieba，k1=1.5，b=0.75，RRF k=60）开发集 Recall@5=0.8469；相对纯密集错→对 6、对→错 1，McNemar p=0.125，方向为正但未达显著。因 Recall@5 更高，纳入主链路
  - DashScope `gte-rerank` 返回 `AccessDenied`，不编造结果，不纳入主链路
- 锁定的最终检索链路：`section_window_400` + 密集与 BM25 的 RRF 融合，不加 rerank，不跑生成。配置见 `reports/locked_retrieval.json`。
- 测试集第一次评测（只跑锁定链路）见 `reports/test_run1.json`：Recall@5=0.9213，分母=25 道有金标切片的可答题（30 题中排除 5 道拒答题）。本段只评检索。封存 sha256 核对一致。

## CI

GitHub Actions 工作流：`.github/workflows/retrieval-ci.yml`。只卡开发集 Recall@5：与 `reports/ci_baseline.json` 的逐题结果比，recall 下降题数减去上升题数，净减少超过 3 题则失败。不设绝对 90% 门槛，延迟不进硬门禁，测试集不参与决策。本机可用 `scripts/ci_check_recall.py` 对已有评测报告做同样检查。

工作流从 GitHub Actions secrets 读取 `DASHSCOPE_API_KEY`，不会把密钥写入仓库。若仓库尚未配置该 secret，工作流会跳过评测并说明原因，不视为门禁失败。需要的话，请在仓库 Settings > Secrets and variables > Actions 中添加同名 secret，不要把密钥发在聊天里。


## 第四段：工具与主路径

四个工具的签名都不含 `user_id`，只从服务端注入的会话上下文取当前用户：

- `query_order(order_no?, keyword?)`：只读。必须返回 sku、类目、签收时间、是否会员、运费险，并把这些字段注入冲突解析（先按 sku → 品类 → 通用套政策，范围相同再比政策 → 手册 → FAQ，最后比生效日）。干扰语料与范围不匹配的专项条款不参与适用。
- `query_logistics(order_no?, tracking_no?)`：只读本库运单与轨迹，不对接外部物流 API。
- `refund(action=query|apply, order_no?, refund_no?, reason?, amount_cents?, idempotency_key?)`：查询只读；申请时同一订单只能有一笔进行中退款，由数据库约束 `ux_refunds_one_open_per_order` 保证。幂等键用于识别同一次提交的重试，不含申请当日或理由。
- `daily_report(action=query|generate, report_date?)`：query 只读实时聚合；generate 才按 Asia/Shanghai 自然日幂等写入 `daily_reports` 快照，不改订单或退款。

主路径「这单冰箱买了 20 天质量问题还能退吗」必须先真实查订单，不能只凭用户口述。生成模型为 `qwen3.8-flash`（因 `qwen-turbo` 已于 2026-10-10 官方下线而替换，级联目标仍为 `qwen-plus`；调用时关闭默认思考模式）。种子脚本、主路径与对抗用例报告见 `reports/stage4_seed.json`、`reports/stage4_fridge_path.json`、`reports/stage4_adversarial.json`。

## 第五段：多轮、句级引用与冲突消解

生成模型仍为 `qwen3.8-flash`（兼容模式，`enable_thinking=False`），本段未升级到 `qwen-plus`，也未用 DeepSeek 顶替生成。

- 多轮对话会复用本会话已查过的订单事实。冰箱主路径可复现追问见 `reports/stage5_multiturn.json`：先问「这单冰箱买了 20 天质量问题还能退吗」，再问「那运费呢？」「那会员能延长七天无理由吗？」，后两轮复用订单 `SO-FRIDGE-20D`，未重新丢上下文。
- 回答带句级引用，落在 `doc_id + section_path + 字符起止`（与条款标注同一 `documents.body` 坐标系）。引用精度按句子算：系统输出的 `[S#]` 映射到证据句后，若该句与任一金标条款跨度在同一章节下字符交集长度大于 0 则计为正确。不对着检索结果改标注。开发集（60 道可答题，全部生成成功且均有引用）宏平均精度 0.4268，微平均精度 0.3810（144/378），宏平均句级召回 0.6066。详见 `reports/stage5_dev_citation.json`。
- 冲突消解会说明更细范围为何覆盖更一般规则。开发集 Q041 实测按「手机数码」品类专项答对：激活后不适用七天无理由，仅支持质量问题，签收之日起 15 日内可申请退换，并写明品类规定优先于通用规则。报告见 `reports/stage5_q041.json`。
- 测试集第二次评测（数字不参与调参）见 `reports/test_run2.json`。封存 sha256 仍为 `092576646b41d9c35a3390e330ea62c2e62b0f00ab4ab78a32cb0220b3fc6a59`，与第一次一致，题目未改。检索指标与第一次相同：Recall@5=0.9213，Hit@5=1.0（分母=25 道有金标切片的可答题）；错→对 0、对→错 0，无分歧题目，无法做 McNemar 检验。第一次只评检索，故生成/引用/拒答为首次实测：测试集可答题句级引用宏平均精度 0.4252、微平均 0.3171（39/123）；拒答 5/5。Q042（测试集）按 SKU 专项答 24 个月，并说明覆盖通用 12 个月。

## 第六段：缓存、Langfuse 与模型级联

默认生成模型仍是 `qwen3.8-flash`，在最终检索链路（`section_window_400` + dense+BM25 RRF）的 top-1 RRF 分数达到阈值时升到 `qwen-plus`。两者都走官方兼容模式，`enable_thinking=False`。本段未改评测集，也未用测试集调参。

- 缓存只覆盖纯政策问答和检索结果，键为规范化后的精确文本（Unicode NFKC 与空白折叠，不做同义改写）。工具类在路由层绕过，不得读写缓存。开发集 70 道可缓存政策题按「先冷启动再原句重复」回放，命中率分母不含同义变体和工具类：140 次中命中 70 次，命中率 0.5；8 条同义变体误命中 0；7 道工具题全部绕过。见 `reports/stage6_units.json`、`reports/stage6_cache.json`。
- Langfuse Cloud 使用官方日本区域 `https://jp.cloud.langfuse.com`，不自建。已用官方 API 回读真实 trace，例如缓存命中 `d2a59f7c6097829d512a9a7815ddefc5`（handle_ask），未缓存政策问答 `b6e038c4b46a92ecb24f3820d298ce1f`（含 hybrid_retrieve 与 generate_answer），工具调用 `c3d295935182264915a58876bc230de1`（query_order）。详见 `reports/stage6_replay.json`。
- 级联阈值在开发集 70 题上按 top-1 RRF 升序均分成 5 箱标定。选定阈值 0.03225806（分数小于等于该值时升到 qwen-plus）。该阈值下开发集 task_ok 64/70（0.9143），与全走 flash 相同，宏平均引用召回 0.6106、要点覆盖召回 0.975，略高于全走 flash（0.6066 / 0.9694）；全走 plus 为 63/70，并不更好。箱内质量不是单调的：第 0 箱 plus 更差，第 1 箱 plus 更好。自评 confidence 对比实验里，阈值 0.95 时 task_ok 可到 66/70，但要额外调用一次快速模型，且全走 plus 本身没有增益，因此不把自评支路留在产品路径。标定过程见 `reports/stage6_cascade.json`，阈值见 `reports/cascade_threshold.json`。
- 模拟流量回放在本机预发（uvicorn `127.0.0.1:8016`）测量。缓存命中的 20 道政策问答首 token：均值 10.62 ms，P50 10.39 ms，P95 12.47 ms，最小 8.72 ms，最大 16.07 ms，全部低于 800 ms；该路径本次请求 token 为 0。刊例价换算来源为阿里云百炼价格页（华北 2 北京，非思考模式）。报告见 `reports/stage6_replay.json`。

## 第七段：对比表交付、最终测试集与演示视频

本段不再把公网演示绑在这台开发机上。演示用视频，见 `docs/demo/policy-qa-demo-90s.mp4`（台本见 `docs/demo/SCRIPT.md`）。界面为 Streamlit，只监听 `127.0.0.1:8501`。若部署到自有云主机，密钥只放服务端环境变量，不要写入仓库，也不要提交到 GitHub Secrets 以外的明文位置。

测试集第三次也是最后一次评测见 `reports/test_run3.json`。封存 sha256 仍为 `092576646b41d9c35a3390e330ea62c2e62b0f00ab4ab78a32cb0220b3fc6a59`，与第一、二次一致，题目未改。数字不参与调参。

- 检索与前两次完全相同：Recall@5=0.9213，Hit@5=1.0（分母=25 道有金标切片的可答题）。相对 test_run1、test_run2 均为错→对 0、对→错 0，无分歧题目，无法做 McNemar 检验。高基线是好事。
- 生成走产品路径：默认 `qwen3.8-flash`，按已标定阈值级联升至 `qwen-plus`（本次 30 题中 20 题走 flash、10 题升 plus）；评测时关闭问答缓存。可答题全部生成成功且都有引用：宏平均精度 0.4291，微平均 0.3190（37/116），宏平均句级召回 0.5651。拒答 5/5。
- 与 test_run2 的生成对照（task_ok 口径与第六段开发集级联标定相同，用已保存的第二次回答重算，未重跑）：task_ok 由 27/30 变为 28/30（可答题 22/25→23/25，拒答两次都是 5/5）。错→对 2（Q076、Q090）、对→错 1（Q075），McNemar p=1.0，方向为正但未达显著。第一次测试集未跑生成，故生成项没有与 test_run1 的对照。
- Q042 仍答对 24 个月，并说明 SKU 专项覆盖了通用 12 个月。三项必查全部命中。


## 密钥

所有密钥只放在环境变量或本地 `.env`（已被 git 忽略）。仓库内 `.env.example` 只有占位符和日本区域默认 `LANGFUSE_BASE_URL`，没有真实密钥。
