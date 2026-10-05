# qatlas-cli — QuantumAtlas 的命令行客户端

> QuantumAtlas 的 `qatlas` 命令行客户端：面向用户侧的论文获取、贡献上传、
> 本地解析等工作流，通过 HTTP 与服务端 qatlasd 交互。本仓库是主仓
> QuantumAtlas 拆出的独立客户端仓库，只保留客户端与本地工具，不含服务端代码。

所有服务端通信都是 `requests` 直发的 HTTP 调用，无重量级 SDK 依赖。

## 安装

> **旧包迁移**：CLI 已拆分为独立的 PyPI 包 **`qatlas-cli`**，不再随
> 主仓的 `quantum-atlas` 包发布。此前通过 uv tool 安装旧工具的用户，应先运行
> `uv tool uninstall quantum-atlas`，再安装下面的新包，避免旧命令入口冲突。
> 已有用户配置（`~/.config/qatlas/config.yaml`、PAT 等）保留；旧包中的 Python
> 帮助库不属于 CLI 的等价替代范围。

从 PyPI 安装为全局 CLI 工具（推荐，与拆分前 `quantum-atlas` 的安装方式一致）：

```bash
# 推荐：uv 全局工具（隔离环境 + 升级方便）
uv tool install qatlas-cli

# 或 pipx
pipx install qatlas-cli

# 或 plain pip
pip install qatlas-cli

qatlas --help
```

也可以直接从 GitHub 安装：

```bash
uv tool install --from git+ssh://git@github.com/IAI-USTC-Quantum/qatlas-cli.git qatlas-cli
```

本地 editable 安装（开发用）：

```bash
git clone git@github.com:IAI-USTC-Quantum/qatlas-cli.git
cd qatlas-cli
uv sync --locked --extra dev # 项目内 .venv，按锁文件 editable 安装本包
# 或装入当前环境：
uv pip install -e .
```

## 命令概览

```
qatlas config    # 管理用户级配置文件（~/.config/qatlas/config.yaml）
qatlas auth      # 管理各 host 的 PAT / session token（login / status / token / logout）
qatlas paper     # 论文工作流：get markdown/images/metadata、status、mineru-lease，
                 #   目录检索 list / lookup，批量下载 fetch 与进度 jobs；
                 #   外部来源登记 source-register（title / authors / year 必填）；
                 #   固定来源 PDF：pdf；可定位续读的派生 JSON：read；
                 #   块级评论原件：source-list / parse-list / parse-json /
                 #   block-list / block-get / block-image
qatlas comments  # 块级讨论：list / show / create / reply / status / edit
qatlas contrib   # 贡献者工作流：上传 PDF（contrib pdf）或本地跑 MinerU 再推送（contrib mineru）
qatlas parser    # 抓取并解析 arXiv 论文（本地工作区命令）
```

别名：`papers` → `paper`，`parse` → `parser`。

完整子命令参考（config / auth / paper / contrib / parser 的全部子命令、
标志与示例，含论文搜索 `qatlas search` 的详细用法）见文档站
<https://qatlas.hfnl.app.chenzhaoyun.com/doc/guide/cli.html>
（默认线上实例；源码：主仓 `QuantumAtlas/docsite/guide/cli.rst`，
任一 qatlasd 实例的 `/doc/guide/cli.html` 均可访问）。

```bash
qatlas auth login                                   # 设备码 / PAT 登录
qatlas paper get markdown quant-ph/9508027 --output paper.md
qatlas paper get metadata quant-ph/9508027          # 论文元数据（JSON：paper_id / ids / title / authors / assets；不含 abstract）
qatlas contrib pdf quant-ph/9508027v1 --pdf paper.pdf
qatlas contrib mineru 2501.00010v1
```

### 从检索到统一身份、正文与贡献

安装 search / match 插件后，研究工作可复用下面的顺序；不需要在客户端启动
qatlasd、数据库或 MinerU 服务。

```bash
qatlas search '"variable time amplitude amplification"' --no-agent --json
qatlas match --arxiv 1010.4458 --json
qatlas paper get metadata qa_…
qatlas paper get markdown qa_…             # 默认 stdout；缓存未命中时由服务端抓取/解析
qatlas paper pdf qa_… --version v2 -o paper.pdf  # 直接鉴权获取固定来源 PDF，不依赖先枚举 source
qatlas paper read qa_… --limit 30000 --json     # 代理友好的派生正文、来源/revision 与续读游标
qatlas paper source-list qa_… --json       # 原始 PDF source、版本来源和完整 hash
qatlas paper parse-list qa_… --json        # 支持 originals API 的服务返回解析 revision
qatlas paper parse-json qa_… REVISION --no-cache  # 固定 revision 的 JSON，默认 stdout
```

