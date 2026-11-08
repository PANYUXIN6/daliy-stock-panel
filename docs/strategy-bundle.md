# 个人声明式策略包

策略池的“策略包”入口支持导出、上传预览和确认导入；策略设置也可导出当前策略及个人调参。预览列出新建、相同内容复用、同ID冲突和缺少的依赖，确认导入前不写入资源。不同内容的同ID对象不能覆盖；导入时重新验证当前状态，导入失败撤销本次新增文件并恢复策略视图。

本地包沿用 `tick-strategy-bundle`、版本1和上游策略包HTTP接口形状，另有 `contract=personal-declarations-v1`。包内仅允许 `strategies/composite/*.json`、`user_data/strategy_overrides/*.json`、`user_data/custom_signals/*.json` 和 `user_data/custom_factors/*.json`，以及 `manifest.json`。manifest精确列出文件，`references`记录本机必须已安装的内置或维护者策略ID。包不会包含供应商配置、账号、会话、行情、整站备份或其他用户的数据。

用户策略仅支持当前引擎的叠加声明。包内JSON导入后使用现有字面量声明存储，加载时通过AST读取，不导入或执行上传的Python代码。上游包含 `.py` 的源码策略包会明确拒绝，维护者策略应由服务器部署。叠加策略和因子依赖递归收集并检测循环；仍遵守本地引擎不允许嵌套叠加策略的约束。公式因子复用DSL校验和现有试算入口。自定义信号的导入、导出仍限管理员，其他资源沿用当前用户的既有操作权限。

文件限额为压缩8MB、展开总量16MB、单项2MB、最多128个ZIP条目。读取只在内存中进行，不直接解压路径；拒绝越界、重复路径、符号链接、特殊文件和加密条目。目标路径由当前会话用户解析，不接收owner参数；已有文件使用语义比对，仅相同内容可复用。

接口为 `GET /api/strategies/{id}/bundle`、`POST /api/strategies/bundle/inspect` 和 `POST /api/strategies/bundle/apply`。上传使用multipart字段`file`；`allow_overwrite=true`返回错误。本地包不支持上游整站恢复或任意源码导入语义。导入成功后热重载当前用户策略、清除其策略/监控缓存，并失效前端策略、信号和因子查询。
