# 小帅工作台

个人维护的行情、选股、回测与监控项目，仅维护 Linux 服务器版，通过浏览器访问。后端使用 Python / FastAPI / Polars，前端使用 React / TypeScript；行情与研究数据保存在服务器。

网页服务 `web` 负责查询与交互，唯一的 `collector` 负责采集、调度和任务执行。两者共享数据目录；页面读取后台已保存的数据。

## 本地开发

需要 Python 3.11+、Node.js 20+、`uv` 和 `pnpm`。首次运行时，从 [.env.example](.env.example) 复制生成 `.env` 并填写所需配置；已有配置继续保留。

```bash
./dev.sh
```

前端默认为 <http://localhost:3011>，网页后端为 <http://localhost:3018>，采集服务为 `127.0.0.1:3019`。Ctrl-C 会关闭本次启动的三个服务。

开发和测试使用 `develop`；`main` 的后端测试与前端构建通过后自动发布服务器预发。发布流程、双服务运行、健康检查与恢复见 [部署指南](docs/deployment.md)。

## 使用入口

- 登录与账号初始化：[账号与访问控制](docs/deploy-password.md)。共享行情、个人策略、自选及研究结果按各自归属保存。
- 数据准备：在数据页配置和执行同步，在设置页核对数据源能力路由。交易日盘后自动同步的顺序、完整性及重试规则见 [盘后管道](docs/postmarket-pipeline.md)。
- 日常操作：选股、回测、因子研究、挖掘、监控、模拟盘和 AI 助手见 [操作说明书](操作说明书.md)；模块说明见 [功能说明](docs/features.md)。
- 配置：数据源、AI、目录、端口和认证选项见 [配置详解](docs/configuration.md)。密钥由后端配置，配置变更后按说明重启服务。

## 开发与维护文档

| 文档 | 用途 |
| --- | --- |
| [维护约定](CONTRIBUTING.md) | 数据口径、模块边界、缓存与验证要求 |
| [开发导航](docs/DEVELOPMENT.md) | 启动、验证命令与 CI 范围 |
| [仓库地图](docs/REPO_MAP.md) | 目录职责、调用链和测试入口 |
| [架构说明](docs/ARCHITECTURE.md) | 数据流、状态归属与进程边界 |
| [策略指南](docs/strategy.md) | 声明式信号和受信任 Python 策略 |
| [策略迭代](docs/strategy-iteration.md) | 研究假设、回测证据与候选判定 |
| [挖掘说明](docs/mining.md) | 样本外验证、任务隔离与发布门槛 |
| [市场阶段](docs/market-phase.md) | 情绪周期和主线计算口径 |
| [自定义数据源](docs/custom-data-source.md) | YAML 配置与数据集契约 |
| [数据源插件](docs/plugin-development.md) | Provider 接口、单位和能力声明 |
| [定制入口](docs/secondary-development.md) | 现有前后端扩展的加载与注册 |

代码沿用原项目 [tick-stock-panel](https://github.com/shy3130/tick-stock-panel)，许可证见 [LICENSE](LICENSE)。