- 服务端搜索结果是 `results[].paper_id` 加 `results[].hit`，`paper_id` 是
  统一 `qa_…` 身份；direct 搜索绕过 qatlasd，不铸造或锚定统一 ID。
- match 只查已有 registry，未命中退出 `1` 不表示论文不存在；多候选/ambiguous
  时先用作者、年份和原文身份消歧，不能直接选择第一个候选。
- `qa_…` 表示论文工作，不固定 arXiv 版本、PDF source 或解析 revision。
  复现证明要另记录版本，或解析列表中的 source/revision/hash；需要核对原件时
  用 `paper pdf qa_… --version v2 --no-cache` 或明确 `--source`。省略 pin 不是
  可复现版本选择；显式 source/version 无命中时不会回退。
- Markdown 便于阅读，固定 revision 的 JSON 便于保留页码、块和公式来源。
  解析正文与 PDF 可能有 OCR 差异；关键前提、公式和编号仍需对照原件。
- `paper get` 使用既有 `papers:read`；缓存未命中可触发服务端下载/转换，并非
  手工上传。先跟踪已有 LRO/job，不因一次调用超时重复创建任务。

若已在其他来源取得服务缺失的合法 PDF，可用已有贡献命令补缓存：

```bash
qatlas contrib pdf 1010.4458v2 --pdf paper.pdf --verify strict
# 或明确 DOI；本地现成 MinerU ZIP 的上传入口限 DOI：
qatlas contrib mineru 10.1145/2090236.2090261 --zip mineru.zip --verify strict
```

这两条是写操作，需当前账户具备贡献权限，并由客户端实施写操作版本检查。
只读获取不需要额外申请贡献权限；上传前核对身份和文件，已有资产不随意
`--overwrite`。`contrib mineru ID` 的本地转换还需要用户已有的 MinerU 配置；
通常的远程 Markdown 获取不需要本地 MinerU token。旧服务若缺 originals API，
对应命令返回 unsupported（退出 `9`），仍可用既有 Markdown 获取路径。

### 固定来源 PDF 与代理续读 JSON

```bash
qatlas paper pdf qa_… --source-id src_… -o paper.pdf
qatlas paper pdf 0811.3171v2 --version v2 --no-cache -o paper-v2.pdf
qatlas paper read qa_… --source-id src_… --revision REVISION --page 5 --block 12 --json
qatlas paper read qa_… --limit 30000 -o first-window.json
# 将响应 next_request.cursor 的字符串原样传回；不要改成“最新” revision：
qatlas paper read qa_… --cursor 'CURSOR_FROM_NEXT_REQUEST' --json
qatlas paper read qa_… --no-wait --json  # 返回异步状态和 operation.status_url，不主动轮询
```

- `paper pdf` 直接请求鉴权的 `GET /api/papers/{id}/pdf`，支持 `qa_`、arXiv、DOI
  等服务端别名；`--source` 与 `--source-id` 同义，和 `--version` **互斥**。
  显式 pin 无命中或存在歧义时由服务端拒绝，客户端不回退到其他 source/version。
  不要求已有 source-list 记录，因此不会因旧 PDF 尚未登记 source 而阻断访问。
- 从响应 `X-QAtlas-Source-Id`、`X-QAtlas-Source-Origin`、`X-QAtlas-Paper-Id` 和
  `X-QAtlas-PDF-SHA256`（或 `X-QAtlas-Sha256`）取得来源与完整性信息。
  所有身份均逐块计算 SHA-256，在校验完成前不输出任何 PDF 字节；大文件用临时
  磁盘缓冲而非整文件常驻内存。错误/摘要不匹配时保留已有输出文件和已验证缓存。
  别名可按响应的规范 `qa_` 身份复用内容寻址缓存；**缓存命中也先取得服务端当前
  鉴权及 paper_access 放行响应**，不会用缓存绕过禁用、401 或 403。
