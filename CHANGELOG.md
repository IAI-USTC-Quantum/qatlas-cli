# Changelog

## Unreleased

### Fix

- 运行时版本统一读取发行包 metadata，并校验项目版本、锁文件和运行时一致。
- 修复插件 HTTP 层在 `--insecure` 首次告警时因缺少 `sys` 导入而崩溃；补充离线回归测试。

### CI

- 补齐锁内 Commitizen/Ruff/packaging、bump hooks 和默认分支/tag 的离线测试门禁。
- 锁内构建一次 wheel/sdist，保留原始 artifact，以 OIDC 发布 PyPI 后再附到 GitHub Release；新增精确版本/日志检查及防重复构建检查。
- 默认测试排除网络/生产集成用例并阻止意外外网访问。

## v0.34.0 (2026-09-12)

### Feat

- **`qatlas paper get figures`**：图注索引（`GET /api/papers/{id}/figures`）——图组、图注、单图下载 URL 的 JSON。
- **`qatlas paper get image ID NAME`**：单图下载（无需整包 zip）。
- **`qatlas paper status`** 支持多 ID：两个及以上自动切换批量端点 `GET /api/papers/status/batch`，单 ID 行为不变。

版本对齐 qatlasd 0.34.x 线（兼容协议：`(major, minor)` 相同即兼容）。

## v0.33.0 (2026-09-12)

### Feat

- **cli**: hint qatlas match as a known standalone plugin

## v0.32.1 (2026-09-12)

### Fix

- pin UTF-8 encoding for config/hosts YAML reads on non-UTF-8 locales

## v0.32.0 (2026-09-12)

版本对齐 qatlasd 0.32.x 线（兼容协议：`(major, minor)` 相同即兼容）。

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
