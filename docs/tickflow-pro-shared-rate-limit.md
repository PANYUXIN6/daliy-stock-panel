# TickFlow 请求预算与进程边界

`backend/app/tickflow/rate_limits.py` 集中处理 TickFlow 批长与 RPM，默认以 `SAFETY_RPM_FACTOR=0.8` 缩减能力注册结果中的 RPM。实际配额以当前能力检测结果为准。

- 限速槽位只在同一 Python 进程内共享。同一账号被其他进程或服务使用时，预算不会自动合并，需协调运行时段与请求量。
- `resolve_limit(..., apply_safety=True)` 默认缩减 RPM；诊断可显式关闭缩减。
- `sleep_between_batches(index=0)` 只登记槽位而不等待；多个调用方同时发出首批请求仍可能突发超限。后续批次才按共享时间轴等待。
- 探测脚本和大批量同步应避免同时运行；429、降级或能力缺失应保留明确结果，不能静默混用数据源。

受控探测方式见 [Pro 探测说明](tickflow-pro-phase1-probe.md)。
