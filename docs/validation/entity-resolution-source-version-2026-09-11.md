# 新文档版本遮蔽已发布实体：修复与验证

## 原因

当前 pump-only 工作区已经发布“循环水泵”（EquipmentCode = BC-P-101）。第二次上传沿用同一文档来源 URI，产生新的 DocumentVersion，推进 Document 的 ACTIVE_VERSION / ACTIVE_SNAPSHOT；生效的 KnowledgePublication 仍引用原来的不可变版本。

自动消歧和人工选择目标的查询把已发布实体证据绑定到文档最新指针，因此既有实体被过滤掉。输入完整名称无法绕过这个版本范围错误，与中文名称匹配或模型理解无关。

## 修改边界

- 自动匹配通过当前发布版本的 USES_KNOWLEDGE_SNAPSHOT / PUBLISHES_KNOWLEDGE_REVISION 定位来源，再通过 HAS_VERSION 读取精确版本；设备身份属性采用相同范围。
- 人工选择、身份冲突检查、确认归入时的目标校验及目标原文读取允许当前发布版本引用的旧来源。未发布候选仍须来自当前摄取版本；没有开放任意历史版本。
- 属性归属选择共用目标读取规则，其设备编号搜索也保持同一版本范围。
- 保留租户、访问组、本体、证据位置、记录修订、来源撤回和查询数量边界；名称查找仍不自动合并实体。不修改评分公式、资料原文或当前发布版本。
- 沿用原有记录头校验：本次修复针对文档版本推进，不改变实体修订冲突处理策略。

## 验证

- 单元与 API 边界测试：27 项通过。命令：`.venv/bin/python -m unittest tests.unit.test_entity_resolution tests.unit.test_review_context tests.unit.test_api_entity_resolution`。
- 真实 Neo4j 隔离库：9 项通过，162.593 秒。只使用循环水泵测试包和确定性离线嵌入；覆盖同一文档新版本上传后自动匹配、名称/ID 查找、原文三个视图、人工确认归入、跨租户/越权、历史来源排除、身份冲突和事务原子性。
- 隔离库使用 Neo4j 5.26.12 Community、2 CPU、1.5 GiB 容器限制。清空查询缓存后的身份计数与读取为 4.233 秒，保持原有 30 秒上限。开发规模结果不代表生产容量验证。
- 初次隔离运行暴露旧测试夹具未准备当前发布流程必需的嵌入索引，已按正式索引准备/激活流程补齐；没有绕过发布校验。首次共享目标查询还触发 25 秒超时，通过 WITH DISTINCT 分隔查询阶段后重跑通过，没有放宽超时。
- 浏览器：实际 8002 服务上的自动匹配、输入“循环水泵”查找及目标原文请求均成功，1280×800、1440×1000、1920×1080、原文滚动与全屏截图已查看。最终报告无 JS 异常、HTTP 错误或白名单外请求。前一次脚本误关闭已经展开的详情，修正脚本后重新验收；最终未通过缓存或接口夹具伪造匹配结果。
- 仅在隔离库执行确认归入；当前工作区没有知识写入、实体合并或发布切换。按既有启动入口重启 8002，bootstrap 确认为 pump-only。
- `git diff --check`、修改 Python 文件编译检查和浏览器脚本 `node --check` 均通过。

浏览器复现命令（ID 对应本次工作区；其他工作区通过环境变量传入自己的候选和目标）：

```sh
IDENTITY_QA_RECORD_ID=0aaddac7-523b-5d84-a195-6bfe524c1c20 \
IDENTITY_QA_TARGET_ID=2927713f-40d7-5c5a-9d5d-8bdbe33f8c81 \
sh scripts/run_browser_qa.sh verify_identity_resolution.cjs
```

集成测试需先设置指向全新本机临时数据库的 `TEST_NEO4J_URI`、`TEST_NEO4J_USER`、`TEST_NEO4J_PASSWORD`、`TEST_NEO4J_DATABASE`；测试入口拒绝非空库，严禁指向工作台库：

```sh
GRAPHRAG_ALLOW_DISPOSABLE_DB=1 .venv/bin/python -m unittest -v \
  tests.integration.test_identity_resolution_neo4j \
  tests.integration.test_property_assignment_neo4j
```

本地日志与本次临时容器启动脚本位于 `.local/browser-qa/identity-resolution-integration/`；浏览器截图和报告位于 `.local/browser-qa/identity-resolution/`。临时测试容器已清理。
