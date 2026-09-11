# 文本与知识统一发布版本：实施与验证

## 目标与边界

发布版本是一份不可变清单，绑定来源文档版本、Chunk 和受治理知识修订。正式检索、图谱、引用和问答使用同一当前发布版本。上传与审核属于待发布工作区；回滚不会删除原文、知识修订或审核历史，也不会改变其审核结论。历史版本可以重新设为当前版本，仍须通过当前权限、来源可用性和完整性检查。

复用未变化的来源与向量，不复制整库。显式来源撤回和永久清理不是版本切换；不得借历史版本恢复已撤销的访问权限或被明确撤回的来源。实体可能被多来源支持，按版本清单与证据关联处理，不能按文件级联删除共享实体。

## 实施顺序

1. **发布快照**：在现有不可变知识清单与 `USES_KNOWLEDGE_SNAPSHOT` 上固定文本范围，校验文档版本和 Chunk 闭包；安全兼容可验证的已有发布版本，缺失或冲突时明确拒绝。支持空的已发布版本。
2. **读取边界**：正式向量/全文召回、图扩展、相邻片段、原文读取、来源列表和图谱浏览按当前发布清单过滤。保留审核区原文读取能力；无发布版本时不得退回全部上传文档。
3. **历史切换**：从历史清单加载来源，不依赖文档最新版本指针。验证当前 ACL、显式撤回及必要索引，原子切换并保留激活审计；查询携带版本边界，检测中途切换。
4. **交互**：统一使用“设为当前版本”，切换前比较文档与知识影响，保留并发版本检查，说明历史保留与正式检索范围。
5. **验证与交付**：确定性单元/API测试；隔离真实 Neo4j 完整生命周期与越权测试；真实浏览器桌面视觉与关键操作验收；差异检查后更新 8002 pump-only 本地服务。

## 必须覆盖的验收情景

- V1 发布文档 A；上传 B 后未发布，正式检索及来源列表不可见 B。
- V2 发布 B 及其知识；切回 V1 后 B 的文本、实体、关系、属性与引用不再属于当前正式范围。
- 再设 V2 为当前，复用原文、知识修订和向量恢复，不重新抽取。
- 同一文档更新内容后，旧发布使用旧文档版本，恢复新版使用新文档版本。
- 删除所有已发布知识形成空版本后仍能恢复；共享实体不误删。
- 当前权限收紧、跨租户、来源明确撤回、篡改或缺失清单不能被历史切换绕过。
- 同时发布或切换时拒绝过期操作，问答与缓存不混用版本。
- 保留此前属性归属修正功能。

## 实际结果

实现完成。下列验证使用 `PLAYGROUND_PUMP_ONLY=1`，真实数据库测试在独立、初始为空的 Neo4j 5.26.12 Community 容器运行，不使用用户工作区作为测试夹具。

| 验证 | 结果 |
| --- | --- |
| `test_publication_snapshot_neo4j.py` | 4/4，163.081 秒：待发布不可检索、双向恢复、同文档旧版本、空发布、当前权限/撤回及索引覆盖 |
| `test_publication_graph_snapshot_neo4j.py` | 4/4，178.267 秒：实体/关系/属性/引用同步、图扩展与邻接范围、BM25 截断前过滤、版本激活与 ACL 复核 |
| `test_publication_snapshot_integrity_neo4j.py` | 6/6，48.591 秒：v3 兼容迁移、v4 清单篡改、原文越界/空位置、审核草稿与发布修订分离、API 预览/比较与空版本 |
| 原有 `test_property_assignment_neo4j.py` | 3/3，200.796 秒；新增发布后完整质量检查也通过 |
| 新发布来源/检索边界/质量/来源浏览/回答 pin 单测 | 33/33 |
| 针对版本切换、比较、发布预览、缓存与身份失效的 UI 单测 | 6/6 |
| Python 编译、3 个变更 JS 模块语法与 `git diff --check` | 通过 |
| 真实浏览器视觉验收 | 独立 Playwright/Chromium 已恢复；1280、1440、1920 桌面宽度的历史选择、真实比较、弹窗滚动与关闭通过，截图已查看；未在用户库执行实际版本切换 |

