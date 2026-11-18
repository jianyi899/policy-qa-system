# 政策问答系统（第一段地基）

电商售后政策问答系统。第一段交付了项目骨架、数据库表、25 篇语料和已锁定的 embedding；第二段补齐 70 篇语料、100 道评测题、条款跨度标注与映射、检索评测脚本，并在开发集上实测了 B0 基线。完整问答链路与切分／检索消融不在本阶段。

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
# 若要做 E0 embedding 选型，在环境中提供 DASHSCOPE_API_KEY，不要把密钥写进仓库或提交。

# 4. 建表（E0 锁定后按实测维度 1024 创建 chunk_embeddings）
python db/migrate.py

# 5. 导入 70 篇语料（documents + sections）
python scripts/import_corpus.py

# 6. 默认切分并写入 embedding（需环境中的 DASHSCOPE_API_KEY，不要写入仓库）
python scripts/chunk_and_embed.py

# 7. 条款跨度映射与开发集 B0 评测
python scripts/map_annotations.py
python scripts/eval_retrieval.py --split dev --out reports/b0_dev_eval.json

# 8. 地基验证
python scripts/verify_foundation.py

# 7. 健康检查
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

## 评测与 B0

- 100 题按文档簇拆成开发集 70 / 测试集 30；Q041 在开发集，Q042 在测试集。测试集 sha256 见 `eval/testset.sha256`。
- 默认切分：章节内字符窗口（`chunk_size=400`，`overlap=80`）。映射规则：切片与条款跨度重叠超过条款长度的 50% 则继承 relevance。
- B0 只在开发集上跑：锁定的 `text-embedding-v4` / 1024 纯密集检索，不加 BM25、RRF、gte-rerank，不跑生成。实测结果见 `reports/b0_dev.json`。本阶段不跑测试集。

## 密钥


所有密钥只放在环境变量或本地 `.env`（已被 git 忽略）。仓库内 `.env.example` 只有变量名。
