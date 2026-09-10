# 知识构建双页签布局验收（2026-09-10）

本次根据用户确认的方案，替换上一版上下堆叠布局。去掉内容区“知识构建”标题与副标题，保留左侧导航及顶部面包屑。

## 页面与行为

- 顶部为同级“本体构建／实例构建”页签，每次只显示一个工作区；有已启用本体时默认进入实例，没有已启用本体时默认显示本体配置。
- 实例构建采用一个白色面板，紧凑的上传／复核／发布步骤条置于面板内。当前步骤只强调当前位置，不把点击步骤当作审核完成。
- 本体名称与版本收在面板右上角，可查看本体页签，也可展开切换当前抽取使用的本体。未启用的本体明确显示状态，上传仍执行原有服务端前置校验。
- 上传优先展示资料类型和文件选择，随后是标题、访问组。保留真实文件输入控件与键盘可访问性，以统一的中文按钮和文件名呈现。来源设置及示例资料继续按需展开。
- 工作区最大宽度为 1160 px，减少宽屏上过长的表单；本体编辑不再嵌套于大型折叠卡片。
- 页签切换、步骤切换及侧栏往返保留文件、元数据、本体编辑草稿和审核／发布选择。方向键、Home、End 支持页签切换；身份重置清空草稿与页签状态。

没有修改后端来源等级、权限、Neo4j 查询、事务、审核或发布约束。保留已有工作区改动，没有提交或推送。

## 自动化验证

45 项 UI 回归测试通过（3.210 秒）。覆盖资料类型捕获与上传锁定、幂等请求、身份重置与旧响应隔离、访问组、审核和发布选择，以及新增的同级页签默认选择与草稿保留。

首次回归发现一项旧断言要求“滚动到本体卡片”；更新为断言本体页签被选中、实例工作区隐藏，再运行全部相关测试通过。前置校验失败时不提交上传的断言保留。

复现：

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

两项会加载包外语料的既有测试未执行，源码保留；本次使用循环水泵包及中性结构夹具。默认空本体页签通过离线 UI 测试验证，未在真实数据库中创建或清空知识库。测试环境存在已有的 FastAPI / httpx 弃用提示。

## 真实浏览器视觉验收

浏览器连接工具仍没有可用浏览器。使用独立 Chromium 连接正在运行的本地 8002 服务，首先断言 `data_scope=pump-only`。只读取现有水泵知识，没有上传、模型抽取、本体保存、审核决定或发布操作。

```sh
NODE_PATH=/tmp/graphrag-industrial-browser-qa/node_modules \
  node scripts/verify_construction_visual.cjs
```

上述路径是本机已安装的 Playwright；其他环境可配置其已有安装位置。

1280×800、1440×1000、1920×1080 和宽屏全屏，共 16 个截图状态通过：上传、权威类型、复核、发布、本体页签，以及全屏。检查了草稿与当前步骤保留、侧栏返回、键盘页签切换、本体切换输入的版本／未启用提示。

实际查看了笔记本上传和本体页签、桌面权威类型与复核、宽屏全屏等截图。笔记本首屏完整显示文件选择区与标题、访问组控件；三步保持同排，工作区没有横向溢出。JavaScript 异常、控制台错误、失败 HTTP 请求及被拦截请求均为 0。

最终截图及指标：[construction-tabs-visual-2026-09-10](construction-tabs-visual-2026-09-10/)、[results.json](construction-tabs-visual-2026-09-10/results.json)。此前上下堆叠版的报告与截图保留作历史记录，不作为本版验收结果。

本次未运行外部模型验证、真实上传至发布全链路验证或生产部署。
