# AI 开发入口

修改、调试或审查本仓库前，必须完整阅读并遵循根目录的 [`CONTRIBUTING.md`](CONTRIBUTING.md)。其中保留模块边界、金融数据契约、缓存与性能要求及验证入口。

定位代码与后续工作可参考 [`docs/REPO_MAP.md`](docs/REPO_MAP.md)、[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 和 [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)。它们记录当前代码结构、调用链和开发入口，具体数据与运行约定以维护规则和领域文档为准。

涉及前端插槽、后端定制模块或扩展注册时，同时阅读 [`docs/secondary-development.md`](docs/secondary-development.md)，并核对实际类型、注册入口和调用方。

同时遵守以下规则：

- 沿调用链和现有测试确认责任，保持改动聚焦，保留工作区已有修改；如实报告验证结果。
- 交易日盘后自动同步的执行顺序、当日完整性验收、重试与取消规则以 [`docs/postmarket-pipeline.md`](docs/postmarket-pipeline.md) 为约定。新增盘后步骤必须遵守依赖顺序，并同步该文档及对应测试。

## 环境与发布约定

- `develop` 对应本地开发与测试，`main` 对应服务器预发环境；日常修改在 `develop` 验证，通过后再合入 `main`。GitHub CI 在 `main` 的后端测试与前端构建均成功后自动发布，PR 与 `develop` 不部署。
- 本地与远端各自维护运行数据和配置。发布代码不得覆盖远端账号、会话、自选、个人策略、通知、研究产物及任何环境配置，包括 `.env`、`tiers.yaml` 和部署覆盖配置。
- 公共行情、财报、交易日历等数据仅在功能发布确有需要时按选定数据集单向同步到远端；常规采集由服务器独立执行，不在每次发版时自动复制本地数据。
- 数据同步采用明确白名单，不能复制整个 `data/`，也不能把整站备份恢复用作跨环境同步。合并、版本口径、写入互斥和缓存刷新要求见 [部署指南](docs/deployment.md#本项目的环境与发布约定)。
