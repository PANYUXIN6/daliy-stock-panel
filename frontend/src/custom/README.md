# 前端自定义入口

`<namespace>/extension.tsx` 会在构建时被自动发现，默认导出 `FrontendExtension`，可注册页面、导航和现有插槽。

类型定义见 [`../extensions/types.ts`](../extensions/types.ts)，示例见 [`_template/extension.tsx.example`](_template/extension.tsx.example)；模板不参与构建。接入说明见 [自定义代码入口](../../../docs/secondary-development.md)。
