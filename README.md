# 政策问答系统（轻量版）

面向政策类文档的企业问答系统，以电商售后为验证场景。当前仓库的 **main 已改为轻量栈**：在 Windows 上用 Python 直接运行，不使用 Docker、Postgres、Redis 或本地大模型。

完整版（Postgres + pgvector + Redis + Docker Compose，含 70 篇语料与当时评测报告）保留在注释 tag **[v1-full-postgres](https://github.com/jianyi899/policy-qa-system/releases/tag/v1-full-postgres)**（对应提交 `9468ee9`）。完整版评测数字以该 tag 中的 README 与 `reports/` 为准；轻量版第一段尚未重跑评测，这里不引用、不改写那些数字。

改成轻量栈的原因：完整版依赖本机或 Docker 中的 Postgres / Redis，不方便在普通 Windows 电脑上直接安装和启动。生成模型方面，`qwen-turbo` 已于 2026-10-10 官方下线，配置改为 `qwen3.8-flash`（DashScope compatible-mode，`enable_thinking=False`）级联 `qwen-plus`。

## 第一段范围

已完成：SQLite 建表（含 users、orders、logistics 以及 documents / sections / chunks / 向量表）、先写入 15 篇语料、锁定 embedding，并用一条命令启动 FastAPI 与 Streamlit。

第一版后续才会接入：检索优化、查订单 / 查物流、句级引用、冲突消解、评测与演示视频。申请退款等写操作、缓存与 Langfuse 留给第二版。不要使用 LangChain。

## 技术栈

- Python 3.11–3.13，FastAPI，官方 SDK 加自写编排
- 存储：SQLite；向量：sqlite-vec（2026-11-21 查 [PyPI 文件列表](https://pypi.org/pypi/sqlite-vec/0.1.9/json)，最新版 0.1.9 提供 `sqlite_vec-0.1.9-py3-none-win_amd64.whl`，因此不退回 Chroma）
- Embedding：沿用 E0 锁定的 `text-embedding-v4`，维度 1024 **从配置读取**，见 `docs/embedding-lock.md`
- 生成配置（第一段只落地配置，不调用生成）：`QWEN_FAST_MODEL=qwen3.8-flash`，`QWEN_STRONG_MODEL=qwen-plus`
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
| 代码与语料 | 本仓库（例如桌面上的项目目录）；15 篇语料在 `corpus/`，不会安装到 C 盘 |

在 Linux 开发机上把 `POLICY_QA_HOME` 换成对应路径即可，例如 `$HOME/policy-qa`。

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

该命令会建库、按需写入语料并调用 embedding，然后启动 FastAPI（默认 `http://127.0.0.1:8000/health`）和 Streamlit（`http://127.0.0.1:8501`）。第二次只需再运行 `python -m app.start`：语料与配置未变时不会重复写入，也不会再次消耗 embedding 额度。

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

## 15 篇语料

从完整版语料中选取，口径不变，清单见 `corpus/INDEX.md`。覆盖：

- 冰箱 / 大家电主路径所需的通用七天无理由、质量问题与质保条款（语料不另设大家电专项，避免编造期限）
- Q041 手机数码专项
- Q042 星河手表 S3（`SKU-HX-WATCH-S3`）24 个月覆盖通用 12 个月
- Q025 定制类与运费口径
- Q039 已超 7 天、不能按无理由退（可答的否定题，不是拒答）