- PDF/read 均要求服务端 `paper_access.enabled: true` 与当前身份的读取权限。
  禁用/资源缺失按服务端 404 详情报告，不把 PDF 当成永远 410 的废弃接口。
  不切换账户、不改已有 token/config，也不自行向外部原始地址附带鉴权下载。
- `paper read` 请求 `GET /api/papers/{id}/read`，stdout 默认就是完整 JSON，
  `--json` 可显式标注；`-o FILE` 将同一 JSON 写入文件。保留 `paper_id`、
  `source_id`、`revision`、`renderer`、`format`、`content`、`request_scope`、
  `content_ranges`、`truncated`、`next_request`、`warnings` 及未来扩展字段。
  页码/块序号为 **1-based**，`--block` 必须带 `--page`；这与旧 block-* 的
  0-based `page_idx` / 原始 block index **不是同一套定位参数**。
  `--limit` 是正文 Unicode 字符预算，1..100000（服务端默认 30000）。
- 若 `truncated: true`，将 `next_request.cursor` 原样交给下一次 `--cursor`。
  游标固定 paper/source/revision、Middle hash、renderer 与选择范围；客户端不
  自行切段、重写定位或拼装“原始 JSON”。这份 JSON 是 **Middle 派生阅读视图**，
  不是 MinerU content-list，也不是可复核的原始 Middle artifact；保留原件仍用
  `paper parse-json ID REVISION`，该命令继续输出 hash 校验后的原始字节。
- 未解析时响应 202，CLI 遵守 `Operation-Location` 和 `Retry-After`，轮询状态
  queued/running，直到 ready/cached/done 后携带固定 source/revision 再取原请求。
  failed/cooldown/unavailable、HTTP 错误或 `--max-wait` 超时只报错，不假装正文
  已就绪。`--no-wait` 返回初始状态 JSON，并补入 `operation.status_url` 与
  `retry_after`，便于外部代理自行接续；轮询 URL 必须仍在配置的服务端同源。
  进度/来源说明只走 stderr；read 的进度/默认推断可用 `--quiet-progress` /
  `--quiet-notes` 控制，PDF 的校验/缓存说明始终走 stderr。

现有 `paper get markdown/images/figures/image` 同样支持新服务的 202 懒解析：
使用同源状态位置、遵守 Retry-After、有界等待后携固定 source/revision 重取原资产；
保留旧 cached/md_ready 状态兼容。各命令可用 `--no-wait` 返回异步 JSON，
`--max-wait` / `--quiet-progress` 控制等待；figures 仍输出 JSON、图片/Markdown
仍输出原字节。source origin 中 arXiv `vN` 是语义版本（arxiv:vN 或 arxiv:IDvN），
**不是** S3VersionId；相同 PDF SHA 可共享 source ID，解析 revision 仍独立固定。

### 贡献者 hosted MinerU V1：确切 PDF → 完整 ZIP

`qatlas contrib mineru ID` 与队列/`--watch` 默认走 V1 上传工作流，不把受保护的
claim URL 直接交给第三方。开启 gate 的 claim 含 source_id/pdf_requires_auth 与
SHA，客户端只向配置的同源 QAtlas 携自己的鉴权下载 PDF，逐字节验证后才按
uploads → PUT → complete/file.id → parse/jobs → poll → files/content 发送该确切字节。
关闭 gate 时仅接受免鉴权的 arXiv 白名单来源，同样必须有完整 SHA，不跳过校验。

```yaml
mineru_api_base_url: https://mineru.net
mineru_api_protocol: v1
mineru_tier: standard
# 使用用户已有 mineru_api_tokens；此处不写实际凭据。
```

```bash
qatlas contrib mineru 0811.3171v2 --tier standard --max-wait 600
qatlas contrib mineru --batch-size 10 --tier standard
qatlas contrib mineru 0811.3171v2 --no-push  # 保留完整原ZIP，路径只在stderr报告
```

- 生产 hosted 根入口自动规范到 `/api`，custom API root 保持明确配置。V1 tier
  flash/basic/standard/advanced 与 legacy model_version **不同**；不会把 vlm 等
  模型名当成质量档位。既有 OCR bool 对应 V1 auto/ocr；language/formula/table
  仅保留为 legacy 配置，不虚构其 V1 参数，命令明确提示。
- 用户 QAtlas bearer 从不进入 provider；vendor bearer 只发其配置的 API origin；
  PUT presign / 跨源结果下载无 bearer、cookie、`.netrc` 隐式凭据。API credential
  请求不跟随跨源重定向，认证 PDF 重定向必须同源。
