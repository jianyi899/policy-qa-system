# 政策问答系统（第一段地基）

电商售后政策问答系统的仓库骨架与数据地基。本阶段只交付可运行的项目结构、数据库表、25 篇合成语料和 embedding 选型协议，不包含完整问答链路、评测题或调参。

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

# 5. 导入 25 篇语料
python scripts/import_corpus.py

# 6. 实测验证
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

25 篇合成语料位于 `corpus/policies`（P01–P20）和 `corpus/manuals`（M01–M05）。冲突排序口径与 Q042 / Q025 / Q039 / Q041 的预埋说明见 `corpus/INDEX.md`。

后续标注使用 `doc_id + section_path + 字符起止`，不直接使用 `chunk_id`。

## E0 embedding

已于 2026-11-18（Asia/Shanghai）用同一组 3 条中文政策句子，对 DashScope 候选模型做小规模实测并锁定，选定后不要更换。实测记录见 `reports/e0_result.json`。

- `text-embedding-v3`：成功，维度 1024，延迟 1763.82 ms
- `text-embedding-v4`：成功，维度 1024，延迟 497.44 ms（锁定）
- `text-embedding-v2`：成功，维度 1536，延迟 586.82 ms

按既定规则在成功结果中选择延迟最低者，锁定 `EMBEDDING_MODEL=text-embedding-v4`，并以实测维度 `EMBEDDING_DIM=1024` 建立 `chunk_embeddings`。该维度来自接口返回，不是预先写死。更换 embedding 会推翻后续全部实验，因此不再改动。

## 密钥

所有密钥只放在环境变量或本地 `.env`（已被 git 忽略）。仓库内 `.env.example` 只有变量名。