真实集成合计 17 项，零跳过。第一轮同文档更新测试发现正常历史 snapshot 的 `retired_at` 被误当成显式撤回；已区分普通替代与 `retirement_id` 等明确撤回标记，并重跑相关测试通过。测试中的向量是确定性离线夹具；未将其表述为外部模型服务验证。

可复现命令（需先设置 `TEST_NEO4J_URI/USER/PASSWORD/DATABASE` 指向独立空库，并设置 `GRAPHRAG_ALLOW_DISPOSABLE_DB=1`；每个模块运行前确保测试库为空）：

```sh
PLAYGROUND_PUMP_ONLY=1 .venv/bin/python scripts/run_test_suite.py --start tests/integration --pattern test_publication_snapshot_neo4j.py --output /tmp/publication-core.json --require-no-skips
PLAYGROUND_PUMP_ONLY=1 .venv/bin/python scripts/run_test_suite.py --start tests/integration --pattern test_publication_graph_snapshot_neo4j.py --output /tmp/publication-graph.json --require-no-skips
PLAYGROUND_PUMP_ONLY=1 .venv/bin/python scripts/run_test_suite.py --start tests/integration --pattern test_publication_snapshot_integrity_neo4j.py --output /tmp/publication-integrity.json --require-no-skips
PLAYGROUND_PUMP_ONLY=1 .venv/bin/python -m unittest tests.unit.test_publication_sources tests.unit.test_publication_retrieval_boundary tests.unit.test_publication_quality_scope tests.unit.test_source_library tests.unit.test_answer_publication_pin
```

当前本地工作区已通过只读兼容性校验，并运行 `scripts/prepare_publication_source_scope.py --tenant-id <当前工作区租户> --group members --apply`：为当前发布的 2 份文档、2 个片段补齐派生全文检索范围和 legacy 嵌入空间绑定。原始 v3 清单及其哈希、来源内容、审核记录、激活历史保持不变。该脚本默认仅验证；`--apply` 才创建 v3 全文索引并写入派生标记。历史版本在后续激活时执行相同的完整性和兼容性检查。

本地 8002 已按 pump-only 配置重启，使用原数据库。重启后的认证接口检查：正式来源列表、发布历史、图谱浏览、质量检查均返回 200；质量检查通过，来源列表为 2 份资料，已明确撤回的 `homonym_report` 不可见。临时测试容器已删除。没有提交、推送或生产部署。

## 设计取舍与实际限制

- 复用 `USES_KNOWLEDGE_SNAPSHOT` 和已有文档/片段/向量，发布 v4 额外保存来源清单、校验和及单一嵌入空间；不保存整库副本。
- 正式来源以最终发布记录支持的完整文档快照为单位。同一发布不允许同一文档同时出现两个版本。删除某一属性而该来源仍支持其他发布记录时，原文仍属于该版本；预览会显示来源未退出。纯文本无实例的显式独立发布不在本次范围内。
- 历史切换不改变上传工作区的最新文档指针，不覆盖审核草稿，也不将历史记录自动标为隔离。明确撤回的来源不能通过恢复版本绕过。
- 全文使用稳定的租户/权限组/文档版本 token，在候选窗口之前限定版本；向量复用同一嵌入空间中的已保存数据。配置不兼容或覆盖不全时明确拒绝，不静默混用模型。旧全文索引保留用于迁移安全，不复制原始数据库。
- 发布来源最多 500 个快照、20,000 个片段，并在读取内容前限制文档/片段总字符；发布和历史事务有明确超时。超过限制需缩小操作或准备相应规模的方案，不能以开发测试代表生产容量。
- 问答在模型调用前后复核当前版本、激活代次、来源权限与精确证据，响应携带所依据的发布版本。并不承诺模型每次输出逐字相同。
- 被隔离的其他工业语料、旧厂商目录路径未恢复、未用于本次验证。此前 CUA 连接失败已通过独立 Chromium 备用路径解决，恢复步骤和实际验收边界见 [浏览器恢复记录](browser-recovery-2026-09-11.md)。实际双向版本切换由上述隔离 Neo4j 集成测试覆盖，未在用户当前数据上点击确认切换。