- 单篇与队列均固定 source+SHA，一篇一个 V1 file-id job；等待包含 source/upload/
  job/result 全流程，受 `--max-wait`、配置 timeout 与 lease 上界限制。confirmed
  daily quota 才轮换 key，不重试结果 UNKNOWN 的提交；watch 遇 UNKNOWN 停止。
  退出/超时释放 claim，未完成 provider job 可能仍运行，不自动当作取消或重提。
- 获取和发送的是**整个原 ZIP**，里面所有 JSON、Markdown、图片、未知文件字节
  不改写/不抽成 MD-only。CRC/大小守卫不改变原件；server 严格验证完整 Middle+MD
  和 manifest 后发布**新 revision**。上传携 exact source_id/pdf_sha256/tier 与
  fullZIP expected_sha256；overwrite 不修改不可变历史。
- `--no-push` 保留完整 ZIP；服务端拒收、写前拒绝或结果不确定也保留 ZIP 供核对。
  `--no-cache` 属于旧 V4，V1 没有对应字段，提示并不发送捏造参数。只有显式
  `mineru_api_protocol: legacy-v4` / `--legacy-v4` 选择旧 URL 协议，受保护 URL
  或旧不完整产物可能不兼容；独立 `qatlas parser` 的 legacy SDK 路径没有被冒充 V1。

### 登记外部论文来源（ePrint / 独立 PDF）

无 DOI/arXiv 身份的合法外部 PDF 使用独立登记命令，不伪造 DOI，也不把
手工元数据混入 downloader enqueue：

```bash
qatlas paper source-register https://eprint.iacr.org/2025/1234 \
  --title "Paper title" --author "Alice Example" --author "Bob Example" \
  --year 2025 --json
# 普通 HTTPS URL 必须直接返回 PDF；ePrint 支持其论文页或 PDF URL。
```

- 当前账户需 `papers:write`。标题、至少一个作者、年份必填；`--author NAME`
  可重复。客户端在任何 HTTP 请求之前检查非空、标题最多 2000 UTF-8 字节、
  作者 1..100 位且每位最多 500 UTF-8 字节、年份 1..9999。
- 请求是 `POST /api/papers/source-register`，仅发送
  `{source_url,title,authors:[string],year:int}`；复用写前版本检查及确定性
  `Idempotency-Key`，不自动重试。URL 安全性、PDF 获取与标题/来源验证由服务端
  实施；客户端不把外部 URL 转成 DOI，不自行按标题合并论文。
- `--json` 完整保留服务器响应的 `paper_id`、`created`、`source` 对象、
  `source_url`、可选 `external_id` 及未来扩展字段，不另造身份字段。
  记录返回的 `source.source_id` 与 `source.sha256` 后，可用
  `qatlas paper pdf qa_… --source src_… --no-cache -o paper.pdf` 获取固定原件。
- 同来源身份与同 PDF hash 的幂等性由服务端保证；来源 URL 内容变化可以产生
  新 source，不能把同一请求正文的幂等键等同于无限期重放旧 PDF 的保证。
  写请求丢响应会提示 **UNKNOWN**，先核对服务端 `source-list` / 元数据再决定
  是否重新提交。这不是下载/转换队列接口；需要正文时另用既有获取命令。
- 未实现此登记端点的旧服务退出 `9`（unsupported）；不回退到另一个写接口。

### 块级评论（paper 原件 / 块 / 讨论）

`qatlas paper pdf|source-list|parse-list|parse-json|block-list|block-get|block-image`
与 `qatlas comments list|show|create|reply|status|edit` 覆盖块级评论闭环的
客户端侧：读取不可变原件与解析修订、按 page_idx+block_index 定位块、读取与
发起/回复/改状态/编辑讨论。需要服务端 qatlasd 具备块级评论端点（Q1/Q2）；
旧服务未实现时 CLI 以退出码 9 明确提示 unsupported。

```bash
qatlas paper parse-list qa_…                        # 解析修订列表（锚点用 revision id，不用 latest）
qatlas paper pdf qa_… --version v2 -o paper.pdf     # 源 PDF 原字节（sha256 校验、可缓存、不回退版本）
qatlas paper block-get qa_… <revision> 4 11 --json  # 单块组合阅读：source+anchor+content+discussions
qatlas comments create qa_… <revision> 4 11 "依据…" --type transcription_error --status pending
qatlas comments reply <discussion_id> "对照原图核对结果"
qatlas comments status <discussion_id> confirmed --reason "已对照原图确认"
```

