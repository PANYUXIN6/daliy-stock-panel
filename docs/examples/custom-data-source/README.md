# 本地数据源联调

本目录的 `mock_server.py` 提供本地 HTTP 样例，`mock_source.yaml` 是对应配置。

在仓库根目录运行：

```bash
python docs/examples/custom-data-source/mock_server.py
```

服务监听 `http://127.0.0.1:3021`，提供 `/daily`、`/adj_factor`、`/realtime`。

将 `mock_source.yaml` 复制到 `data/data_sources/`，在「设置 → 数据源」重新加载，选择 `mock_source` 后用「试拉测试」验证。运行数据目录使用 `DATA_DIR` 时，复制到该目录下的 `data_sources/`。

字段、单位与请求配置见 [自定义数据源](../../custom-data-source.md)。
