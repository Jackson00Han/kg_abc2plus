# 上传查重与审核反馈修复（2026-09-11）

本次只处理重复上传、执行反馈、实体归并等待和审核列表跳动。使用 `industrial-demo-v1` 循环水泵测试包及用户由此构建的当前知识；未导入其他语料。保留工作区已有修改和当前知识，未提交或推送。

## 定位与行为

| 问题 | 原因 | 本次行为 |
| --- | --- | --- |
| 重复文件再次进入抽取 | 浏览器只缓存当前重试 operation key；文档身份依赖 URI，改名或新会话可能产生新任务/来源 | 新增服务端内容预检，默认停止重复构建，可查看已有原文。确需更换来源、抽取方式或本体时，展开选项明确继续 |
| 高度相似文件产生重复候选 | 上传阶段没有近重复核对 | 展示相似来源和首处文本差异，用户确认后才能继续；不把相似度当实体身份依据 |
| 执行期间看不出是否运行 | 多处只禁用按钮或使用静态文字 | 上传、匹配、审核、证据读取、检查和刷新增加旋转指示、阶段文字及 `aria-busy`；结束/失败清除，支持 reduced-motion |
| 确认归并等待 | 重算匹配、本体重复读取，加上无关面板串行刷新 | 已明确选择记录时按 record/revision 精确预检，复用本次本体读取；审核队列完成后恢复操作，来源和待发布列表后台刷新 |
| 审核后换成另一个实体 | 直接按新接口顺序整体重绘，丢失展开状态与视口位置 | 保留原分组/来源顺序，新组追加；处理后优先定位同组剩余来源，尊重用户等待期间主动滚动的位置 |

`POST /v1/knowledge:preflight` 是无模型、无知识写入的检查。`POST /v1/knowledge:construct` 在执行 workflow 前再次检查，不能绕过前端直接重复构建。继续构建需要与当前内容、全部上传设置、身份和可见匹配结果对应的 `preflight_token`；这是明确继续的决定回执，不替代权限校验。回执失效后重新核对。

自动匹配结果仍作为参考。用户明确确认时，如同一响应中存在可选择的对应目标记录，携带其 record/revision 进入精确目标检查；没有可核验的目标记录则保留原匹配检查路径。事务仍检查权限、活动本体、来源/发布版本、目标修订、身份编号冲突并原子重绑依赖事实。事实不会随实体确认而自动批准。

## 查重与资源边界

