# 小帅工作台图标资产

图形由方括号与紫色 K 线组成。SVG 为源文件，PNG 为预缩版本。

| 文件 | 用途 |
| --- | --- |
| `logo.svg` / `logo-dark.svg` | 浅色 / 深色背景 |
| `logo-white.svg` | 纯白标记 |
| `logo-badge.svg` | 方形徽章 |
| `logo-tile-light.svg` / `logo-tile-dark.svg` | 带底卡图标 |
| `logo-title.svg` / `logo-dark-title.svg` | 裁剪留白后的行内标题图标 |
| `png/` | 对应 PNG 及菜单图标预览 |

## 接入指引

- 页面 favicon 位于 `frontend/public/favicon.svg`，菜单图标在 `frontend/src/components/BrandIcons.tsx`。
- 小于 32px 时可选用对应尺寸的 PNG，避免缩放损失细节。
