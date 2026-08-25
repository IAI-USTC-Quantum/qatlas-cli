# Changelog

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