要点：

- 评论写操作自动携带确定性幂等键（SHA-256(method|path|body)），发送后丢失
  响应会明确提示结果可能为 **UNKNOWN**，不会自动重试；重发相同正文、方法
  和路径会回放原结果，不要通过改正文/换键来重复发帖。写前版本探测失败则
  明确提示**请求未发送**，不会误报 UNKNOWN。`edit` 自动取当前 revision 做
  `If-Match` CAS，过期修订按 409（退出码 6）拒绝。
- 正文上限 20,000 Unicode 字符（客户端镜像检查，超限退出码 2 并提示拆分）；
  分页 per_page 默认 20、上限 100，游标翻页。
- 原件缓存按「服务来源 + qa_ + 固定 sha256」内容寻址，配置项 `cache_dir`
  （默认 `~/.cache/qatlas`）；下载校验 hash 后原子发布（tmp+rename），并发
  去重；401 不会用旧缓存伪装成功，token 撤销不删除已合法下载的原件。
  评论等可变数据不落盘缓存。
- stdout 只出数据（原字节 / 完整机器 JSON / 人读格式），提示与进度走
  stderr；结构化退出码：0 成功、1 传输/5xx/坏内容、2 用法、3 未找到、
  4 未认证、5 无权限、6 冲突/CAS、7 超限、8 限流、9 服务不支持。

`search` 命令由独立插件 qatlas-search 提供（仓库 IAI-USTC-Quantum/qatlas-search），
`rag` 命令由独立插件 qatlas-rag 提供（仓库 IAI-USTC-Quantum/qatlas-rag），
`match` 命令由独立插件 qatlas-match 提供（仓库 IAI-USTC-Quantum/qatlas-match，
论文身份高精度匹配：判定 DOI/arXiv/OpenAlex/URL/标题是否已入库并返回统一 qa_ id）；
未安装时 `qatlas search` / `qatlas rag` / `qatlas match` 会给出对应的安装提示。

### 插件机制

第三方包可以通过 entry point 组 **`qatlas.plugins`** 注册插件，向 CLI 贡献
顶层命令或 `contrib` 子命令。插件协议（基类 `QatlasPlugin` 与
`CommandSpec`）定义在 `qatlas/client/plugins/base.py`：

```toml
# 插件包自己的 pyproject.toml
[project.entry-points."qatlas.plugins"]
myplugin = "my_package.qatlas_plugin:plugin"
```

**协议 v2**：`CommandSpec.handler` 支持 `(ctx, argv)` 双参数签名——`ctx`
是 `CliContext`（已解析的 server URL / token / 超时 / TLS 选项，来源与
内置命令相同的 config.yaml + hosts.yml）。配套的公共 HTTP 层在
`qatlas.client.pluginsupport`（`server_request` / `format_api_error` /
`poll_lro`），插件服务器调用自动获得 PAT 注入、版本协商头与统一错误
格式，无需自行实现。v1 的 `(argv)` 单参数签名继续可用（CLI 按签名探测
分发）；插件声明 `cli_api_version` 高于 CLI 提供的 `PLUGIN_API_VERSION`
时其命令被跳过并给出一行警告。完整协议说明见任一 qatlasd 实例文档站的
`/doc/guide/cli/`「插件协议」小节。

内置命令优先于插件命令；插件不可用时会被静默跳过，不影响 CLI 本体。

## 版本与兼容性

qatlas-cli 与服务端 qatlasd **各自独立演进版本号**，兼容协议是：

> **两者的 `(major, minor)` 相同即兼容**，patch 位随意漂移。
> 兼容性修复只 bump patch，例如 qatlasd `0.22.4` ↔ qatlas-cli `0.22.3` 是
> 受支持的配对；而 `0.23.x` 服务端配 `0.22.x` 客户端则不兼容。

运行行为：CLI 每个请求带 `X-Qatlas-Client-Version` 头，服务端响应带
`X-Qatlas-Server-Version` 头。明确写操作先探测 `GET /api/server/info`，
再发业务请求：

