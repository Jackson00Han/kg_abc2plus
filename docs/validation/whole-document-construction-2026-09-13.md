# 整份文档构建预算与并发验证（2026-09-13）

## 范围与实现

8002 的旧开发预算只允许 4 次模型调用，每片段预留首次抽取与一次校验纠错，因此实际上只能处理 2 个 LLM 片段。这不是模型只能读取两段。此次使用版本化的 `contracts/profiles/dev-mini-construction.v2.json` 调整开发规模，保留单份来源、稳定 URI、不可变版本、精确 Chunk 证据和审核发布边界，不要求用户手工分文件。

| 限制 | 本次配置 |
| --- | --- |
| 来源字节上限 | 5 MiB |
| 规范化文本总字符 | 262,144 |
| 单份片段数 | 128 |
| 模型调用数 | 256（每片段最多两次） |
| 模型调用并行数 | 4 |
| 单次模型超时 | 60 秒 |
| 单次模型输出 | 8,192 tokens / 65,536 字符 |
| 单次向量化超时 | 30 秒；关闭 SDK 自动重试 |
| 工作流总时限 | 4,200 秒 |
| 构建 HTTP 时限 | 4,260 秒；其他 API 保留 105 秒 |

工作流时限覆盖全部模型调用的预算时间（256 × 60 ÷ 4 = 3,840 秒）并留出准备与持久化余量。它是硬的开发预算，不承诺满载时每个依赖都耗尽超时仍能完成；每次向量化和每次模型调用前检查剩余时限，不足时停止并保留已完成证据。SDK 没有隐式模型重试；校验纠错仍为最多一次，所有请求与回复仍有审计。

启用并行的工作流实例共享并发槽位。8002 继续串行接收构建及索引更新；另一份构建在忙碌时立即返回现有 `upload_in_progress`，不会在长锁等待后继续写入。逐片段结果事务先锁定构建任务，准确更新计数，迟到的成功结果不能覆盖另一片段的 `RETRY_WAIT`。同一操作重试恢复 RUNNING，复用已完成片段及不可变模型审计。

现有同步 `POST /v1/knowledge:construct` 契约保持不变。任务列表和详情新增可空的 `operation_key`，用于浏览器精确匹配本次上传；租户、访问组及能力检查保持不变。新增字段兼容旧记录，不能用最新任务猜测本次进度。

## 数据规模检查

8002 为现有 JSON 解析器启用 `json-record-boundaries:v1`：顶层数组及顶层对象的数组成员按完整记录边界切块，过长元数据仍受原 1,200 字符上限约束。原始规范化正文不重排、不复写上下文、不遗漏字符；新的 splitter 签名参与 Chunk 和处理配置身份，旧分块证据不会被静默覆盖。普通解析器默认仍沿用旧切块方式。

本次实际解析 `busway_files/topology.source.json`：71,434 bytes、68,958 个规范化字符、75 个 Chunk，最大 Chunk 为 1,192 字符。原始来源与业务记录未改，仅将元数据中的过时“须分批”说明更新。24 个工程对象、51 个端口、64 个逻辑测点的全部 139 条记录均完整落在单一片段内；旧普通按行切块为 59 段，但会拆散单条记录。此检查只证明整份受理与完整记录边界，不等同于真实模型提取质量或知识已发布。

## 重复上传预检查

整份源文件准备完成后，首次重复上传检查暴露原有复杂 Neo4j 边界计划冷编译超时：无候选时约 17.5 秒、有候选时单次 EXACT 查询约 16.3 秒，超过既有 15 秒事务限制。此处没有提高查询限额。

修复先执行只返回布尔值的租户与文档 ACL 必要条件检查，覆盖同一 checksum/original checksum 或现有相似度长度窗口全部候选；空结果再次检查，若候选在两次读取间出现则使预检查失败。存在可能候选时仍执行全部快照、来源生命周期、Chunk ACL、唯一归属、完整连续字符区间及当前发布固定关系检查。复杂边界使用 DISTINCT 分阶段，EXACT、SIMILAR 与 TEXT 通过参数共用查询；原有结果上限、Jaccard 方法、源文本 checksum 复核与响应前二次权限复核均保留。TEXT 仅对已选中的最多 50 个版本返回正文，其他模式返回空的 text 字段，API 仍不暴露该内部字段。

