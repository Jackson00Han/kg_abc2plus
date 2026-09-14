# 结构化拓扑构建与性能验证（2026-09-13）

## 实现与边界

8002 保留单一“上传并构建”入口。LLM 模式下，程序在 JSON 对象集合中识别跨记录引用；匹配后使用 `json-field-mapping:v1`，由模型为整份文件提出声明式字段映射，再由程序执行全部记录与引用关联。没有执行模型生成的 Python、Cypher、表达式、外部文件路径或网络请求。

本次自动映射支持 JSON 中的对象数组，包括对象容器内的嵌套集合；不声称已经支持任意 XML、CSV 或 PDF 的自动映射。没有显式记录引用的 JSON，以及普通文档，继续走现有抽取方式。SOURCE_ONLY 不调用映射模型。

- 每份来源最多一次映射请求及一次有界校验纠错，单次 60 秒、8,192 输出 tokens；依赖错误不自动重试。映射请求与逐片段抽取共享工作流并发槽位和总调用预算。
- 映射只能指定集合、标识字段、类型字段/类型映射、属性与显式引用关系；每个集合和字段必须有去向，未知字段明确只保留来源。
- 全量扫描检查重复标识、未知类型、遗漏字段、悬空/歧义引用和本体端点。不存在的外部对象不由模型补造，关系语义由映射提出后仍须复核。
- 来源批次使用 4,000 字符上限，保留连续、无重写的精确原文范围。切片签名参与证据身份，旧片段和旧任务不被覆盖。过大单条记录或输出超过既有校验预算时明确失败。
- 映射精确响应在执行前保存为不可变审计，绑定租户、操作键、原文版本、来源权限、本体、模型及策略；同一操作恢复复用审计。新操作可以重新提出映射。没有把未获人工确认的映射自动推广为所有文件的通用配置。
- 同一来源中的重复设备引用使用稳定候选身份；不同来源文档仍走既有身份确认流程，不根据同名或局部编号直接合并。没有新建跨文件主数据管理服务。
- 形成方式仍为模型辅助的 `LLM_EXTRACTED` 候选，保留原有来源等级、复核与明确发布。程序校验通过不代表来源内容权威或映射语义已获人工确认。

回执展示映射方式、全部来源记录数，以及可展开的字段映射和未入图字段。摘要仅包含经过字段白名单处理的映射结构，不显示原始模型回复或输入样例。

## JSON 字符串语义

实际数据库测试发现：只在映射执行器中解码反斜杠，会被持久化层重新规范化时拒绝。本次增加显式 `source_encoding=JSON_STRING`：raw_value 保留包含引号的完整 JSON 字符串 token；服务器从该 token 解码后重新校验类型、枚举与规范值。该编码随候选、审计、API、审核编辑及发布图的字面量投影传递。

旧 TEXT 字面量默认行为与身份映射不变，默认编码不加入历史 `to_mapping()` 输出。服务端仍重新计算规范值，篡改规范值或去掉 JSON 编码后无法通过验证。没有通过放宽证据范围或关闭持久化校验解决错误。

## 实测

授权来源 `busway_files/topology.source.json`：71,434 bytes，68,958 个规范化字符，24 个工程对象、51 个端口、64 个逻辑测点。

| 检查 | 结果 |
| --- | --- |
| 现有 1,200 字符分块 | 75 个片段 |
| 新结构化证据分块 | 20 个片段 |
| 真实外部模型生成并纠正映射 | 两次请求，合计 22.453 秒（含本地映射检查） |
| 该映射的程序执行初测 | 0.204 秒，139 个不同对象、171 条关系、744 条属性 |
| 真实 Neo4j＋真实向量服务、重放已取得的模型映射 | 153.975 秒，20 个片段，0 个拒绝片段 |
| 实际持久化 | 139 个不同候选对象，310 条来源提及，915 条属性/关系候选 |

最终代码再次调用真实模型：两次请求共 23.037 秒，程序执行 0.288 秒，仍生成 139 个对象、171 条关系、20 个通过校验的片段，未写入用户知识库。本次模型把 `source_annotations` 列为仅保留来源，属性候选为 727 条；前次为 744 条。这说明映射语义仍可能随模型输出变化，未入图字段必须在映射摘要中显式展示并复核，不能把程序校验通过表述为模型映射完全正确。