- `(major, minor)` 一致：静默通过（patch 差异不算事）；
- 服务端更新且为写：探测阶段硬失败（exit code 4），提示 `uv tool upgrade qatlas-cli`，**不发送写请求**。探测失败（404 除外）同样不发送。请求已发出后版本变化只警告，不表示写入被拒绝或未发出；
- 服务端更新且为读：stderr 警告一次，继续执行；
- 客户端更新：stderr 警告一次（提示运维方升级 qatlasd），继续执行；
- 响应无版本头或版本无法解析：跳过协商；info 接口 404 视为旧接口，但仍检查
  版本头，已知更新的服务端不能借 404 绕过写前拒绝。探测不跟随重定向。

所有实际写请求（fetch、插件显式 `write=True`、lease、贡献上传、MinerU
提交/上传/清理、设备认证）发送阶段发生传输异常时都会在 stderr 明确提示
结果为 **UNKNOWN**；异常类型和既有退出码保留，不自动重试。`paper fetch`
和通用插件写没有客户端幂等回放保证，不能把网络失败等同未写入，也不能把
重复 enqueue 说成安全重试；先核对 jobs / 元数据等服务端状态。版本 preflight
失败仍只说**未发送**，不会提示 UNKNOWN。设备认证和内部 lease 清理仍沿用
既有无版本 preflight 流程；清理继续 best-effort，不遮蔽原异常。设备 token
只对服务端明确返回的 pending / slow_down 继续轮询，丢失 token 响应时不盲目重试登录。
MinerU `--watch` 捕获到 UNKNOWN 后停止当前监视并退出 `1`，不会在下一轮
重新提交同一队列；读状态轮询和明确的服务端 quota/backoff 响应不受影响。

服务端推断的 ID / 版本默认值通过 `X-QAtlas-Defaults-Applied` 在 stderr 显示，
`--quiet-notes` 可关闭。新服务端使用 ASCII 头；CLI 也兼容旧服务端的 UTF-8
箭头字节，仅修复已知箭头乱码，不猜测或重新解码其他合法 Latin-1 内容，
不修改响应正文或 JSON stdout。

