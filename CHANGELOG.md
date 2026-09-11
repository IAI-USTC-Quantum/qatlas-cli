# Changelog

## Unreleased

- **`qatlas paper fetch`**：批量提交下载（`POST /api/downloader/fetch`，
  `papers:write`）——混合 DOI / arXiv ID / 论文链接，单次 ≤50 条，支持
  `--file`；输出逐项入队结果与 enqueued 汇总。
- **`qatlas paper jobs`**：下载进度（`papers:read`）——本地任务快照与
  counters，`--remote` 查询持久化 outbound fleet 快照，`--watch` 轮询至
  空闲，`--json` 机器可读（watch 模式 JSON lines）。
- **`qatlas paper list`**：目录检索（`GET /api/papers`）——`--has-md` /
  `--status` / `-q` / 身份精确过滤 / 分页排序。
- **`qatlas paper lookup`**：批量引用解析（`GET /api/papers/lookup`）——
  `arxiv:` / `doi:` / `openalex:` 引用 ≤200 条，报告 hosted / has_md。
- **插件协议 v2**：`CommandSpec.handler` 支持 `(ctx, argv)` 签名，`ctx` 为
  `CliContext`（已解析 server URL / token / 超时 / TLS）；新增插件公共
  HTTP 层 `qatlas.client.pluginsupport`（`server_request` /
  `format_api_error` / `poll_lro`）；`PLUGIN_API_VERSION` 与
  `cli_api_version` 版本协商（过新插件跳过 + 一行警告）；
  `CommandSpec.usage` 在 `qatlas --help` 展示。v1 `(argv)` 签名完全兼容。

## 0.22.1

- 客户端/服务端兼容策略调整：由「客户端 x.y ≥ 服务端 x.y」改为「**(major, minor)
  相同即兼容**」，patch 位随意漂移（如 qatlasd 0.22.4 ↔ qatlas-cli 0.22.3）。
  两个方向不一致都会给 stderr 警告（带双方版本号与建议动作）；写请求遇更新的
  服务端仍硬失败（exit code 4）。
- 升级提示由过时的 `pip install --upgrade quantum-atlas` 改为
  `uv tool upgrade qatlas-cli`。
- 与 qatlasd 0.22.x 任意 patch 版本兼容（本次 patch 发布即为该契约的演练）。

## 0.22.0

- 仓库迁移至 IAI-USTC-Quantum/qatlas-cli；安装方式与版本血统继承自
  主仓 `quantum-atlas` 包。
- 新增 PyPI Trusted Publishing 发布流程（tag `v*` 触发）。

## 0.21.0a3

- CLI 从主仓 QuantumAtlas 拆分为独立仓库；版本号自主仓 `quantum-atlas`
  包的 0.21.0a3 继承延续，不重新从 0.x 计数。
- 安装方式与拆分前一致：`uv tool install qatlas-cli`（PyPI）。
- 发布流程：commitizen（`cz bump`，tag 格式 `v<version>`）打 tag 后由
  `.github/workflows/release.yml` 经 PyPI Trusted Publishing 自动发布。

## v0.24.0 (2026-09-07)

### Feat

- **paper**: get metadata 子命令——按 qa_/arXiv/DOI 拉取论文元数据

## v0.23.0 (2026-08-29)

### Feat

- qatlas rag 命令的插件提示

## v0.22.1 (2026-08-26)

### Feat

- x.y compatibility policy; drop paper-get-pdf; add paper-get-images

## v0.22.0 (2026-08-25)

### Fix

- **ci**: grant contents:read so actions/checkout can fetch the tag

## v0.21.0a3 (2026-08-25)
