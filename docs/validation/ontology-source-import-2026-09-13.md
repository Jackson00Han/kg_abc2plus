# 母线槽源本体导入与静态知识校验（2026-09-13）

## 范围

8002 的 `/v1/ontologies:import` 现支持十部分 `ai_power.knowledge_ontology`、合同版本 `1.0.0` 的源 JSON。原有原生 TBox JSON 契约继续可用，旧定义未使用新扩展时 checksum 不变。

本次使用用户授权的 `busway_files/ontology.source.json` 与 `new_files/rule_v2.md`；不修改原本体和原规则，不依赖历史工业语料、旧数据库或规则执行服务。本体启用不导入业务实体，也不代表知识已经审核发布。

## 保存与使用

1. 在知识构建的本体区域加载 `busway_files/ontology.source.json`。
2. 可同时加载 `busway_files/rule.references.json`，以启用静态规则版本/条款引用核对。
3. 保存并启用。保存与激活在同一事务内使用现有 checksum / active-version CAS；失败不留下半启用的版本。
4. 随后上传拓扑和静态专业知识文档，产生待复核候选，按正常流程核对身份、属性和证据后发布。

源版本 `5.0.0` 对应原生 `version=5000000`。整数映射为 `major×1,000,000+minor×1,000+patch`，major 最大 2146，minor/patch 小于 1000，只接受明确稳定三段版本。源语义版本仍单独保存、返回并供页面显示。

完整原定义以规范化 JSON 保存于不可变 TBox，`source_checksum` 为其 UTF-8 规范化 JSON SHA-256，**不是原文件字节校验和**。`activate`、CAS 字段、规则目录不写入原定义。原本体中的兼容性说明按历史作者声明保留；实际服务能力由导入响应的 `import_capabilities` 给出。

- 源规范化 checksum：`64f8ff53a807a51d96149f1ab1874583172848008c4050176608b700e92d9acd`。
- 源本体加本次规则引用目录的编译 TBox checksum：`68c03632d9b506d9f3e91021a650cdbef7776fa57ecf0bdc00bbbc99b50af366`。
- 规则目录本身纳入整体 TBox checksum；目录或本体内容改变后，已发布的同版 TBox 不可覆盖。

## 执行约束与边界

本阶段运行 profile 为 `governed_document_extraction_v1`。

| 能力 | 实现方式 |
|---|---|
| 抽象类型、继承 | 检查引用与环；继承仅合并相同声明，差异约束拒绝，不自动推算交集或削弱约束。抽象类型保留定义，禁止写入实例。 |
| 实例关系端点 | 类型闭包展开与精确 allowed_type_pairs 校验；CONTAINS 不被扩大为端点笛卡尔积。 |
| 类型端点 | MAY_EXHIBIT / MAY_AFFECT 保留源声明，禁止普通实例关系抽取、写入与发布；不创建假设备类别实例。 |
| 属性 | 保留声明类型、属性存在性、枚举、const、数值上下限、JSON 集合类型及长度、条件必填；服务端重新规范化验证。 |
| 证据与等级 | 继续执行现有 Chunk 精确引用、来源等级、形成方式与审核分离，源本体不能自行授予权限或来源等级。 |
| 发布时完整性 | 必填属性/基数、条件必填、业务身份作用域及唯一性、所有者、项目边界、组成无环、规则引用、现象定位范围等在完整发布清单校验。 |
| 模型提示 | 使用可抽取的类型、关系、紧凑属性约束与语义说明；不在每个 Chunk 提示中复制完整源本体或外置规则全文。 |

明确禁止普通实例抽取/发布的实体类型为 `EngineeringContainer`、`EngineeringComponent`、`ObservableConcept`、`DiagnosticScopeInstance`。前三者是抽象类型，后者必须由外部受治理的派生能力生成。

明确禁止普通实例关系的九类声明为 `MAY_EXHIBIT`、`MAY_AFFECT`、`PRECEDES`、`BRANCHES_INTO`、`TOPOLOGICALLY_ADJACENT_TO`、`EXPOSES_OBSERVABLE`、`EXPECTED_OBSERVABLE`、`INSTANCE_OF_SCOPE`、`HAS_SCOPE_MEMBER`。它们仍在本体中保存，当前导入器不会执行其派生或展开到设备实例。

本次没有建设诊断执行引擎、实时数据/参数注册服务或完整的领域推理系统。拓扑组态不是运行状态，候选根因不是已确认诊断，规则引用不是规则命中。外部派生涉及的运行证据、范围粒度、输入完整性等高级约束，由当前禁止该派生操作的边界保护；本体启用不代表这些外部能力已接通。

## 规则引用目录

目录仅含规则文档名、版本、profile、原文**字节 SHA-256**和条款 ID/行号，不复制规则正文、数值阈值或执行程序。目录由用户在本体导入时显式装配，作为该本体版本的不可变引用核对依据；它不自动提升文档权威性。授权目录文件可用下列命令重建：

```sh
.venv/bin/python scripts/build_rule_reference_registry.py new_files/rule_v2.md busway_files/rule.references.json
```

本次目录含 22 个已定位条款：D1–D6、C1–C5、G1–G5、M2–M7。EVALUATED_BY 必须匹配同一文档/版本/profile中的注册条款；CHARACTERIZED_BY 的 rule_criterion 必须能沿现象规则绑定解析。不加载目录仍可启用本体和构建结构知识；缺少目录、规则版本不匹配或条款未注册的诊断图不能通过发布。

## 自动化验证

```sh
.venv/bin/python -m unittest tests.unit.test_ontology_source tests.unit.test_ontology tests.unit.test_typed_literals tests.unit.test_construction_extraction tests.unit.test_api_ontology_hierarchy tests.e2e.test_knowledge_api tests.security.test_knowledge_api_security -q
sh scripts/run_stage8_neo4j_tests.sh .local/ontology-source/neo4j-results.json .local/ontology-source/neo4j test_ontology_source_neo4j.py
```

实际结果：相关单元、API e2e 与安全测试 **87/87 通过**；独立一次性 Neo4j 5.26.12 容器的 **12/12 集成测试通过**（81.599 秒，零跳过）。集成覆盖原源本体保存并启用、active 读取、幂等回放、完整源/规则目录回读、同版不可变、跨租户、类型/配对限制、持久化枚举复核及完整发布清单必填校验。最初两轮新增夹具分别因未清理关系对象引用、合成字面量未配自己的证据而失败，修正夹具后完整重跑通过；没有放宽任何证据规则。结果文件为 `.local/ontology-source/unit.log` 和 `.local/ontology-source/neo4j-results.json`。

Python 编译检查和 `git diff --check` 通过。浏览器保存启用与整体文件抽取的验收由本次工作台总报告另行记录，不能用本报告的单元或 Neo4j 结果替代视觉验收。
