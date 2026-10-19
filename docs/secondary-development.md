# 自定义代码入口

本页记录当前仓库已经实现的接入位置。日常修改仍以 [维护约定](../CONTRIBUTING.md)、实际调用链和测试为准；核心行为可以直接在所属模块修改，无需先增加扩展框架。

## 现有入口

| 需求 | 入口 |
| --- | --- |
| HTTP 数据源字段映射 | `data/data_sources/*.yaml`，见 [自定义数据源](custom-data-source.md) |
| Python 数据源适配 | `backend/app/plugins/<name>/plugin.yaml` 与 provider，见 [数据源插件](plugin-development.md) |
| 扩展数据、声明式分析页 | [功能手册](features.md)的数据与扩展章节 |
| 自定义前端页面、导航或局部内容 | `frontend/src/custom/<namespace>/extension.tsx` |
| 自定义后端路由、启动处理或通知文案 | `backend/app/custom/` 下的模块或包 |
| 策略叠加条件 | 策略设置、回测基础过滤面板；实现见 `backend/app/strategy/overlay.py` |

候选过滤、评分、仓位、风控、回测成本和配置迁移没有通用注册接口。因子条件信号和回测因子归因已有实现；因子库的策略模板按钮仅提示维护者部署，不在网页生成 Python 策略。

## 前端注册

[`bootstrap.ts`](../frontend/src/extensions/bootstrap.ts) 自动发现 `frontend/src/custom/*/extension.tsx`。文件默认导出 [`FrontendExtension`](../frontend/src/extensions/types.ts)，包含 `id`、`apiVersion: 1` 以及可选的 `routes`、`navigation`、`slots`。完整示例保留在 [`extension.tsx.example`](../frontend/src/custom/_template/extension.tsx.example)，模板本身不参与构建。

- 路由只支持非根静态绝对路径，不能覆盖核心路径或其他扩展路径。
- 导航项通过 `routeId` 引用同一扩展的路由；页面可以不注册导航。
- ID、版本或路由校验失败会禁用对应扩展。注册完成后冻结；插槽和导航按 `order`、`id` 排序。
- 插槽组件有错误边界，单个组件失败不会使整个页面无法渲染。

当前插槽及传入属性以 [`FrontendSlotContextMap`](../frontend/src/extensions/types.ts) 为准：

| 插槽 | 位置 | 属性 |
| --- | --- | --- |
| `layout.navigation.extra` | 侧栏导航底部 | `collapsed`、`pathname` |
| `stock-preview.footer` | 个股详情图表下方 | `symbol`、`name`、`view: 'daily' \| 'intraday'` |
| `watchlist.toolbar` | 自选页工具栏末尾 | `symbols`、`viewMode`、`selectedGroup`、`refresh()` |

`symbols` 是当前筛选、排序后视图的标的；扩展修改自选数据后可调用 `refresh()`。插槽属性不包含 `apiVersion`，版本声明位于扩展清单。

请求与缓存继续使用 `frontend/src/lib/api.ts`、`queryKeys.ts` 和已有共享查询。新增页面沿用现有认证和账号缓存隔离。

## 后端注册

[`loader.py`](../backend/app/extensions/loader.py) 扫描 `app.custom` 下的直接子模块和包，跳过下划线开头的名字。模块需提供：

```python
EXTENSION_ID = "personal.example"
EXTENSION_API_VERSION = 1


def setup(registrar):
    ...
```

契约从 [`app.extensions`](../backend/app/extensions/__init__.py) 导入，完整示例见 [`_template.py.example`](../backend/app/custom/_template.py.example)。当前支持：

| 接口 | 用途 |
| --- | --- |
| `registrar.include_router(router)` | 添加独立 FastAPI 路由 |
| `registrar.register_notification_formatter(id, formatter, order=100)` | 注册 `NotificationFormatter`，调整通知文案 |
| 可选 `startup(context: ExtensionContext)` | 核心数据层就绪后获取数据目录与只读仓库接口 |

重复 ID、版本不匹配或 HTTP 路由冲突会阻止该扩展注册；加载、启动失败会记录日志并隔离。注册表启动后冻结。`NotificationFormatter.format_message(event, context)` 返回文案，不修改输入事件和触发语义；当前没有其他可替换业务策略接口。

## 个人数据与执行边界

- 受信任源码由维护者部署。网页策略入口只接受受限声明，不保存或执行用户 Python 代码；个人策略及存档位于 `data/users/<user_id>/strategies/`。
- 个人数据使用 `UserContext`、`private_storage`，后台任务显式携带归属；共享行情仓库不能写入个人规则、结果、告警或报告。
- 自定义路由沿用 web、collector 的认证和权限边界，不从请求参数决定 owner，不临时替换共享 `app.state`。敏感延迟操作执行前重新验证会话与权限。

## 策略叠加条件

`strategy/overlay.py` 是选股、回测和监控的共享求值入口。设置最多 8 条 AND 条件，支持自定义信号字段、比较运算符和 0–60 个标的交易日偏移；缺省或空列表表示不叠加。非法配置、缺列明确失败，空值和非有限数不入选。

条件在完整窗口上求值，再于评分归一化和数量截断前过滤候选；行情行与退出信号保留。矩阵依赖和历史暖机包含条件引用的字段与偏移。日线普通、矩阵策略支持；组合策略在日线子策略配置，顶层及分钟策略不支持，矩阵策略暂不支持字符串条件。

扩展字段只使用按交易日保存的时序数据，复用 `attach_ext_columns(include_snapshot=False)`，避免最新快照进入历史回测。条件按用户持久化，保存后沿用个人策略缓存、监控失效和前端查询刷新链路。

## 定位与验证

接线实现位于前端 `src/extensions/`、后端 `app/extensions/`；后端测试入口为 `backend/tests/test_extensions.py`。数据源字段、单位和失败语义分别见上述两份数据源文档。

修改注册或插槽时验证版本、ID／路由冲突、默认行为与失败隔离；修改业务规则时断言实际结果。策略叠加条件已有 `test_strategy_overlay.py`、`backtest/test_strategy_backtest_correctness.py`、`test_ext_factors.py`、`ConditionEditor.test.tsx` 和 `StrategySettingsDialog.overlay.test.tsx` 可供定位。
