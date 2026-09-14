# 独立事实审核与身份续审（2026-09-14）

## 问题与修改

原自动预审将同一映射的多个实体事实装入一个顶层记录，只取首、中、尾样本，让模型返回整组 `APPROVE/UNCERTAIN`。程序把组意见复制到每条候选，导致“不同接头的名称不同”被解释为单条事实混入多个实体。页面还会在修订、重新归属后显示未绑定版本的旧意见。

审核策略及模型协议升级为 `evidence-auto-review:v2`：

1. `mapping` 审核的顶层 ID 使用 `mapping:` 前缀，与事实记录 ID 分开。`VALID` 只确认字段映射语义，须引用所有展示样本。不同实体的属性值可以不同。
2. 程序仍对全部结构化事实检查源对象、精确原值、引用、身份端点、约束及冲突。映射有效后，逐条形成程序审核结果；不宣称模型读过每条事实。
3. 映射 `UNCERTAIN` 时，转入 `facts` 逐条模型审核，每个顶层记录为一个断言，使用自己的证据，引用同批其他记录的证据会被拒绝；只将其自己的不确定理由呈现给人工。模型不可用时标记 `INCOMPLETE`，不复制为数据缺陷。
4. 提交前继续使用现有语料锁、身份/事实比较及审核事务。候选版本变化导致批次失败时，有界拆分重校验；未提交项标记未完成，不影响无关候选。服务依赖故障保留未完成，时间预算不因拆分失效。
5. 回执增加可选 `input_revision`。行内仅显示当前 `CANDIDATE`、输入版本相同的意见；无版本旧意见仍可在历史预审明细中查看，不作为当前行结论。显示区分模型存疑、身份阻塞与执行未完成。
6. 人工确认身份后，工作台向同一构建任务提交 `resume_identity_record_ids`。后端先检查这些记录属于当前授权任务且身份已确认，再仅复核依赖这些实体的事实。其他人工待办保留；不在这个路径重新执行上下文补全或其他身份审核。任务关联不明确时不猜测，保留显式“续审未决记录”入口。
7. 旧预审审计保留，升级不自动重批旧候选。新策略独立创建运行记录。实体键继续使用原身份键版本，避免审核协议升级改变稳定实体 ID。

## 沿用能力与范围

当前普通切片为不重叠、完整覆盖的字符区间；结构化拓扑使用对象范围和全局字符位置。此次不新增重叠切片或跳过原文。结构化映射已有字段覆盖、唯一来源标识、引用连接和幂等构建检查，继续复用。

实体来源提及、事实证据分别保留。身份确认不直接批准属性关系；重复事实在受治理发布时按现有语义身份聚合，来源引用不丢失。不恢复隔离语料、不重新上传或发布当前知识。

本次修复聚焦当前结构化拓扑路径。未建立结构化来源身份或映射证明的普通文档仍可能转人工；未新增任意文档自动消歧能力，也未放宽既有跨来源身份、时态冲突或发布约束。对普通文档“没有漏抽”的全量保证不在本次实现范围内。

借鉴：Graphiti 的实体候选解析与逐关系判定、LangExtract 的原文定位、LangGraph 的可恢复人工流程；未替换当前 Neo4j 治理服务或 DeerFlow 平台边界。

- [Graphiti 参考版本](https://github.com/getzep/graphiti/tree/c035afb7990b6077331a81e98b04efcfd9bf8184)
- [LangExtract](https://github.com/google/langextract)
- [LangGraph 人工中断与幂等](https://docs.langchain.com/oss/python/langgraph/interrupts)

## 验证

- 单元、API 和界面行为测试：93 项通过。覆盖映射/事实不同协议、映射存疑回退、单条存疑隔离、提交版本冲突、回执版本匹配、人工续审请求和权限边界。
- 相关构建与上下文回归：34 项通过。旧夹具补上真实回执必需的修订号后重跑通过，结果见 `.local/auto-review-v2/related-tests.log`。
- 真实模型：当前配置模型 `qwen3.8-max`，仅发送合成接头记录。映射审核一次调用返回 `VALID`；逐事实审核一次调用分别返回两条 `APPROVE`、一条故意错误值 `UNCERTAIN`。结果 `.local/auto-review-v2/live-model.json`。这是小规模协议探针，不代表真实语料全量准确率或生产性能。
- 浏览器：CUA 无可用浏览器，按授权使用固定 Playwright 1.63.0 / 独立 Chromium 153。首次连接检查因初次图谱读取超过 30 秒超时；服务日志中请求随后返回 200，按既有入口重启并保持 `pump-only` 后重查通过。
- 浏览器专项：原有 27 张审核/证据/上下文截图与新流程 8 张截图，覆盖 1280、1440、1920、原文展开关闭、滚动、全屏，以及人工确认后恰好一次限定身份续审请求。浏览器写请求由隔离响应夹具接收，没有知识写入。报告在 `.local/browser-qa/auto-review/report.json`、`.local/browser-qa/auto-review-v2/report.json`；真实连接报告在 `.local/browser-qa/recovery/results.json`。
- 真实 Neo4j：使用独立临时容器、确定性模型夹具。最终回归 6 项全部通过，无跳过，耗时 461.811 秒；覆盖身份隔离、逐关系审核、人工确认后的限定续审、失败重试及审计。结果在 `.local/auto-review-v2/result-final.json`，观测在 `.local/auto-review-v2/observations-final/`。测试容器已由脚本清理。
- 本地工作台：使用既有 `scripts/restart_workbench.py` 启动最终代码，保留现有数据库；启动检查确认 `data_scope=pump-only`。重启后的真实浏览器连接检查再次通过。

## 可复现命令

```sh
.venv/bin/python -m unittest tests.unit.test_auto_review_model tests.unit.test_auto_review_core tests.unit.test_auto_review_api_adapter tests.unit.test_auto_review_ui tests.unit.test_playground_resolution tests.e2e.test_auto_review_api -q
.venv/bin/python -m unittest tests.unit.test_structured_mapping tests.unit.test_construction_parser tests.unit.test_context_property_evidence tests.unit.test_context_projection tests.unit.test_context_mapping_api -q
sh scripts/run_stage8_neo4j_tests.sh .local/auto-review-v2/result-final.json .local/auto-review-v2/observations-final test_auto_review_neo4j.py
sh scripts/run_browser_qa.sh --check
sh scripts/run_browser_qa.sh verify_auto_review.cjs
sh scripts/run_browser_qa.sh verify_auto_review_v2.cjs
.venv/bin/python scripts/verify_individual_auto_review.py
node --check src/graphrag_prod/playground/static/industrial/governance.mjs
git diff --check
```

模型探针使用已配置服务，会产生两次合成数据审核请求；其余测试不调用外部模型。浏览器与模型探针均不批准或发布用户当前知识。
