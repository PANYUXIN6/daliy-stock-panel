# TickFlow Pro 接口探测

脚本为 `backend/scripts/probe_tickflow_pro.py`，用于检查 `tf.klines.batch` 与 `tf.quotes.get` 的少量样本请求。

## 生成请求计划

```bash
cd backend
python3 scripts/probe_tickflow_pro.py
```

结果写入 `reports/tickflow_pro_probe/<timestamp>/`。`DRY_RUN_OK` 只表示计划与本地门控通过，不证明 SDK、网络或认证可用。

## 实际请求

在环境中配置 `TICKFLOW_API_KEY` 后运行：

```bash
cd backend
python3 scripts/probe_tickflow_pro.py --live
```

脚本默认只允许北京时间 16:00 后发出真实请求；确需提前时使用 `--force`。样本标的为 `000403.SZ`、`600489.SH`、`300059.SZ`，不执行全市场回填。不要与其他探测或大批量同步同时运行，进程间不共享账号级限速，见 [请求预算](tickflow-pro-shared-rate-limit.md)。
