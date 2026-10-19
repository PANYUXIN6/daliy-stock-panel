# 后端自定义入口

本目录的直接子模块和包会被自动发现，下划线开头的名字除外。提供 `EXTENSION_ID`、`EXTENSION_API_VERSION` 与 `setup(registrar)`，可注册路由和通知格式化器；可选 `startup(context)` 在核心数据层就绪后调用。

契约见 [`../extensions/`](../extensions/)，示例见 [`_template.py.example`](_template.py.example)；模板不参与运行。接入说明见 [自定义代码入口](../../../docs/secondary-development.md)。
