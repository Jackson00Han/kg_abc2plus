# 工作台检查点验证（2026-09-10）

本检查点保存此前未提交的 Industrial 工作台迁移、知识库隔离、循环水泵测试范围、图谱/构建/检索/来源页面调整和最新侧栏顺序。旧工业资料只记录已有删除，不读取或恢复。未部署生产环境。

## 当前检查结果

- 146 项工作台、权限隔离、草稿与审核发布状态、来源目录及图谱交互离线回归通过。
- 24 项 Industrial 适配与循环水泵构建 UI 补充回归通过。
- 14 项上传安全和前端/API 合约检查通过。首次运行发现测试摘取前端函数时遗漏 `reviewConfirmLabel`；补齐真实函数后全部通过，未改变产品逻辑或移除断言。
- 1 项真实 Neo4j 5.26.12 工作空间持久化集成测试通过：创建两库、分别摄取、重置其中一库、归档保留、另一库不变、旧令牌失效、重启后持久化。
- 集成测试改用 `tests/fixtures/pump_ingestion.py`，仅从授权水泵包读取本体和检修记录，构建精确位置的来源片段与明确版本的离线向量。初次夹具导入/治理配置错误修复后重跑通过。数据只写入本次创建的临时容器；验证后已停止并移除该容器，未操作现用数据库。
- 16 个第一方 JavaScript 模块/脚本语法检查通过；待提交 Python 文件 AST 解析、`git diff --check` 通过。
- 待提交文件完成常见令牌、私钥、带密码 URL 模式扫描与意外生成文件检查，未发现匹配。该检查不等同于完整秘密检测工具审计。
- 最新侧栏已在独立 Chromium 中核对文字删除、导航顺序和页面切换；四页样式与图谱/构建视觉验收见 [页面风格记录](page-style-consistency-2026-09-10.md)。

## 范围与限制

四个加载历史语料的测试未执行：`test_industrial_personas_append_without_changing_legacy_identities`、`test_industrial_restart_only_reuses_sources_and_disables_full_database_reset`、`test_workbench_uses_only_the_explicitly_selected_identity_for_all_steps`、`test_demo_downloads_match_committed_checksums_and_reject_unknown_paths`。原因是当前水泵隔离约束，测试源码和断言保留。没有运行全仓库测试或外部模型服务验证。

真实 Neo4j 集成测试需显式设置独立空白回环测试库的 `TEST_NEO4J_URI/USER/PASSWORD/DATABASE`，再运行：

```sh
PLAYGROUND_PUMP_ONLY=1 GRAPHRAG_ALLOW_DISPOSABLE_DB=1 \
  .venv/bin/python -m unittest tests.integration.test_workspaces_neo4j -q
PLAYGROUND_PUMP_ONLY=1 .venv/bin/python -m unittest \
  tests.security.test_industrial_upload_security tests.e2e.test_knowledge_api -q
.venv/bin/python scripts/check_playground_assets.py
git diff --check
```

测试存在已有的 Starlette/httpx 弃用提示；空白 Neo4j 上存在首次写入前缺失类型/属性的通知，未导致检查失败。
