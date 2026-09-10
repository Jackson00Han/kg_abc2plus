# 知识构建页面布局验证（2026-09-10）

## 本次变更

页面分为“本体构建”和“实例构建”。本体编辑与版本管理独立展开；实例使用固定的横向三步：01 上传资料、02 复核候选、03 发布知识。

取消业务／权威的并列流程入口。上传表单首项为“资料类型”下拉，提供业务资料与权威资料，两者共用上传、复核与发布页面。专家实例 JSON 导入归入复核步骤的高级操作。来源 URI、处理参数与示例资料按需展开，文档、本体、标题和访问组保持可见。

来源类型独立于页面导航；切换步骤或展开本体不改变来源类型，也不清空文件、元数据、审核或发布选择。上传在第一次异步等待前捕获资料类型并禁用下拉；请求和幂等指纹保留 `knowledge_scope`。身份重置恢复业务资料，旧身份响应不能解锁新任务。

未修改后端权限、审核、发布、Neo4j 查询或持久化逻辑。没有提交、推送、导入或发布知识。

## 自动化验证

44 项确定性 UI 回归测试通过（3.184 秒）：构建流程、示例表单、身份重置、访问组、复核流程、审核上下文与发布分组。复现命令：

```sh
PLAYGROUND_PUMP_ONLY=1 .venv/bin/python - <<'PY'
import unittest
modules = [
    'tests.unit.test_playground_flow',
    'tests.unit.test_playground_demo_ui',
    'tests.unit.test_playground_reset_ui',
    'tests.unit.test_document_user_group_ui',
    'tests.unit.test_playground_review_flow',
    'tests.unit.test_review_context',
    'tests.unit.test_publication_groups',
]
excluded = {
    'test_workbench_uses_only_the_explicitly_selected_identity_for_all_steps',
    'test_demo_downloads_match_committed_checksums_and_reject_unknown_paths',
}
def cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        elif test._testMethodName not in excluded:
            yield test
result = unittest.TextTestRunner().run(unittest.TestSuite(
    cases(unittest.defaultTestLoader.loadTestsFromNames(modules))))
raise SystemExit(not result.wasSuccessful())
PY
.venv/bin/python scripts/check_playground_assets.py
node --check scripts/verify_construction_visual.cjs
git diff --check
```

两项既有测试会加载当前允许范围之外的语料，因此本次未执行，测试源码保留。其余测试只使用循环水泵包及中性结构夹具。16 个前端模块／内联脚本语法检查通过。测试环境有已有的 FastAPI / httpx 弃用提示。

## 真实浏览器验收

连接工具没有可用浏览器，尝试发现浏览器与创建内置标签后仍不可用。改用项目独立 Chromium 测试环境，连接真实运行的 `http://127.0.0.1:8002/industrial`，先确认服务返回 `data_scope=pump-only`。

```sh
NODE_PATH=/tmp/graphrag-industrial-browser-qa/node_modules \
  node scripts/verify_construction_visual.cjs
```

本机已安装 Playwright 于上述路径；其他环境可使用已有 Playwright 安装位置配置 `NODE_PATH`。脚本限制为本地读取与会话创建，不提交上传、抽取、审核决定、本体保存、预览或发布。

验收覆盖 1280×800、1440×1000、1920×1080，以及宽屏全屏状态；查看上传首屏、权威选项、复核、发布与本体展开的实际截图。检查三步同排、无横向溢出、类型与草稿保留、步骤及侧栏往返。截图和几何指标保存在 [验收目录](construction-visual-2026-09-10/)，最终状态及浏览器错误记录见 [results.json](construction-visual-2026-09-10/results.json)。

最终 16 个截图状态全部通过；已人工查看笔记本上传、本体展开、桌面权威选项、复核、发布和宽屏全屏截图。1280×800 文件选择控件底部为 784.33 px，首屏完整可见。页面无横向溢出，JavaScript 异常、控制台错误、失败 HTTP 响应及被拦截请求均为 0。第一轮脚本因旧标题选择器不匹配而失败，更新为实际标题节点后重跑通过；最终产物对应压缩留白后的页面。

本次不进行外部模型验证、知识写入或生产部署；浏览器使用现有水泵知识库读取结果，不代表新上传至发布的真实服务端全链路验证。
