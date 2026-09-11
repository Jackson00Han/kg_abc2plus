# 知识流程加载提示收敛到按钮

## 行为

同一操作只在对应按钮显示旋转图标并禁用按钮。移除请求层创建的右下角加载浮层，以及内容区、结果框和审核行里重复的“正在处理／读取／保存”等过程提示。结果、错误、权限说明、审核判断和持久化任务状态继续显示。

覆盖上传与构建、本体与实例导入、审核保存、实体匹配、事实检查、属性归属查询、发布预览与发布、知识档案、图谱、来源原文、质量检查及发布历史维护。自动读取使用所在区域的刷新或检查按钮；展开原文时使用原文区域的读取按钮。

共享按钮状态支持嵌套请求、失败恢复和身份失效。审核与发布状态重绘时仍保持发起按钮禁用，避免加载期间重复提交。来源与比较弹窗在读取完成或失败后打开，等待期间保留发起按钮的加载反馈。

## 验证

- 51 项相关单元测试通过：流程、按钮反馈、发布分组、身份重置、属性归属、实体匹配，以及知识浏览身份失效与维护弹窗取消检查。
- `sh scripts/run_browser_qa.sh verify_button_feedback.cjs`：15 个场景通过。包含图谱刷新、知识刷新及证据、来源列表及原文、上传、审核刷新及保存、匹配、事实检查、属性归属查询、发布预览及提交、质量检查、真实发布版本比较。
- 浏览器将请求保持在进行中，检查对应按钮有 `aria-busy` 且禁用、当前操作只有一个可见按钮加载指示、无浮层或正文重复过程提示，并检查请求结束后加载状态恢复。
- 已实际查看 1280、1440、1920 桌面截图和全屏截图，验证滚动到操作区域；版本比较包含弹窗内部滚动和关闭。
- `sh scripts/run_browser_qa.sh --check --publication` 通过，真实比较当前可访问版本，在三个桌面宽度检查弹窗布局和关闭。首次运行出现等待比较响应超时；复跑通过，专用反馈检查也独立验证了真实比较请求。
- JavaScript 语法检查、`git diff --check` 通过。

可复现单元测试：

```sh
.venv/bin/python -m unittest tests.unit.test_playground_flow tests.unit.test_playground_review_feedback tests.unit.test_publication_groups tests.unit.test_playground_reset_ui tests.unit.test_playground_property_assignment tests.unit.test_playground_resolution tests.unit.test_knowledge_browser_ui.KnowledgeBrowserTests.test_browser_scope_refresh_clear_and_identity_invalidation tests.unit.test_knowledge_browser_ui.KnowledgeBrowserTests.test_maintenance_dialog_cancels_stale_preview_and_rechecks_removal_selection -q
```

## 数据范围与限制

使用 8002 服务，bootstrap 确认为 `pump-only`。CUA 无可连接浏览器，使用项目固定脚本启动的独立 Chromium 153.0.8010.12，未接管用户浏览器。

当前审核队列为空，浏览器审核与预览夹具从当前水泵已发布知识派生，仅替换浏览器响应。上传、审核保存和发布提交模拟服务失败以验证反馈恢复；实际知识写入为 0。知识浏览、来源读取、质量读取及发布版本比较使用真实只读接口。未调用真实模型构建或执行发布、版本切换、删除。

截图与机器结果位于 `.local/browser-qa/button-feedback/`；版本比较截图位于 `.local/browser-qa/recovery/`。此前要求存在真实待审核记录的 `verify_review_feedback.cjs` 和 `verify_upload_state.cjs` 本次未重跑；上述专用脚本通过浏览器隔离夹具覆盖当前加载行为。本记录不代表后台生命周期或生产部署验证。
