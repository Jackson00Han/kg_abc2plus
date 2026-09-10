# 图谱探索页面精简（2026-09-10）

> 最新状态：用户已授权使用独立 Playwright 浏览器，并明确只做电脑端。真实浏览器连接现已成功，后续改动及桌面视觉验收见 [最新验收记录](graph-visual-2026-09-10/README.md)。下文保留连接成功前的阶段记录，不作为最终验收结论。

## 设计与行为

图谱作为主要内容，移除右上角的实体档案总入口、工业知识图谱标签、发布版本号，以及统计区的授权局部视图说明。保留三个统计项并改为三等分，不改变实际权限与统计口径。质量与维护中的发布信息未修改。

移除右侧常驻的知识详情介绍栏，画布在各宽度下使用单列全宽布局。没有选择时不渲染空白详情区域；在画布提示点选后可到图谱下方查看详情与来源。点选实体、关系、本体声明或属性后，图谱下方的原生 details 元素展开，可手动折叠。空白点击、恢复全图、重新载入或身份切换沿用原有重置链路，清空详情并关闭区域。

来源引用、原文、审核记录和实体的具体档案操作仍可在选中后的详情中使用。全屏时详情最多占 35vh 且可滚动，画布占剩余空间；折叠或隐藏后释放空间。没有新增请求或改变查询、权限、持久化、算法。

修复关联交互：切换到本体声明时递增选择代次，防止之前尚未完成的属性证据请求追加进新详情。异步来源返回不自动重新展开用户已折叠的区域。

## 验证记录

```sh
PLAYGROUND_PUMP_ONLY=1 uv run python -m unittest tests.unit.test_graph_exploration_layout tests.unit.test_pump_graph_views tests.unit.test_pump_isolation tests.unit.test_industrial_web.IndustrialStaticRouteTests
node --check src/graphrag_prod/playground/static/industrial/app.mjs
git diff --check
```

13 项测试通过；JavaScript 语法检查和差异空白检查通过。覆盖真实前端函数的展开、折叠、选择复位、延迟来源返回、属性到本体切换、循环水泵本体筛选、隔离与静态资源安全边界。

实际 HTTP 检查确认 8002 的 data_scope=pump-only，页面已移除指定内容；app.mjs、graph.css、workbench.css 的服务响应与工作区文件一致，刷新即可加载。未重启服务，未提交或推送。

浏览器工具的可用浏览器列表为空，无法进行真实浏览器截图、响应式布局和全屏视觉验收；上述布局结论基于代码实现及离线 DOM 检查。未调用外部模型，未运行真实 Neo4j 集成测试；本次没有相关查询或持久化变更。结果不代表生产环境验收。

## 独立复核与浏览器连接诊断

用户进一步明确：设计建议不是逐项照搬的指令，须独立判断并完成真实浏览器视觉验收。已将此原则加入 AGENTS.md。

复核判断：删除常驻空白详情栏的方向合理，但仅把详情移到下方会降低可发现性。补充按需“查看详情”入口：选中时不打断图谱探索，主动点击后展开详情、滚动至摘要并移动键盘焦点；清空选择后隐藏入口。异步来源返回不能替用户重新展开已折叠详情。纠正关系选中的“点击右侧”旧提示。

本次连接尝试：浏览器工具列出的 apps/browsers 为空；创建 Chrome 页及内置浏览器页均返回 Browser is not available。官方本机诊断脚本确认 Chrome 已安装且正在运行，但连接扩展未安装，本机 Native Messaging 配置不存在。已请用户通过插件界面完成浏览器安装/连接；不通过修改浏览器安全配置绕过连接流程。

因此桌面、窄屏和全屏的视觉验收仍未完成，不能把当前版本称为最优或视觉已通过。关于页头高度、统计区密度、控制区域层级和窄屏布局，须在连接恢复后实测再决定；不能只读早期 CSS 声明而忽略后续覆盖规则。

补充交互验证：`PLAYGROUND_PUMP_ONLY=1 uv run python -m unittest tests.unit.test_graph_details_action tests.unit.test_graph_exploration_layout tests.unit.test_pump_graph_views tests.unit.test_pump_isolation tests.unit.test_industrial_web.IndustrialStaticRouteTests` 共 17 项通过。随后将共用图谱组件提示改为不依赖位置的“查看所选关系的来源与依据”，避免影响其他宿主布局，对应 3 项组件测试重跑通过。app.mjs/graph.mjs 语法检查、git diff --check 通过，8002 服务返回的两个模块与工作区文件一致。上述仍为非视觉验证。
