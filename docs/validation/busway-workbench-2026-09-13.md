# 母线槽知识构建与材料验证（2026-09-13）

## 验证范围

本轮针对用户提供的源本体、拓扑、诊断实例和规则：修复原本体页面导入与启用，支持一次上传完整拓扑，编写可上传的知识 Markdown 和独立人工检索题集。没有恢复隔离工业语料、启动旧 8000、重置数据库或向 Git 远端提交。

8002 由 `./quick_start.command` 重启，保留现有数据库，bootstrap 的 `data_scope=pump-only`。在页面新建独立“母线槽知识库”，ID 为 `43b248f9-927a-5811-8b86-c2af24bb8cc6`；既有循环水泵知识库保持独立。最终版本重启后再次以服务端签发的管理员身份只读确认：该知识库本体仍为 `PUBLISHED`，原拓扑任务仍为 `COMPLETED`，数据隔离配置保持。

## 本体实际启用

独立 Chromium 在真实 8002 页面选择原始 `busway_files/ontology.source.json` 和 `busway_files/rule.references.json`，点击“保存并启用本体”。实际 HTTP 200，`status=PUBLISHED`，key 为 `ai_power.busway.ontology`，源版本 `5.0.0`，TBox ID 为 `058fe9f4-5042-5cf2-833b-540629bceb11`。整体 checksum 为 `68c03632d9b506d9f3e91021a650cdbef7776fa57ecf0bdc00bbbc99b50af366`。

随后刷新页面，实际验证“载入并校验”“导出 JSON”“复制为下一版本”：十部分源定义逐项与原文件 JSON 一致，规则引用目录保留；副本版本为 `5.0.1`，仅载入编辑器，没有激活副本。原文件作者状态与兼容性注记保留，实际启用状态由服务端及页面显示。

22 类实体与 22 类关系定义完整保存；当前普通文档抽取允许 18 类实体、13 类关系。抽象类型、类型端点和外部派生声明保留定义但禁止变成普通实例事实。详细约束与源合同校验见 [本体验证记录](ontology-source-import-2026-09-13.md)。

## 整份拓扑

本轮开发配置、139 条 JSON 记录边界完整性、调用预算、并发、超时与恢复验证见 [整份文档构建记录](whole-document-construction-2026-09-13.md)。一次提交原文件，由后台分块；用户不需要手工分文件。解析覆盖不等同于实体关系已全部准确抽取，也不等于知识已发布。

真实页面首次构建完整登记了 75 个片段并完成向量化。模型处理至 34 / 75 时，一次 60 秒模型超时使任务进入 `RETRY_WAIT`；已完成片段和模型审计保留。任务 ID 为 `ac4e6fb1-6cd2-54b4-bad5-34ebe5ee88ec`，操作键为 `playground-e391084b-95ad-4a0b-b108-5d8dbdc636f0`。

续跑使用同一操作键及原策略，真实浏览器 HTTP 200，返回同一任务；最终签名身份下的 GET 也确认 `COMPLETED`，`completed_chunks=expected_chunks=75`，完成时间为 `2026-09-13T10:25:22.175021Z`。41 个片段生成候选，29 个片段未通过本体或精确证据校验，5 个片段没有可抽取内容；共 133 条实体提及、446 条属性或关系候选。它们不是 133 个去重实体，也不是已发布事实。原先超时的片段在续跑后通过了校验，旧审计与其余 34 个完成结果被复用。

最终页面没有 HTTP 错误、脚本异常或越界请求。没有为得到成功状态而关闭原文范围、端点或本体检查。此结果证明整份文件能够完成处理和恢复；29 个拒绝片段及候选的实际语义仍需复核，不能宣称 139 条原始业务记录都已完整准确入图。两份业务资料尚未发布，知识说明 Markdown 尚未上传；24 题的实际检索结果仍为待验收。

## 知识说明与验收题

`busway_files/busway.knowledge.md` 含 24 节，保留静态结构、测点语义、候选原因、规则引用、适用范围和四处来源差异。`busway_files/acceptance/busway-retrieval-cases.md` 与同名 JSON 含 24 题；题集不能上传知识库，全部实际结果初始化为 `not_run`，留给用户在选定发布版本上逐题检索。