模型计时与数据库构建计时是分开测量，不能当作一次约 176 秒的完整 HTTP 实测。数据库是独立 1 CPU、1.5 GiB 限额的开发测试容器，构建为四路并发；结果不是生产容量承诺，也不是用户当前知识库的写入记录。数据库写入和向量化仍是剩余耗时的重要部分，没有承诺小文件均可秒级完成。

139 个源标识与生成对象逐项核对；全部显式 CONTAINS、HAS_PORT、HAS_POINT、CONNECTS_TO_COMPONENT 关系与来源引用集合一致，64 个 variable_name 与 JSON 解码后的来源逐值一致。未生成运行数据、诊断结论或派生 PRECEDES 关系。

## 验证命令与结果

新增结构化单元测试覆盖完整性、异构集合/嵌套路径、引用顺序、身份作用域、恢复不重复调用模型、两次纠错上限、JSON 转义及服务端重新校验。另有工作流、字面量、本体、API、发布边界和前端相关回归。

```sh
.venv/bin/python -m unittest tests.unit.test_structured_mapping tests.unit.test_construction_workflow tests.unit.test_construction_parser -q
.venv/bin/python -m unittest tests.unit.test_typed_literals tests.unit.test_domain_models tests.unit.test_knowledge_store tests.unit.test_published_quality tests.unit.test_published_inventory tests.unit.test_construction_extraction tests.unit.test_api_runtime tests.unit.test_knowledge_review -q
.venv/bin/python -m unittest tests.e2e.test_knowledge_api tests.security.test_knowledge_api_security tests.unit.test_relationship_facts tests.unit.test_standardized_publication tests.unit.test_publication_retrieval_boundary tests.unit.test_publication_sources tests.unit.test_publication_comparison -q
.venv/bin/python -m unittest discover -s tests/unit -p 'test_playground*.py' -q
```

已记录的联合回归分别为 183、91、142 项通过，执行式回执/字段映射检查亦通过。日志位于 `.local/structured-mapping/`；这些分组有重叠，不相加为不重复测试总数。

默认真实 Neo4j 测试使用确定性映射与向量夹具，无跳过：

```sh
sh scripts/run_stage8_neo4j_tests.sh \
  .local/structured-mapping/neo4j-suite.json \
  .local/structured-mapping/neo4j test_structured_mapping_neo4j.py
```

本次实测额外设置 `STRUCTURED_MAPPING_PLAN_PATH` 为本地保存的真实模型响应审计文件，`STRUCTURED_MAPPING_LIVE_EMBEDDINGS=1` 启用既有模型服务的真实向量化，`STRUCTURED_MAPPING_BENCHMARK_OUTPUT` 指定指标文件。最终 **2/2 通过，208.613 秒，无跳过**，包括全量候选写入、映射与候选审计恢复，以及跨租户/访问组拒绝。第一次运行的失败日志保留在 `neo4j.log`；编码修复后通过结果为 `neo4j-suite-final.json` 和 `neo4j-final.log`。

真实独立 Chromium 对 8002 页面验证了 1280、1440、1920 桌面宽度、折叠/展开映射、滚动和逐片段结果，并实际查看截图。为避免重新上传用户知识，任务详情 GET 使用隔离的构建回执夹具；它证明页面真实渲染和交互，不代表又执行了一次真实用户上传。请求白名单禁止知识写入。

```sh
.venv/bin/python scripts/verify_structured_mapping.py
# 可选：调用既有模型服务生成映射，仍不写入知识库
.venv/bin/python scripts/verify_structured_mapping.py --live-model
sh scripts/run_browser_qa.sh --check
sh scripts/run_browser_qa.sh verify_structured_mapping.cjs
node --check src/graphrag_prod/playground/static/industrial/governance.mjs
git diff --check
```

连接检查曾在服务重启后首次图查询等待超时，按固定入口重跑通过；自定义页面验收无脚本异常、横向溢出或被拦截的写请求。截图位于 `.local/browser-qa/structured-mapping/`。

## 启用

通过项目既有 `./quick_start.command` 流程重启 8002，保留数据库，继续 `pump-only`。没有替用户再次上传或发布拓扑，也没有提交或推送 Git。用户刷新页面后重新上传；重复检查命中旧资料时，可展开“仍需使用当前设置重新构建”并确认新构建。旧任务与候选不会被自动删除。
