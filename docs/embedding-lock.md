# Embedding 锁定记录

本记录沿用完整版（tag `v1-full-postgres`，提交 `9468ee9`）在 **2026-11-18** 的 E0 实测结果，**不重新选型**。原始报告见 `reports/e0_result.json`（该文件从完整版保留到本轻量版仓库）。

## 当时实测

协议：`dashscope_small_scale_latency_and_dim`，样本数 3。候选均调用成功（HTTP 200）。

| 模型 | 实测维度 | 实测延迟 |
| --- | --- | --- |
| text-embedding-v3 | 1024 | 1763.82 ms |
| text-embedding-v4 | 1024 | 497.44 ms |
| text-embedding-v2 | 1536 | 586.82 ms |

选定：`text-embedding-v4`，维度 1024。选定理由以当时报告为准：该次实测中 v4 延迟低于同维度的 v3，且维度可从配置读取后用于建列。

## 本轻量版如何使用

- 模型锁定为 `text-embedding-v4`，不要更换；更换等于推翻已有实验。
- 维度 **1024 从配置项 `EMBEDDING_DIM` 读取**，建 SQLite / sqlite-vec 表时按该正整数创建，不在 SQL 或代码里写死 1024。
- `.env.example` 给出与 E0 一致的默认值：`EMBEDDING_MODEL=text-embedding-v4`，`EMBEDDING_DIM=1024`。
