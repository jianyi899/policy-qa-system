# 政策问答系统

电商售后政策问答系统。第一段交付了项目骨架、数据库表、语料导入和已锁定的 embedding；第二段补齐 70 篇语料、100 道评测题、条款跨度标注与映射，并在开发集上实测了 B0 基线；第三段完成了三种切分消融、BM25+RRF、gte-rerank 尝试、开发集 Recall@5 CI 门禁，以及测试集第一次评测。完整生成与拒答链路不在本阶段。

技术栈（已锁定，后续阶段沿用）：Python、FastAPI、官方 SDK 加自写编排、Postgres + pgvector、应用层 `rank_bm25.BM25Okapi`（jieba，k1=1.5，b=0.75）、RRF 融合、gte-rerank API、qwen-turbo 级联升到 qwen-plus（DeepSeek 只作行判或备用）、Langfuse Cloud、Streamlit、Docker Compose。不使用 LangChain 全家桶，不在本地跑 reranker，不自建 Langfuse。

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

## 密钥

所有密钥只放在环境变量或本地 `.env`（已被 git 忽略）。仓库内 `.env.example` 只有变量名。
