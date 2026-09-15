# qatlas-cli — QuantumAtlas 的命令行客户端

独立的 `qatlas` 客户端，通过 HTTP 与 qatlasd 交互，提供论文获取、贡献上传和本地解析等工作流；本仓不含服务端代码。

## 安装

推荐使用隔离的全局工具环境（Python 3.11+）：

```bash
uv tool install qatlas-cli
qatlas --help
```

旧 `quantum-atlas` 工具用户先执行 `uv tool uninstall quantum-atlas`，再安装新包；已有配置和 PAT 保留。
其他安装方式、命令与插件协议、兼容性、配置、开发测试和发布规则见 [完整项目概览](docs/overview.md)。

## 文档

[文档目录](docs/index.rst)使用原生 Sphinx + MyST + Furo；完整正文只维护在 `docs/`，不在 README 重复维护。
文档构建独立于 `pyproject.toml` / `uv.lock`，不安装应用、插件或 GPU 依赖。

从仓库根目录执行（Python 3.12、uv 0.11.30）：

```bash
uv run --locked --script docs/build.py
```

产物为 `build/docs/index.html`，只在本地生成，不自动发布。
独立的 `.github/workflows/docs.yml` 对默认分支 push/PR 的 README、docs 和该 workflow 变更执行同样的严格构建与校验，也支持手动运行；不发布网站、镜像或应用版本。

文档依赖写在 `docs/build.py` 的 PEP 723 头，锁为 `docs/build.py.lock`。
Sphinx 暂固定 8.2.3，以避开 9.1.0 已确认的中文搜索回归；不对搜索实现打补丁。
有意更新文档依赖时，改脚本头并用同一 uv 版本重新锁：

```bash
uv lock --script docs/build.py
```

## 开发与发版

开发、测试、兼容协议与完整发版步骤见 [项目概览](docs/overview.md)。版本由锁定的 Commitizen（`uv` provider）从 `pyproject.toml` 写入 PEP 440 号（如 `0.34.1rc1`）：先 `cz bump --dry-run`，授权后再 bump 并推送 annotated tag，CI 发 PyPI 与 GitHub Release。本仓不用 GoReleaser；服务端发版在 [QuantumAtlas](https://github.com/IAI-USTC-Quantum/QuantumAtlas)。
