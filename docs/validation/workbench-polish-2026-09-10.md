# 工作台可读性与操作布局优化

日期：2026-09-10。基于已推送的 `ff4a899` 检查点，按用户确认的设计建议优化四个工作台页面。本轮为本地实现与验收，未再次提交或推送。

## 设计与行为

- 统一主要文字层级：导航与正文 15px，常规控件 14px，辅助说明 12–13px；页面标题保持 28px / 宽屏 31px。加深辅助文字，共享原有深绿、浅底、白色卡片体系。
- 检索任务区最大宽度 1360px，提示栏 280–320px，避免宽屏输入区过度拉长。检索结果可复制之前不显示无效的复制操作。
- 来源卡片突出真实文档标题，集中显示版本、片段数量和发布状态。来源名称、地址及文档/版本 ID 放入可用键盘展开的“来源信息”。原始标题与来源等级不改写，原文仍使用指定版本与 Chunk 读取。
- 实例构建采用紧凑的文件选择区，主按钮改为“上传并构建”；仅保存来源模式显示“上传并保存来源”。主操作靠近必要字段且首屏可见，来源/处理设置按需展开。校验失败会显示并滚动到操作附近。
- 复核与发布的空状态提供上传、人工补充或复核队列入口。批量操作依赖可选择的记录；预览与发布按钮仍受忙碌状态、当前选择及有效预览限制。保留人工补充、高级导入、错误状态与草稿。
- 同步处理知识维护以程序方式加入移除清单的路径：使旧预览失效并更新按钮状态，修正该路径的预览函数调用。没有候选新增时仍可预览纯移除操作。
- 图谱节点/关系标签增大，工具栏、图例、详情与来源证据字号协调；保留节点尺寸、布局、来源等级配色与交互。

## 验收结果

真实独立 Playwright Chromium 连接 `http://127.0.0.1:8002/industrial`，先检查 bootstrap 的 `data_scope=pump-only`。使用循环水泵测试包与其已有知识；没有提交上传、抽取、审核、发布或知识移除，也没有调用外部模型验证。

| 检查 | 实际结果 |
| --- | --- |
| 四页样式与来源交互 | 24 张截图，1366×900、1440×900、1920×1080；标题位置、卡片样式、横向边界一致，来源键盘展开和原文读取通过 |
| 构建流程 | 17 张截图，1280×800、1440×1000、1920×1080、全屏；步骤切换、草稿保留、空状态入口及无文件校验通过 |
| 笔记本构建首屏 | 主按钮 y=743–785；访问说明底部 713.64，按钮栏顶部 728，间隔 14.36px，无遮挡 |
| 图谱 | 1280×720、1440×1000、1920×1080、全屏；选择、搜索聚焦、标签开关、来源/本体筛选、证据详情通过 |
| 文字抽查 | 导航 15px；页面说明 14px、对比度 4.87:1；辅助说明 13px、对比度约 4.87–5.25:1；原文 15px、对比度 8.59:1。这是选定元素抽查，并非全站可访问性认证 |
| 自动化回归 | 157 项定向离线测试全部通过；16 个第一方 JS 模块/内联脚本语法检查通过；`git diff --check` 通过 |

已实际查看笔记本上传、校验错误、复核/发布空状态、检索及展开选项、来源卡片/展开信息/原文、图谱与证据、宽屏和全屏截图。最终视觉脚本未报告页面异常；四页/构建脚本未出现失败或被拦截的请求。

初次组合回归发现身份重置测试的摘取式 JS harness 未包含新依赖，已加入实际 `updateReviewBulkActions` 函数并强化私有输出清空与隐藏的断言，最终全量定向重跑通过。图谱首次脚本启动等待超时，经真实 Chromium 诊断后重跑通过。初版粘底操作栏与说明间距不足，已修正并加入间距验收。

本次没有修改后端、Neo4j 查询或持久化，不重新执行真实 Neo4j 写入集成测试；离线 UI 回归不代表外部模型上传/检索链路或生产部署已验证。

## 复现

保留当前隔离配置与 8002 服务。Playwright 安装位置为本次会话现有位置，可替换为本机同版本安装目录。

```sh
NODE_PATH=/tmp/graphrag-industrial-browser-qa/node_modules STYLE_QA_OUTPUT=/tmp/graphrag-polish-final node scripts/verify_page_style_visual.cjs
NODE_PATH=/tmp/graphrag-industrial-browser-qa/node_modules CONSTRUCTION_QA_OUTPUT=/tmp/graphrag-construction-usability node scripts/verify_construction_visual.cjs
NODE_PATH=/tmp/graphrag-industrial-browser-qa/node_modules GRAPH_QA_OUTPUT=/tmp/graphrag-graph-readability-20260910-final node scripts/verify_graph_visual.cjs
PLAYGROUND_PUMP_ONLY=1 .venv/bin/python -m unittest tests.unit.test_workspaces tests.unit.test_restart_workbench tests.unit.test_pump_isolation tests.unit.test_pump_graph_views tests.unit.test_industrial_web tests.unit.test_industrial_client_migration tests.unit.test_industrial_source_catalog tests.unit.test_industrial_retrieval_options tests.unit.test_document_user_group_ui tests.unit.test_playground_flow tests.unit.test_playground_reset_ui tests.unit.test_playground_resolution tests.unit.test_playground_review_flow tests.unit.test_publication_groups tests.unit.test_publication_issue tests.unit.test_review_context tests.unit.test_knowledge_browser_ui tests.unit.test_graph_details_action tests.unit.test_graph_exploration_layout tests.unit.test_graph_viewport_controls tests.unit.test_graph_result_state tests.unit.test_standardized_publication tests.unit.test_playground_demo_ui.PlaygroundDemoUiTests.test_prefill_selects_mode_but_leaves_file_and_all_writes_to_user -q
.venv/bin/python scripts/check_playground_assets.py
git diff --check
```

## 截图与数据

- [检索](workbench-polish-2026-09-10/search.png)、[来源卡片](workbench-polish-2026-09-10/sources.png)、[原文](workbench-polish-2026-09-10/source-original.png)
- [笔记本构建](workbench-polish-2026-09-10/build-laptop.png)、[复核空状态](workbench-polish-2026-09-10/review-empty.png)、[发布空状态](workbench-polish-2026-09-10/publication-empty.png)、[图谱](workbench-polish-2026-09-10/graph-laptop.png)
- [页面结果](workbench-polish-2026-09-10/page-results.json)、[构建结果](workbench-polish-2026-09-10/construction-results.json)、[图谱结果](workbench-polish-2026-09-10/graph-results.json)、[文字抽查](workbench-polish-2026-09-10/text-measurements.json)
