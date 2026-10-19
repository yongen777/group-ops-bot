# icons/ — Tabler Icons SVG 源文件

本目录存放管理后台界面实际使用的 Tabler Icons **SVG 源文件**，与
`../fonts/` 下的 Tabler Icons webfont（v3.49.0）同版本、同来源，MIT 许可。

## 运行时的图标机制

界面中的图标通过 webfont 类名渲染（如 `<i class="ti ti-layout-dashboard">`），
由 `../css/tabler-icons.min.css` + `../fonts/` 提供，无需引用本目录的文件。

## 本目录的用途

- 需要**内联 SVG**（颜色跟随、无字体加载、打印场景）时，从对应文件取源码；
- 新增图标时按同一版本（3.49.0）从官方包补齐，保持与 webfont 一致；
- 本目录是纯静态资产，不参与任何构建。

## 现有图标

| 文件 | 界面位置 |
|---|---|
| layout-dashboard.svg | 侧边栏「仪表盘」 |
| plug-connected.svg | 侧边栏「群实例绑定」 |
| users-group.svg | 侧边栏「群管理」 |
| chart-bar.svg | 侧边栏「统计中心」 |
| file-analytics.svg | 侧边栏「审计日志」 |
| settings.svg | 侧边栏「系统设置」 |
| info-circle.svg | 各页信息提示条 |
| plus.svg | 新建 / 添加按钮 |
| key.svg | 实例绑定弹窗密钥提示 |
| alert-triangle.svg | 警告提示 |
| chevron-down.svg | 侧边栏折叠箭头 |
