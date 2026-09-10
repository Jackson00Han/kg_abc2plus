# 工作台页面风格统一（2026-09-10）

## 改动范围

以证据检索、来源资料为样式基准，调整图谱探索和知识构建。

- 图谱探索增加 `EXPLORE CONNECTIONS` 与「看清设备之间的联系。」，保留节点、关系统计及证据入口。
- 知识构建增加 `BUILD WITH EVIDENCE` 与「把资料变成有据可查的知识。」，说明本体、上传、复核和发布流程。
- 移除图谱独立的标题字号与页首间距覆盖，两页使用共用的 `.page-heading`、`.eyebrow`、`h1` 样式。
- 主卡片共用 8px 圆角与边框色，输入控件共用 6px 圆角；图谱移除外层阴影。
- 构建区域继承工作台文字、边框与绿色强调色，缩小页签层级，统一卡片内边距；保留 1160px 最大工作宽度，并与页首左侧对齐。

产品改动仅涉及 HTML/CSS；补充并更新浏览器验收脚本。未修改业务逻辑、查询、权限、数据或模型配置，未提交、推送或部署。

## 已执行检查

```sh
PLAYGROUND_PUMP_ONLY=1 .venv/bin/python -m unittest \
  tests.unit.test_playground_flow \
  tests.unit.test_graph_exploration_layout \
  tests.unit.test_graph_viewport_controls \
  tests.unit.test_graph_details_action \
  tests.unit.test_document_user_group_ui
git diff --check
```

结果：24 项离线回归通过，差异空白检查通过。覆盖既有的构建页签、步骤、草稿保持、图谱详情与视口控件行为；这些检查不代表视觉验收。

本地 `http://127.0.0.1:8002/playground/bootstrap` 返回 `data_scope=pump-only`。通过 HTTP 比对确认服务已提供当前 HTML 和三个 CSS 文件。HTML 按服务现有的 `pump_only_page` 过滤后比对；首次直接对比原始文件的断言因隔离过滤而失败，按实际服务转换复核后通过。

## 真实浏览器验收：已补齐

浏览器连接工具的可用列表为空，Chrome、内置浏览器与重置后自动连接均失败。这只说明该连接工具没有接入浏览器，不能据此判定独立 Chromium 不可用。此前已获授权的独立 Playwright 路径仍可正常使用；本次经用户提醒后沿用该路径完成验收。

本机依赖位置：`/tmp/graphrag-industrial-browser-qa/node_modules`。普通 `require.resolve('playwright')` 未找到包是模块搜索路径问题；设置既有 `NODE_PATH` 后加载成功，无需重新安装浏览器或依赖。

```sh
NODE_PATH=/tmp/graphrag-industrial-browser-qa/node_modules \
  GRAPH_QA_OUTPUT=docs/validation/page-style-2026-09-10/graph \
  node scripts/verify_graph_visual.cjs
NODE_PATH=/tmp/graphrag-industrial-browser-qa/node_modules \
  CONSTRUCTION_QA_OUTPUT=docs/validation/page-style-2026-09-10/construction \
  node scripts/verify_construction_visual.cjs
NODE_PATH=/tmp/graphrag-industrial-browser-qa/node_modules \
  node scripts/verify_page_style_visual.cjs
node --check scripts/verify_construction_visual.cjs
node --check scripts/verify_page_style_visual.cjs
git diff --check
```

三组真实 Chromium 检查均通过：

- 图谱：1280×720、1440×1000、1920×1080 及图谱全屏；验证范围筛选、刷新、搜索定位、布局切换、关系标签、节点与连线详情、全屏进出。无横向溢出，普通视图节点最小实际字号约 12.49px，JavaScript 异常为 0。
- 构建：1280×800、1440×1000、1920×1080 及宽屏全屏，共 16 个截图状态；验证本体/实例页签、三个步骤、键盘导航、草稿保持、资料类型与本体切换。文件选择区完整位于首屏；三步保持同排。JavaScript 异常、控制台错误、失败 HTTP 请求、被拦截请求均为 0。
- 四页对照：1366×900、1440×900、1920×1080，共 15 张截图（四个主页和本体页签）。实测各页主标题的 x/y 位置、字号、行高、字重、颜色及英文小标题样式完全一致，卡片左边与标题对齐，主卡片圆角、边框及背景一致，无横向溢出。JavaScript 异常、失败 HTTP 请求、被拦截请求均为 0。

实际查看本次截图，包括笔记本构建页、图谱桌面和选中详情全屏、构建宽屏全屏、证据检索与来源资料的笔记本对照、1440px 构建页、1920px 本体页签。标题层级、卡片边界和留白协调，未观察到遮挡或横向溢出。构建表单保留自然纵向滚动；宽屏工作区保留 1160px 上限。

截图和可复现指标：[page-style-2026-09-10](page-style-2026-09-10/)，分别见 `graph`、`construction`、`comparison` 子目录。既有构建脚本按此次明确的新需求，将“没有页首”断言更新为“恰好一组页首且包含指定介绍”，其余断言保持不变。

验证只访问 `8002` 的循环水泵测试包及据此建立的当前知识；文件选择和草稿测试仅发生在独立浏览器会话内，没有提交上传、模型抽取、本体保存、审核决定或发布。未进行外部模型验证、生产部署或真实写入全链路验证。浏览器工具的连接状态本身未修复，后续可继续复用上述独立 Chromium 命令。