- 完全相同使用不可变版本的原始 SHA-256 与规范化文本 SHA-256；规范化沿用现有解析器（UTF-8 BOM、换行、NFC），保留数字、标点及单位。修改文件名或 URI 不影响按内容发现重复。
- 近重复采用标准字符 5-shingle 的 Jaccard 交并比，阈值 0.85 只触发提示，没有新增自定义加权评分。算法依据：[Stanford《Introduction to Information Retrieval》近重复与 shingling](https://nlp.stanford.edu/IR-book/html/htmledition/near-duplicates-and-shingling-1.html)。
- 只比较同租户可访问的当前入库版本或当前发布版本所固定的来源。检查 Document、Version、Snapshot、全部 Chunk 的关联、权限和撤回状态，读取后再次核验来源选择；不扫描任意历史作为结果，不泄露不可见来源的元数据、计数或差异。
- 相同结果最多 20 条；近重复最多比较 50 个长度在输入 0.8–1.25 倍范围内、各不超过 20 万字符的可见版本。输入超限仍查相同内容，但明确报告近重复未覆盖；范围截断要求用户看到限制并决定是否继续。
- 相似文件并不自动合并，也未实现变更文件的逐字段增量抽取。选择继续后仍沿用现有抽取与审核，保留新来源证据。既有重复知识不会被本次修复自动删除。
- `UploadReservation` 使用唯一约束 `015_upload_reservation.cypher`，按租户、内容和访问范围做短事务占用；同时重复提交不会重复启动 workflow。构建期间不持有数据库长事务，失败后释放，仅当前 owner 可释放。租约使用数据库时钟，1020 秒覆盖 workflow 900 秒上限及收尾余量；对无视自身 timeout 的永久卡住外部调用，不宣称提供强制 fencing。

## 验证

所有持久化写入验证使用临时、独立、标记 ownership 的 Neo4j 5.26.12 Community（2 CPU、1.5 GiB），没有在用户知识库执行构建或审核写入。测试容器已删除并确认不存在。本地工作台只新增 UploadReservation 唯一约束并按既有流程重启；bootstrap 已核验 `pump-only`。

- API/运行时/构建相关 90 项通过：`tests.unit.test_api_upload_preflight`、`test_api_knowledge`、`test_api_runtime`、`test_construction_endpoint_feedback`、`test_construction_authority`、`tests.e2e.test_knowledge_api`。
- 前端相关 55 项通过：`tests.unit.test_playground_review_feedback`、`test_playground_resolution`、`test_playground_property_assignment`、`test_playground`。
- 预检/占用/schema 单测 21 项通过；相关实体匹配/API 单测 39 项通过。最后合并 API backend、实体匹配和上传相关检查，72 项通过（与上述组有重叠，不相加计数）。
- 隔离 Neo4j：身份/属性边界 8 项通过（110.827 秒），上传预检/并发 7 项通过（40.838 秒）。覆盖旧发布来源、精确目标 CAS 冲突、跨租户、单个 Chunk 越权、撤回与成员破损、改名同文、编号变化、4 线程竞争、过期回收与 owner 隔离。
- 只读性能对照：当前水泵候选的人工确认前置阶段，五轮交错中位数 58.8 → 20.8 ms，查询 12 → 4；这是前置阶段，不是整个确认请求的耗时，也不代表生产负载。自动匹配接口消除重复本体读取（14 → 12 次查询）。

可复现入口：

```sh
.venv/bin/python -m unittest -q tests.unit.test_api_upload_preflight tests.unit.test_upload_preflight tests.unit.test_upload_guard tests.unit.test_schema
# 以下只在明确设置 TEST_NEO4J_* 的独立可丢弃库执行：
.venv/bin/python -m unittest -v tests.integration.test_identity_resolution_neo4j tests.integration.test_property_assignment_neo4j.PropertyAssignmentNeo4jTests.test_target_revision_identity_conflict_and_acl_are_enforced_atomically tests.integration.test_upload_preflight_neo4j
sh scripts/run_browser_qa.sh --check
sh scripts/run_browser_qa.sh verify_upload_preflight.cjs
sh scripts/run_browser_qa.sh verify_review_feedback.cjs
```

浏览器使用独立 Chromium 153 / Playwright 1.63.0，覆盖 1280、1440、1920、滚动、全屏。首轮在新增 schema 后冷规划与并行验收/隔离库负载竞争时等待 30 秒超时；没有提高业务超时或放宽断言。清理临时库后串行复验连接与上传通过。原始失败诊断和最终结果保留在 `.local/browser-qa/`。

上传视觉验证已实际查看截图：改名原文命中当前已有来源，未调用构建；`BC-P-101` 改为 `BC-P-202` 相似度 0.9249，双栏差异保留编号与 37.5 kW。已有原文通过真实接口读取。明确继续构建的浏览器响应被拦截模拟 503，以验证动画与失败恢复；没有向当前知识发送构建写入。结果区位于“上传并构建”下方。

审核视觉验收已通过，7 张截图均已实际查看：三个桌面宽度下处理中显示旋转指示，完成后水泵来源 3→2，保持分组与视口，机械密封仍在后方；全屏、滚动、reduced-motion 通过。浏览器中的来源变化采用当前水泵记录构成的响应夹具，3 次确认写请求只在浏览器内模拟，真实知识写入为 0；实际写事务正确性由上述隔离 Neo4j 测试验证。自动建议确认提交精确目标 record/revision 的路径也经浏览器断言。最后新增精确目标及缺失目标 fallback 两项 UI 契约检查后，相关 UI 48 项通过。

日志/截图：`.local/browser-qa/upload-api-unit.log`、`upload-ui-unit.log`、`upload-preflight/`、`review-performance/`、`review-feedback/`。`git diff --check` 与相关 Python/JavaScript 语法检查通过。未调用外部抽取模型，未执行生产容量或持续负载验收。
