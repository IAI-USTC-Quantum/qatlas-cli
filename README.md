# qatlas-cli — QuantumAtlas 的命令行客户端

> QuantumAtlas 的 `qatlas` 命令行客户端：面向用户侧的论文获取、贡献上传、
> 本地解析等工作流，通过 HTTP 与服务端 qatlasd 交互。本仓库是主仓
> QuantumAtlas 拆出的独立私有仓库，只保留客户端与本地工具，不含服务端代码。

所有服务端通信都是 `requests` 直发的 HTTP 调用，无重量级 SDK 依赖。

## 安装

> **安装方式变更**：本包拆分自主仓的 `quantum-atlas` Python 包（0.21.0a3 起
> CLI 不再随主包发布）。PyPI 包名由 `quantum-atlas` 改为 **`qatlas-cli`**，
> 版本号从 0.21.0a3 延续（首个独立发布为 0.22.0）。之前用
> `uv tool install quantum-atlas` 安装的用户，改用下面的命令即可，
> 已有配置（`~/.config/qatlas/config.yaml`、PAT 等）不受影响。

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
uv sync --extra dev          # 项目内 .venv，editable 安装本包
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
`rag` 命令由独立插件 qatlas-rag 提供（仓库 IAI-USTC-Quantum/qatlas-rag）；
未安装时 `qatlas search` 或 `qatlas rag` 会给出对应的安装提示。

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
- 响应无版本头（0.8.0 之前的老服务端）：跳过协商。

完整策略见主仓文档：
[QuantumAtlas 版本与兼容策略](https://github.com/IAI-USTC-Quantum/QuantumAtlas/blob/main/docsite/dev/versioning.rst)。

## 配置

用户级配置位于 `~/.config/qatlas/config.yaml`（首次运行非 `config` 命令时
自动创建），顶层 `server_url` / `token` 等字段供各客户端命令读取。环境变量
`QATLAS_*` 系列可覆盖对应配置；`QATLAS_SKIP_DOTENV=1` 跳过仓库 `.env` 加载。

## 开发与测试

```bash
uv sync --extra dev
uv run pytest -q        # 默认全离线；真打外网的用例标了 network，需 -m network 显式开启
```

测试覆盖：CLI 命令分发与版本解析、auth（设备码登录 / token 存储，mock HTTP）、
paper / contrib / config 命令行为（mock 服务端）、parser 的 arXiv 抓取与
MinerU 客户端（固定 fixture / mock）。带 `network` 标记的 live 用例默认跳过。
