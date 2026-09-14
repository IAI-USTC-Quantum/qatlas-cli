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
qatlas config   # 管理用户级配置文件（~/.config/qatlas/config.yaml）
qatlas auth     # 管理各 host 的 PAT / session token（login / status / token / logout）
qatlas paper    # 论文工作流：get markdown/images/metadata、status、mineru-lease，
                #   目录检索 list / lookup，批量下载 fetch 与进度 jobs
qatlas contrib  # 贡献者工作流：上传 PDF（contrib pdf）或本地跑 MinerU 再推送（contrib mineru）
qatlas parser   # 抓取并解析 arXiv 论文（本地工作区命令）
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
`X-Qatlas-Server-Version` 头，客户端据此比较：

- `(major, minor)` 一致：静默通过（patch 差异不算事）；
- 服务端更新且为写操作：硬失败（exit code 4），提示 `uv tool upgrade qatlas-cli`；
- 服务端更新且为读操作：stderr 警告一次，继续执行；
- 客户端更新：stderr 警告一次（提示运维方升级 qatlasd），继续执行；
- 响应无版本头或版本无法解析：跳过协商。

**现有限制**：比较发生在业务响应收到之后；即使 CLI 报 exit code 4，写请求也可能
已经执行。这不是业务调用前的兼容握手，也不能用来保证写操作未发生；
前置握手需另行定义跨仓 API 和迁移方案。

完整策略见主仓文档：
[QuantumAtlas 版本与兼容策略](https://github.com/IAI-USTC-Quantum/QuantumAtlas/blob/main/docsite/dev/versioning.rst)。

## 配置

用户级配置位于 `~/.config/qatlas/config.yaml`（首次运行非 `config` 命令时
自动创建），顶层 `server_url` / `token` 等字段供各客户端命令读取。环境变量
`QATLAS_*` 系列可覆盖对应配置；`QATLAS_SKIP_DOTENV=1` 跳过仓库 `.env` 加载。

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
