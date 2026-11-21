# 轻量版依赖的 Windows 安装依据

核查日期：2026-11-21。来源：各包在 PyPI 上的最新发行文件列表（`https://pypi.org/pypi/<name>/json`）。目标环境：Windows x64，Python 3.11–3.13。

| 包 | 当时最新版本 | Windows x64 / 纯 Python 依据 |
| --- | --- | --- |
| fastapi | 0.165.0 | `fastapi-0.165.0-py3-none-any.whl`（requires-python >=3.11） |
| uvicorn | 0.54.0 | `uvicorn-0.54.0-py3-none-any.whl` |
| pydantic-settings | 2.16.3 | `pydantic_settings-2.16.3-py3-none-any.whl`（requires-python >=3.11） |
| python-dotenv | 1.2.4 | `python_dotenv-1.2.4-py3-none-any.whl` |
| PyYAML | 6.0.4 | `pyyaml-6.0.4-cp311-cp311-win_amd64.whl`、`cp312`、`cp313` 均有 |
| httpx | 0.28.1 | `httpx-0.28.1-py3-none-any.whl` |
| dashscope | 2.1.1 | `dashscope-2.1.1-py3-none-any.whl` |
| rank-bm25 | 0.2.2 | `rank_bm25-0.2.2-py3-none-any.whl` |
| jieba | 0.42.1 | 仅有纯 Python sdist：`jieba-0.42.1.tar.gz`（无 wheel，无需编译） |
| streamlit | 1.67.0 | `streamlit-1.67.0-py3-none-any.whl`（requires-python >=3.11） |
| sqlite-vec | 0.1.9 | **有 Windows x64 wheel**：`sqlite_vec-0.1.9-py3-none-win_amd64.whl`（上传时间 2026-03-31T08:02:36.035538Z）。同版本还有 manylinux x86_64 / aarch64 与 macOS wheels。因此采用 SQLite + sqlite-vec，不退回到 Chroma。 |

未列入本轻量版：psycopg、redis、langfuse，以及任何本地大模型运行时。
