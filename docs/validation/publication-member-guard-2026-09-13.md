# 发布修订成员闭包读取验证

## 问题与修复边界

人工补充的一条关系包含两个实体提及和一条断言。正式 `Neo4jRetrievalEngine.retrieve` 实测显示：发布后删除该发布指向断言的 `PUBLISHES_KNOWLEDGE_REVISION` 关系，来源快照和发布指针仍在，检索仍能返回整条人工语句。之前只核对发布身份和来源快照，不能识别发布成员集合被破坏。原失败记录为 `.local/whole-document-extraction/manual-probe.log`。

新增共享 `knowledge/publication_guard.py`，核对不可变 `published_revision_ids` 与真实关系端点的精确一致性。首次修复复用原发布上限 500。随后针对整份拓扑候选超过该总量的问题，改为共享的 `MAX_PUBLICATION_CHANGE_RECORDS=500`（单次变更）与 `MAX_PUBLICATION_MANIFEST_RECORDS=2000`（累计清单）；读取与完整发布校验共用累计上限，不另设更小规模限制。当前首先最多读取 2001 条成员关系，只有数量等于有界清单长度才检查成员。容量修复及后续重新验证见[发布容量验证](publication-capacity-2026-09-13.md)。有效成员必须属于同租户、同本体，为已发布的实体提及修订或断言修订，稳定修订 ID 必须精确出现在清单。实际关系数和不同有效 ID 数均等于清单长度，因此缺失、额外、等数量替换、重复关系和重复清单 ID 都不能通过。

合法的 `published_revision_ids=[]` 且零成员关系仍允许发布纯来源文档；本检查不要求使用新的清单版本，v3 合法发布继续兼容。缺少清单属性不被当作合法空清单。

## 读取路径

- 向量、BM25、图扩展、候选重排评分、邻接补全、正文取回和发布来源版本选择共用该守卫。
- 正式检索开始和结束的状态读取均计算成员完整性。已有损坏发布不返回 Chunk 或召回 ID；取回正文后才发生的成员破坏会丢弃本轮结果并按既有有界重试策略处理。保留发布身份，不把损坏伪装成未发布工作区。
- 回答证据重新授权以及外部重排后的状态核对也检查成员完整性；重排失败保持既有“已经调用提供方”的异常，不能再花费一次模型调用自动重试。
- 来源库列表、直接 Chunk 读取及共同的图读取状态检查均使用该守卫；损坏发布的直接证据读取按现有 `GraphViewChanged` 契约拒绝。

该守卫仅验证全发布的修订成员结构，不要求读者拥有整个发布中每条知识的权限。所有原有文档/Chunk ACL、来源撤回、证据位置和校验和检查仍由各读取路径执行。没有改动本体、抽取提示、模型策略、现有业务数据或正在运行的 8002 服务。

## 离线验证

```sh
.venv/bin/python -m unittest tests.unit.test_publication_member_guard tests.unit.test_publication_retrieval_boundary tests.unit.test_retrieval_reranking tests.unit.test_source_library tests.unit.test_graph_browsing tests.unit.test_industrial_retrieval -q
.venv/bin/python -m unittest tests.unit.test_retrieval.RetrievalContractTests tests.unit.test_retrieval_resource tests.unit.test_retrieval_subgraph tests.unit.test_ontology_source tests.e2e.test_knowledge_api tests.security.test_knowledge_api_security -q
```

两组分别 **58/58、65/65 通过**。新增六例覆盖损坏发布不召回、正文取回后成员变动、回答重新授权、重排中变动、直接来源/图状态拒绝和所有正式查询共用同一守卫。既有测试夹具补充真实查询新增的完整性字段；图查询标签计数同步包含新公共守卫；一个无发布却期待召回的旧夹具改用明确的合法已发布状态，保留原本要验证的查询行为。没有放宽安全断言。

日志为 `.local/ontology-source/publication-guard-unit.log`、`.local/ontology-source/publication-guard-regression.log`。Python 编译与 `git diff --check` 通过。

## 真实 Neo4j 验证

独立 `tests/integration/test_publication_member_guard_neo4j.py` 使用一次性测试数据库，所有合成节点在每个子场景事务结束时回滚。首次修复时的矩阵包含 14 个场景：合法空清单/v3、正常成员、缺失、额外、等数量替换、重复绑定、重复清单、跨租户、跨本体、候选修订、错误标签、缺少清单、500 合法和 501 超限。

`test_construction_workflow_neo4j.py` 的原人工关系场景已补充正式来源库读取断言：合法发布可读取原语句，删除断言发布关系后正式检索为空、直接来源读取拒绝。完整套件已在独立 Neo4j 容器中实测 **12/12 通过，391.147 秒，零跳过**；其中人工关系的两个正式读取路径均通过。日志为 `.local/whole-document-extraction/neo4j-verified.log`。

成员矩阵已由标准 runner 在另一独立 Neo4j 容器顺序运行：**1 项测试、14 个子场景全部通过，14.986 秒，零错误/失败/跳过**，测试容器已清理。结果为 `.local/whole-document-extraction/publication-guard-neo4j.json`，日志为同名 `.log`。当前矩阵保留全部安全用例，并将累计边界同步为 2000/2001；后续结果见上方容量验证。重跑矩阵命令：

```sh
sh scripts/run_stage8_neo4j_tests.sh .local/whole-document-extraction/publication-guard-neo4j.json .local/whole-document-extraction/publication-guard-neo4j test_publication_member_guard_neo4j.py
```

这是开发规模的真实 Cypher 和正式读路径回归，不代表生产容量验证或当前 8002 已加载新代码。