优化遵循 Neo4j 官方建议的尽早筛选与参数化查询复用，不使用已弃用的规划器开关：[Neo4j 5 查询优化](https://neo4j.com/docs/cypher-manual/5/planning-and-tuning/query-tuning/)。

对当前授权母线槽工作区进行真实只读验证：整份 topology 的新 EXACT 冷计划约 5.836 秒，连同其他选择与二次复核约 6.12 秒；正式代码缓存后总读取约 0.475 秒，正确返回 1 项相同来源。只在内存中改变元数据一句话的近似副本返回 EXACT=0、SIMILAR=1，首次 TEXT 模式约 5.462 秒，正式代码缓存后总读取约 0.473 秒。没有导入副本、改写用户来源或返回其他租户内容。这些是当前开发数据规模的观察值，不是持续负载指标。

## 自动化验证

以下离线命令通过 105 项检查，覆盖完整多片段、JSON 完整记录与精确字符、四路并发上限、失败后取消未开始的调用、恢复时复用已完成结果、每次向量化前检查截止时间、独立 HTTP 时限及提供方调用设置：

```sh
uv run --locked python -m unittest tests.unit.test_construction_parser tests.unit.test_construction_workflow tests.unit.test_api_runtime tests.unit.test_construction_recovery tests.unit.test_construction_validation_feedback tests.unit.test_construction_endpoint_feedback tests.unit.test_construction_authority tests.unit.test_playground.PlaygroundRuntimeTests.test_playground_source_bounds_extraction_provider_calls tests.unit.test_playground.PlaygroundRuntimeTests.test_playground_accepts_complete_development_document tests.unit.test_playground.PlaygroundRuntimeTests.test_construction_rejects_busy_generation_without_waiting_or_writing -q
```

知识 API 与安全检查通过 28 项，包含 job 列表/详情、跨租户及权限拒绝：

```sh
uv run --locked python -m unittest tests.e2e.test_knowledge_api tests.security.test_knowledge_api_security -q
```

真实 Neo4j 检查在独立空数据库进行；不复用或重置用户 8002 数据库。复现命令：

```sh
sh scripts/run_stage8_neo4j_tests.sh .local/whole-document-extraction/neo4j-suite-verified.json .local/whole-document-extraction/neo4j-verified test_construction_workflow_neo4j.py
```

构建工作流最终 **12/12 通过**，用时 391.147 秒，无失败、错误或跳过。包含 12 个 Chunk 的四路抽取、逐片段审计与完成计数、失败和迟到成功的交错、恢复重放不重复调用、真实持久化、来源等级以及租户与 Chunk 权限检查。此前并行运行出现超时的两个恢复用例，在独占容器复跑时保持原有 15 秒事务和 30 秒局部工作流预算通过；没有提高测试阈值或删除安全断言。

人工补充用例改用正式 `Neo4jRetrievalEngine.retrieve` 和 `Neo4jSourceLibrary.read`，由真实索引与发布固定关系完成读取，不再改写或直接绕过底层检索 guard。此检查复现并修复了原有缺陷：删除活动发布中的断言成员后，人工来源仍可能被召回。当前正常发布仍可读到原文及“人工补充记录”出处；缺失成员后检索返回空、来源读取拒绝，并保留原有未发布不可读和访问组拒绝断言。失败探针日志保留在 `.local/whole-document-extraction/manual-probe.log`；最终通过结果见 `neo4j-suite-verified.json` 和 `neo4j-verified.log`。

共享发布成员检查另外在独立 Neo4j 运行 **1 项测试、14 种情况全部通过**，用时 14.986 秒。覆盖合法空清单的 v3 来源发布、正常成员、缺失/多余/同数替换、重复绑定/清单、跨租户或本体、候选状态、错误节点类型、缺失清单，以及既有 500 条发布上限内有效、501 条拒绝。各场景只建立合成节点并回滚，保留 15 秒事务预算。

```sh
sh scripts/run_stage8_neo4j_tests.sh .local/whole-document-extraction/publication-guard-neo4j.json .local/whole-document-extraction/publication-guard-neo4j test_publication_member_guard_neo4j.py
```

以上构建用例使用确定性提供方夹具，证明实际 Neo4j 持久化与安全边界，不代表外部模型抽取质量；8002 的真实模型和浏览器操作另见 [工作台实测报告](busway-workbench-2026-09-13.md)。

预检查及上传守卫的 29 项离线/API 测试通过，覆盖空候选、两次读取间出现候选、同一参数化计划的三种模式、大输入只做精确匹配及精确/相似结果响应前撤权检查：

```sh
uv run --locked python -m unittest tests.unit.test_upload_preflight tests.unit.test_api_upload_preflight tests.unit.test_upload_guard -q
```

预检查的独立真实 Neo4j 5.26.12 安全回归最终 **9/9 通过**，用时 146.545 秒、无跳过与超时。覆盖精确/近似/规范化匹配、跨租户和私有来源、单个不可读 Chunk、当前发布固定的旧来源、撤回与退休、坏位置及缺失成员、空检查间插入和并发租约；原有 15 秒单次事务预算未变。此前旧计划版本的超时日志保留，最终结果为 `.local/whole-document-extraction/preflight-neo4j-final.json`，日志为同目录 `preflight-neo4j-final.log`。

```sh
sh scripts/run_stage8_neo4j_tests.sh .local/whole-document-extraction/preflight-neo4j-final.json .local/whole-document-extraction/preflight-neo4j-final test_upload_preflight_neo4j.py
```

独立代码审查也核对了参数化前后的授权、来源版本与证据边界，没有发现新增放宽。`git diff --check`、相关 Python 编译检查通过。

## 审核详情按需读取

整份文档产生较多候选后，原页面每次加载最多 100 条审核队列时，会自动读取所有记录的匹配、事实评估及部分属性归属详情；即使正在上传、尚未进入复核页面，也会占用接口请求额度。现在后台只读取队列，实体匹配与事实检查由当前视口中的行或用户点击触发，没有提高接口频率限制。未检查时“匹配实体”“检查事实”可点击，但批准条件仍要求当前版本的有效检查结果。

自动请求保留每类两路工作线程；启动前再次核对行仍连接在页面、未隐藏且处于视口。离开阶段或滚出页面后，未开始的自动任务取消，已开始的请求只保留合法检查结果，不继续在隐藏页面读取属性归属。重新进入视口可继续检查；显式检查不被自动取消。切库、队列版本及观察器代次继续阻止迟到结果或回调作用于新内容；检查面板局部更新保留编辑、选择和焦点。

相关执行式前端测试 **46/46 通过**；审核按需读取这一轮的全部 Playground 离线测试 **138/138 通过**，用时 34.329 秒。覆盖 100 条隐藏队列不预取、仅可见记录触发、离开/返回视口、无 IntersectionObserver 时手动检查、旧身份和旧 revision、并发上限、人工确认与批准门槛；现有上传夹具同步适配真实函数参数和前置查重步骤，保留资料等级、身份切换及不自动重试断言。最终日志为 `.local/whole-document-extraction/playground-ui-verified.log`。

```sh
uv run --locked python -m unittest discover -s tests/unit -p 'test_playground*.py' -q
node --check src/graphrag_prod/playground/static/industrial/governance.mjs
git diff --check
```

这些是执行实际页面 JavaScript 的离线测试；真实桌面宽度、滚动和操作结果仍以工作台浏览器实测报告为准。

## 75 个片段的构建回执

构建回执首先显示任务状态、已处理/总片段数，以及待复核、校验未通过、无抽取结果、仅保存来源和隔离待核对各数量。任务详情中的 `RUNNING`、`RETRY_WAIT`、`FAILED`、`COMPLETED` 分别显示，不根据返回的局部片段数推断任务完成；直接构建响应未声明 job 状态时显示“结果已返回”。候选计数不代表知识已审核或发布，原有下一步按钮及其允许进入复核的条件保持不变。

完整结构化安全摘要放在默认关闭的详情中，JSON 区域最高 240px、可滚动；全部逐片段校验记录放在另一默认关闭的详情中，展开后最高 400px、可滚动。75 个片段的稳定 ID、候选记录 ID、findings、校验次数及响应 checksum 全部保留。继续只展示允许的摘要字段，原始模型响应不进入 DOM，结构化摘要与校验卡均做 HTML 转义。

原有 14 个上传/任务详情安全用例保留；新增 75 个片段折叠与完整保留、运行/重试/失败时局部进度、恶意 HTML 字符与原始响应屏蔽检查，共 **17/17 通过**。与上传流程、切库清理一起 **34/34 通过**，用时 2.794 秒；本次修改后的全部 Playground 离线回归最终 **141/141 通过**，用时 22.546 秒。日志分别为 `.local/whole-document-extraction/construction-receipt-ui.log` 和 `playground-ui-receipt-final.log`。

```sh
uv run --locked python -m unittest tests.unit.test_playground_demo_ui tests.unit.test_playground_flow tests.unit.test_playground_reset_ui -q
uv run --locked python -m unittest discover -s tests/unit -p 'test_playground*.py' -q
```

回执与审核页面的真实 1280/1440/1920 宽度及滚动验收使用已经完成的构建任务，不重新调用 LLM；其截图与观察记录见 [工作台实测报告](busway-workbench-2026-09-13.md)。
