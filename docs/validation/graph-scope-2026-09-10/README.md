# 图谱内置范围与刷新：电脑端验收

本轮移除图谱外的整行筛选与文字刷新按钮，将本体范围、来源等级及刷新图标统一放在图谱工具栏。刷新图标保留悬停说明、可访问名称和键盘焦点提示；全屏时这些控件仍可直接操作。

“事实范围”实际绑定 `trust_policy`，筛选权威导入与次权威，因此改名为“来源等级”。本体范围仍由当前 schema 的层级声明及关系类型生成，不把来源等级当成本体层级。当前循环水泵本体没有独立层级声明，显示其已定义的关系类型；离线测试另外覆盖有层级声明时的生成行为。

未修改查询、筛选、刷新、权限或数据逻辑，仅调整标记结构与样式。清理被替代的范围布局样式。

## 验收结果

- 独立 Playwright Chromium 访问 8002 的 pump-only 服务，使用当前循环水泵知识进行只读操作。
- 1280×720、1440×1000、1920×1080 以及全屏交互通过；查看实际渲染截图确认布局。图谱上方不再有独立控件行，主桌面画布起点从 379.36 px 移到 301.17 px，提前约 78 px。
- 实测来源等级和本体关系筛选的请求参数与选择一致；刷新图标保留当前来源等级和关系筛选；控件位于全屏元素内且可点击刷新。
- 原有搜索、选中、来源详情、布局切换、本体模式、全屏聚焦、档案跳转流程通过。未捕获页面异常。
- 25 项相关离线测试通过；脚本语法检查与 `git diff --check` 通过。

[桌面效果](01-desktop.png) · [笔记本效果](06-laptop.png) · [宽屏效果](06-large-desktop.png) · [筛选状态](00-scope-filtered.png) · [全屏状态](05-fullscreen-selected.png) · [测量数据](results.json)

```sh
GRAPH_QA_OUTPUT=docs/validation/graph-scope-2026-09-10 NODE_PATH=/tmp/graphrag-industrial-browser-qa/node_modules PLAYGROUND_PUMP_ONLY=1 node scripts/verify_graph_visual.cjs
PLAYGROUND_PUMP_ONLY=1 uv run python -m unittest tests.unit.test_graph_exploration_layout tests.unit.test_graph_result_state tests.unit.test_graph_viewport_controls tests.unit.test_graph_details_action tests.unit.test_pump_graph_views tests.unit.test_pump_isolation tests.unit.test_industrial_web.IndustrialStaticRouteTests
node --check scripts/verify_graph_visual.cjs
git diff --check
```

工具目录为本机已有的临时 Playwright 安装，并非项目运行依赖。没有操作日常 Chrome，没有重启服务、写入知识数据、提交或推送。此次仅覆盖电脑端和当前小型泵知识图谱；未进行外部模型调用、独立 Neo4j 集成套件或生产容量验证。系统级 Escape 的限制沿用上一轮记录，退出按钮及 API 状态同步已有覆盖。
