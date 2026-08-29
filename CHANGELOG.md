# Changelog

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
