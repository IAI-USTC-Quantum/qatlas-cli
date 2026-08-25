# qatlas-cli — QuantumAtlas 的命令行客户端

> QuantumAtlas 的 `qatlas` 命令行客户端：面向用户侧的论文获取、贡献上传、
> 本地解析等工作流，通过 HTTP 与服务端 qatlasd 交互。本仓库是主仓
> QuantumAtlas 拆出的独立私有仓库，只保留客户端与本地工具，不含服务端代码。

所有服务端通信都是 `requests` 直发的 HTTP 调用，无重量级 SDK 依赖。

## 安装

作为全局 CLI 工具安装（推荐）：

```bash
uv tool install --from git+ssh://git@github.com/Agony5757/qatlas-cli.git qatlas-cli
qatlas --help
```

本地 editable 安装（开发用）：

```bash
git clone git@github.com:Agony5757/qatlas-cli.git
cd qatlas-cli
uv sync --extra dev          # 项目内 .venv，editable 安装本包
# 或装入当前环境：
uv pip install -e .
```

## 命令概览

```
qatlas config   # 管理用户级配置文件（~/.config/qatlas/config.yaml）
qatlas auth     # 管理各 host 的 PAT / session token（login / status / token / logout）
qatlas paper    # 从服务端拉取论文 PDF / markdown（静默 fetch + LRO 轮询）
qatlas contrib  # 贡献者工作流：上传 PDF（contrib pdf）或本地跑 MinerU 再推送（contrib mineru）
qatlas parser   # 抓取并解析 arXiv 论文（本地工作区命令）
```

别名：`papers` → `paper`，`parse` → `parser`。

```bash
qatlas auth login                                   # 设备码 / PAT 登录
qatlas paper get markdown quant-ph/9508027 --output paper.md
qatlas contrib pdf quant-ph/9508027v1 --pdf paper.pdf
qatlas contrib mineru 2501.00010v1
```

`search` 命令由独立插件 qatlas-search 提供（仓库 Agony5757/qatlas-search），
未安装时 `qatlas search` 会给出安装提示。

### 插件机制

第三方包可以通过 entry point 组 **`qatlas.plugins`** 注册插件，向 CLI 贡献
顶层命令或 `contrib` 子命令。插件协议（基类 `QatlasPlugin` 与
`CommandSpec`）定义在 `qatlas/client/plugins/base.py`：

```toml
# 插件包自己的 pyproject.toml
[project.entry-points."qatlas.plugins"]
myplugin = "my_package.qatlas_plugin:plugin"
```

内置命令优先于插件命令；插件不可用时会被静默跳过，不影响 CLI 本体。

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