根代理独立运行 `python3 scripts/verify_busway_knowledge_materials.py` 通过：XML/JSON 的全部 24 个对象、51 个端口和 64 个测点逐值核对；测点语义及规范单位、38 个可观测量、19 条候选原因关系、6 个现象规则引用、11 个未绑定参数、四项来源差异，以及题目的证据位置与校验和通过。该结果是来源材料核对，不能当作模型召回率或生产诊断验证。

## 浏览器与回归

CUA 返回空浏览器列表后，按项目约定使用 `scripts/run_browser_qa.sh` 的独立 Chromium，未接管用户个人浏览器。本体页面实际覆盖 1280、1440、1920 桌面宽度及滚动，查看了实际截图；没有页面异常、横向溢出或越界请求。本体大定义折叠展示，启用状态不挤压换行。

可复现命令：

```sh
sh scripts/run_browser_qa.sh verify_busway_workbench.cjs ontology
sh scripts/run_browser_qa.sh verify_busway_workbench.cjs visual
sh scripts/run_browser_qa.sh verify_busway_workbench.cjs topology
sh scripts/run_browser_qa.sh verify_busway_workbench.cjs receipt
sh scripts/run_browser_qa.sh verify_busway_workbench.cjs review
.venv/bin/python -m unittest tests.unit.test_ontology_source_ui tests.unit.test_playground_flow tests.unit.test_playground_reset_ui tests.unit.test_playground -q
python3 scripts/verify_busway_knowledge_materials.py
git diff --check
```

`ontology` 和 `topology` 阶段会按本任务授权写入所选母线槽知识库；`visual` 阶段只读取本体并在编辑器中操作，不保存副本。首次创建知识库使用单独 `setup` 阶段。脚本每次使用临时上下文，写请求白名单不包含候选审核或知识发布。截图及实际响应保存在 `.local/browser-qa/busway/`，属于本地验证产物，不应提交。

前端与预检查相关 71 项联合回归已通过，最终完整 Playground 回归 141 项通过（22.546 秒）。预检查冷编译超时已经修复，9 项独立真实 Neo4j 安全矩阵全部通过，未提高原 15 秒事务预算。页面脚本遇到的隐藏控件、重复上传折叠确认与异步切库时序问题已经修正。

首次有大量候选后，页面后台对全部审核行预取详情，触发接口限流并阻止构建重试。现在按视口及明确操作加载审核详情，隐藏页面不预取。最终真实浏览器实测：隐藏的 100 条审核队列没有详情请求；滚动查看实体并切换到事实阶段总共触发 9 个所需详情请求，没有 429。未检查或关联实体未确认时，批准按钮保持禁用。三个桌面宽度及滚动截图已经检查。

成功回执改为任务状态、处理进度和结果分类优先展示。完整安全 JSON 摘要及 75 个逐片段校验卡默认折叠，展开后分别限制在 240 / 400 像素滚动区，后续复核操作不被长输出挤走。根代理通过真实已完成任务读取验证默认折叠、完整 75 卡可展开及三种桌面宽度，查看实际截图；没有再次调用模型或审核、发布候选。

完整构建真实 Neo4j 回归 12 / 12 通过（391.147 秒）。其中发现并修复的发布成员闭包问题已通过正式检索与来源读取双路径验证；独立成员矩阵 1 项测试、14 个场景全部通过（14.986 秒，此次仍为原 500 条累计上限），见 [读取边界报告](publication-member-guard-2026-09-13.md)。

最终将单次发布变更 500 条与累计发布清单 2000 条分开约束，避免本次 579 条候选以后分次审核发布时仍被累计上限阻挡。相关离线检查 203 / 203 通过；新的真实 Neo4j 联合回归 2 / 2 通过（235.239 秒）：正式服务依次预览并发布 300 + 279 条，累计 579 条、两份来源和 15 个证据片段均保留且可以正式检索；另经 14 个成员边界场景验证 2000 条合法、2001 条拒绝。保留原单次变更上限、事务预算、权限及证据约束，全部容量测试写入一次性隔离数据库，没有发布用户候选。579 条样例证明累计容量及来源保留；2000 条仅完成读取成员边界验证，不能声称已通过复杂图谱满载性能验证。详细命令、结果和限制见 [累计发布容量报告](publication-capacity-2026-09-13.md)。