完整策略见主仓文档：
[QuantumAtlas 版本与兼容策略](https://github.com/IAI-USTC-Quantum/QuantumAtlas/blob/main/docsite/dev/versioning.rst)。

## 配置

用户级配置位于 `~/.config/qatlas/config.yaml`（首次运行非 `config` 命令时
自动创建），顶层 `server_url` / `token` 等字段供各客户端命令读取。环境变量
`QATLAS_*` 系列可覆盖对应配置；`QATLAS_SKIP_DOTENV=1` 跳过仓库 `.env` 加载。
`cache_dir` 指定块级评论原件（paper pdf / parse-json）的内容寻址缓存根目录，
相对路径锚定在项目根（同 `raw_dir`）；未设置时用系统用户缓存目录
（Linux 默认 `~/.cache/qatlas`）。

## 开发与测试

默认分支是 **master**，支持 Python 3.11+，CI 使用 Python 3.11 和 uv 0.11.30。
依赖由 `pyproject.toml` / `uv.lock` 管理；只有有意更新依赖时才运行 `uv lock`。

```bash
uv sync --locked --extra dev
uv run --no-sync ruff check .
uv run --no-sync python -m pytest
```

测试覆盖 CLI、auth、paper/contrib/config、parser 的 fixture/mock，以及版本来源和
发布门禁。普通测试禁止意外外网 DNS/连接（允许本机 loopback fixture），默认排除
`network` 和 `e2e`。网络测试须显式 `uv run --no-sync python -m pytest -m network`；
生产集成测试另行授权，不将个人 PAT、真实配置或生产目录带入默认测试。
安装工具/依赖可以联网，**离线测试不等于离线安装**。Ruff 的错误检查基线在本仓显式
固定为 E4/E7/E9/F，不附带全仓格式化或类型写法迁移。

`[project].version` 是唯一版本来源，运行时只读 `importlib.metadata.version("qatlas-cli")`。
源码 checkout 也必须先 `uv sync` 安装；没有 metadata 时明确报错，不猜测源码版本。
本仓没有也不新增 `VERSION`。测试比较项目、锁文件、metadata 和 `__version__`。

需要本地检查 wheel/sdist 时，保持 Hatchling 后端并安装锁内构建工具：

```bash
uv sync --locked --extra dev --group build
uv build --no-build-isolation --python .venv/bin/python
```

不临时 `pip install build/twine`；显式关闭构建隔离，让后端依赖使用 `uv.lock` 中的
build group，而不是误以为普通 `uv build` 自动锁住隔离环境。构建产物在 `dist/`，不提交。

## 版本管理与发布

Commitizen 的工具依赖已锁定，使用 uv provider，保留 `major_version_zero = true`。
在干净、已同步的 master 上可先预览：

```bash
uv run --extra dev cz bump --dry-run
```

**只有获得发版授权后**才执行 `uv run --extra dev cz bump`，审核版本、锁文件和
CHANGELOG 的变更，再单独授权推送默认分支和 annotated tag。bump 前置 hooks 会跑
Ruff 和默认 pytest；hook 失败可能留下修改过的文件却没有 commit/tag，先检查差异，
不要盲目重跑。不要执行会重生成手写历史的裸 `cz changelog`。新版本说明保持
`## vX.Y.Z (YYYY-MM-DD)` 格式，破坏性变更写清迁移要求。

`.github/workflows/release.yml` 的流程为：

1. master push/PR、tag 和手动运行均执行锁内 lint/test；管理员需将 `test` 设为分支保护必需检查。
2. 仅 tag 在测试通过后发布；手动选择分支只测试，不发布。
3. 校验 canonical PEP 440、tag/项目/锁文件及可选 VERSION 一致、精确且唯一的非空 CHANGELOG 段。
4. 查询 PyPI，已经记录该版本则在构建前停止；异常响应、网络或鉴权错误不当作“版本不存在”。
5. 一次构建 wheel/sdist，先保存 Actions artifact **release-dist**（保留 90 天），再上传 PyPI。
6. 独立的 GitHub Release job 下载**同一批**产物，添加发布说明和附件，不再次构建。

PyPI 保持 OIDC Trusted Publishing，不使用静态 PyPI token。PyPI publisher 应匹配
owner `IAI-USTC-Quantum`、repo `qatlas-cli`、workflow `release.yml`、environment `pypi`；
这些远端设置需管理员核验。GitHub Release job 单独获得 `contents: write`，不需要 PyPI 身份。

### 部分失败的恢复

- PyPI 本身禁止覆盖已有文件；关闭 `skip-existing` 是选择让重复上传显式失败，**不是**靠它防覆盖。
- 只要任一发行文件上传成功，就不要重建同版本或重跑整个发布 job。取回原 run 的
  **Actions → Artifacts → release-dist**，先核对 PyPI/GitHub 已存在文件及 SHA256，只补缺失上传。
- 若仅 GitHub Release job 失败，可只重跑该失败 job：它只下载原包，不重新构建/发布 PyPI；
  `overwrite_files: false` 不覆盖已有附件。已存在附件仍应核对其 SHA256。
- artifact 过期/丢失无法取回原包时停止恢复并发新版本，不猜测、移动 tag 或覆盖已有产物。
  本模板不自动完成 PyPI 的部分文件恢复；不要用“Re-run all jobs”代替恢复核对。
- 两个平台不是原子事务，GitHub Release 失败不会撤回 PyPI 包。旧 tag 指向旧 workflow，
  新门禁不追溯修改旧 run；不要通过重跑旧版本来验证新流程。发版测试也必须使用
  经确认、尚未发行的新版本，不移动或覆盖旧 tag。发布成功不意味着获准部署生产。

### 预发布验证

预发布同样是真实发行：PyPI 会保存包，GitHub 会创建标记为 prerelease 的 Release。
在默认分支 CI 通过、版本号获得确认后，可用 Commitizen 的 `--prerelease rc`
和 `--prerelease-offset 1` 创建 RC；先加 `--dry-run` 检查结果，再执行实际 bump。
例如 patch 级 RC 使用 `cz bump --increment PATCH --prerelease rc --prerelease-offset 1`。
推送时只推本次明确的分支和 tag，不用旧版本测试，也不把 RC 当作正式稳定版。

验证发行后，从 PyPI 下载该精确版本并在隔离环境安装，核对包 metadata、
`qatlas --version`，再比较 PyPI 文件与 GitHub Release 同名附件的 SHA256。
需要安装 RC 时显式指定 `qatlas-cli==<已发布的完整RC版本>`；不要依赖不带版本的
`uv tool upgrade qatlas-cli` 自动选择预发布。
