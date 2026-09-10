# 工作台导航与检索解释优化（2026-09-10）

## 本次范围

在已有未提交改动上增量修改，仅调整工作台前端与对应测试；未改检索算法、Neo4j 查询、权限或持久化。没有提交、推送或重启服务。

- 删除图谱探索的固定绿点及样式；选中项继续由导航背景表达。
- 删除左侧重复的领域区域和身份切换时对它的写入，顶部知识库选择器作为唯一当前库入口。
- 检索选项先说明默认行为；引擎规则、精确文档/版本过滤和高级预算分层折叠。
- 检索过程展示向量、BM25、图扩展、RRF、模型重排与上下文数量。阶段数量按 Chunk 去重，不相加。
- 评分表对应实际返回片段，分别展示候选向量分数/名次、BM25 分数/名次、RRF 分数及模型重排名次/可选分数。明确核心片段、相邻补全、排名补足的用途；缺失分数显示“—”。
- 规则依据 `retrieval/engine.py` 与 `retrieval/ranking.py`：RRF 为各通道 `1 / (k + rank)` 求和；RA 用于图候选扩展；模型排序不与 RRF 分数相加。分数不代表事实可信度。
- 去重、门槛和预算导致未入选的原因以中文汇总；有上下文/重排候选预算排除时，在折叠区外提醒结果有界。原始响应保留在单独折叠的调试数据中，使用文本节点呈现。
- 修复 `SKIPPED_EMPTY` 被误报为已排序，以及重试失败时旧检索过程残留的问题。修正空状态把阅读顺序说成相关性顺序的文案。

## 验证

以下命令实际通过，共覆盖 25 个不同测试（检索选项 13、身份请求 6、隔离 3、静态路由 3）：

```sh
PLAYGROUND_PUMP_ONLY=1 uv run python -m unittest tests.unit.test_industrial_retrieval_options tests.unit.test_industrial_client_migration tests.unit.test_pump_isolation
PLAYGROUND_PUMP_ONLY=1 uv run python -m unittest tests.unit.test_industrial_retrieval_options tests.unit.test_industrial_web.IndustrialStaticRouteTests
node --check src/graphrag_prod/playground/static/industrial/retrieval-options.mjs
node --check src/graphrag_prod/playground/static/industrial/app.mjs
git diff --check
```

第一批运行时检索选项为 12 项，21 项通过；补充重试失败测试后第二批 16 项通过。覆盖空结果、跳过/执行/未知重排状态、只返回名次的模型、不同阶段分数映射、相邻片段缺分、图扩展重复 Chunk、预算警示、文本安全、筛选/身份重置与重试失败。

对正在运行的 `127.0.0.1:8002` 进行了只读 HTTP 检查：bootstrap 的 `data_scope` 为 `pump-only`；页面已无 `nav-dot` / `rail-domain`，保留知识库选择器；实际服务返回的 app.mjs、retrieval-options.mjs、workbench.css 与工作区文件逐字节一致。可直接刷新页面载入。

限制：浏览器工具返回可用浏览器列表为空，未完成截图或真实浏览器交互验收。未发送真实模型检索请求，未运行真实 Neo4j 集成测试（本次没有查询或持久化变更）。测试仅使用离线结构夹具及循环水泵隔离配置，不代表生产验证。测试环境有既有 Starlette/httpx 弃用提醒，不影响通过结果。

## 检查发现的后续问题

| 优先级 | 问题与证据 | 影响与建议 |
| --- | --- | --- |
| P1 | `app.mjs` 的 `isIndustrialIdentity()` 仍按旧工业租户识别；`changePersona()` 据此禁用 `search-asset`，`selectedIndustrialScope()` 也拒绝新库的设备范围。实际 8002 bootstrap 的 industrial.enabled=false，所有当前身份均非该旧租户。 | 当前循环水泵/新建库虽然显示设备编码框，却不能使用这一精确筛选。应根据当前库本体与设备能力提供过滤，并补跨库隔离测试。不能仅解禁输入框，否则后端范围语义仍不正确。 |
| P1 | 证据检索提交 `/v1/retrieval`；`RetrievalResponse` 返回有界 Chunk 与 trace，没有针对“当前库有几台设备”的完整实体计数。 | 返回片段数不能代表设备数量。应提供受租户、权限、发布版本约束的独立统计路径，区分统计和找证据。 |
| P2 | `engine.py` 在候选合并后直接执行 `candidate_ids[:limits.candidate_limit]`；BM25 扫描及图查询也有限额，trace 未为所有早期截断提供明确标记。 | 本次可以提示已有的上下文/重排排除，但不能确认早期召回是否完整。应补各阶段截断/达到上限状态，避免前端靠数量猜测。 |
| P2 | 精确文档与版本筛选仍要求手工输入 ID，预算包含多个专业参数。 | 本次已折叠并提示可从来源资料进入。后续可用带标题、版本与权限过滤的资料选择器替代 ID 输入，并提供参数帮助。 |

上述是相关代码与当前配置范围内的检查结果，不代表全应用漏洞审计。后续问题未在本次扩展实现。
