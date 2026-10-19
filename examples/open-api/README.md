# 历史开放接口示例

本目录保留旧版 Bearer Token 接口的标准库脚本。当前应用已停用外部 Token、Token 管理和开放事件票据入口，这些脚本不能直接用于当前版本。

| 脚本 | 历史用途 |
| --- | --- |
| `01_pull_market.py` | 标的搜索、日 K、市场总览 |
| `02_write_ext_data.py` | 扩展表行数据写入与读回 |
| `03_run_backtest.py` | 策略清单、回测与结果读取 |
| `04_events_stream.py` | SSE 短期票据与事件流 |

当前认证见[账号与部署说明](../../docs/deploy-password.md)，源码扩展入口见[扩展开发](../../docs/secondary-development.md)。
