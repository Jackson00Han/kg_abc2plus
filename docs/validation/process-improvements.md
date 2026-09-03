# Stage 1–9 流程评估与改进清单

## 结论与适用边界

本清单基于 Stage 1–9 的实现、验证记录，以及本次运行在
`/tmp/sample-graphrag-stage9-authoritative.yBzbr3/evidence/report.json` 生成的
权威 Stage 9 报告。该路径是运行期位置，持久身份以下列提交和摘要为准。
报告绑定：

- 实现提交：`7142fa331f74ecd868a5ba20d343c787e2f9d367`；
- 报告 schema：`production-candidate-report-v1`；
- 报告文件 SHA-256：
  `92b2865fca874ccca2350c53fb0397a5cb7ea11bcad8c19d3e7ad52663ea9722`；
- semantic digest：
  `71d67bee2c155656cb663f602e92fe30aa2e5f58a35d8848847a7bdc24b4d575`；
- `passed: true`、`production_candidate_eligible: true`、`failures: []`；
- 20 项 Stage 1 验收指标全部通过，包括 Recall@5
  `0.9714285714285714`、检索阶段 p95 `935.1435330027016 ms`、
  检索吞吐 `10.868080177885693 requests/s`、回答 p95
  `176.21599599719048 ms`、服务端错误率 `0` 和未授权暴露数 `0`；
- 测量包络为 10,000 个目标租户活动 Chunks、8 个并发客户端、
  `300.4210446150005` 秒持续窗口和 30 个回答样本。

因此，在已提交的 `production-reference` 参考包络内，没有遗留的
Stage 1–9 验收阻塞问题，可以将该实现称为 **validated production
candidate**。该结论只覆盖报告绑定的代码、配置、数据、确定性 provider、
单容器 Neo4j 和运行环境；不能解释成已经上线、已批准上线、已验证真实
provider，或已证明适用于客户数据。

下面的 P0 是**真实上线前置条件**，不是对权威 Stage 9 通过结果的追溯性
否定。它们在关闭前仍阻止任何 live-production deployment approval。P1 用于
增强验证深度和证据治理，P2 用于降低维护成本与长期回归风险。

## 端到端流程评估

| 证据链环节 | 覆盖阶段 | 评估结论 | 仍受限于 |
| --- | --- | --- | --- |
| trusted source 与验收边界 | Stage 1 | 需求、问题类别、20 项指标、数据 owner 和正/负例均可机器判定 | 真实来源审批和连接器信任仍是 P0 |
| versioned document 与 traceable chunk | Stage 2、3、5A、9 | 稳定业务 ID、不可变校验和、精确范围、24,000-Chunk 装载、活动版本重放、删除和恢复均通过 | 客户数据格式、对象存储和长期保留尚未验证 |
| governed graph | Stage 4、9 | schema、保守实体解析、来源证据、隔离、异常检查和质量门通过 | `load-v1` 的派生图复杂度有限，需补大规模 Assertions/治理发现 |
| bounded retrieval | Stage 5、9 | RRF/RA/邻接补全保持有界；tenant/ACL/活动版本过滤、并发切换一致性和性能门通过 | 单租户偏斜、集群拓扑、长时混合读写需继续扩展 |
| cited answer | Stage 6、9 | 49 例回答、引用、数值、拒答和时序行为通过，来源可回溯到 Chunk | 默认 provider 为确定性 reference，不代表外部 LLM 质量 |
| secure and diagnosable API | Stage 7、9 | 认证授权、限流、超时、错误分类、脱敏、健康检查和 17 个故障/生命周期场景通过 | 多副本共享控制、托管身份、TLS、生产监控仍是 P0 |
| measurable validation | Stage 8、9 | 当前 baseline 两次重放一致，Stage 9 报告和 observations 可字节重建，所有 suite 无失败/错误/跳过 | 证据签名、长期归档、多机多轮趋势仍需增强 |

逐环节检查没有发现会推翻当前参考包络资格的断链、未执行门禁或隐含例外；
已知不足均被明确归入下面的上线前置或后续增强项，而不是被当作已完成。

## P0：真实上线前置条件

### P0-01 客户语料与外部 provider

- 使用代表实际语言、文档长度、版本历史、访问组分布、实体歧义、数据偏斜
  和问题分布的客户语料，建立独立人工标注的检索、回答、引用、冲突、拒答
  和未授权负例；合成 `load-v1` 继续保留为确定性回归层，不能替代该评估。
- 在目标区域和网络路径验证选定的 embedding 与 LLM provider：质量、延迟
  分布、超时、限流、配额、重试、降级、tokenizer、成本、数据保留、区域
  路由和供应商事故行为。
- 对真实来源连接器补充来源真实性、版本获取、内容类型/大小、恶意文档、
  重放和不可变原文归档检查，使“trusted source”由 Stage 1 假设变成经批准
  的部署控制。
- 完成标准：发布负责人批准客户数据集和标注规范；目标 provider 的独立
  报告通过既定门槛；数据处理、保留和成本证据均可追溯且有有效期。

### P0-02 生产身份、密钥、TLS 与分布式访问控制

- 将开发期共享密钥替换为生产身份系统和托管 secret manager，定义密钥
  生成、轮换、吊销、审计、break-glass 和最小权限；数据库、provider、
  服务间及入口流量必须启用经验证的 TLS/mTLS。
- 在所有服务副本上部署共享的租户/ACL 决策边界，验证策略版本传播、缓存
  失效、即时撤权、故障时 fail closed，以及跨副本没有存在性信号。
- 将单进程限流替换为或前置共享的分布式限流与配额控制，验证多副本并发、
  时钟漂移、存储故障、热点租户和绕过尝试。
- 完成标准：身份与密钥责任人、轮换演练、TLS 扫描、Neo4j/provider 最小
  权限、跨副本撤权和分布式限流测试全部形成可审计证据。

### P0-03 目标集群容量、长时稳定性和运行时身份

- 在最终硬件、容器平台、网络、存储和 Neo4j 拓扑上重复容量验证；覆盖
  leader/read replica 路由、滚动升级、节点丢失、连接池耗尽和跨可用区
  延迟，而不是把单个 Neo4j Community 容器结果外推到集群。
- 运行至少数小时至数天的 soak、峰值/突发和容量余量测试，同时约束 API、
  worker 和 load generator 的 CPU、内存、文件句柄及连接数。当前报告明确
  记录 API 与 load generator 为 `host-default-unbounded`，不能作为目标部署
  资源规格。
- 分别记录冷启动、冷 page cache、热缓存、索引重建和扩缩容后的结果；捕获
  CPU 型号/架构、cpuset、cgroup quota、Docker/容器运行时、runner 类型、
  磁盘与网络、Neo4j 所用 JDK 版本/JVM 参数和时钟来源。
- 完成标准：目标拓扑在已批准的峰值及余量下满足 SLO，长时运行没有资源
  泄漏或质量漂移，冷启动/恢复时间满足发布目标，环境清单可重建。

### P0-04 备份、恢复、故障转移与 RPO/RTO

- 在选定的生产存储类上执行加密、访问受控、定期监控的备份和恢复；验证
  密钥恢复、保留/删除策略、跨账号或跨区域复制、损坏备份检测及恢复后的
  schema、索引、向量和业务图一致性。
- 定义并批准 RPO/RTO，以故障注入验证 Neo4j 集群故障转移、区域灾难恢复、
  provider/队列/对象存储依赖故障和恢复期间的读写语义。
- 权威 CI artifact 不包含数据库 dump；合格 dump 及其 manifest 必须进入
  长期、访问受控、带保留策略的证据库，不能只依赖临时本地目录。
- 完成标准：实际恢复演练满足 RPO/RTO，恢复后执行权限、删除、引用和质量
  抽查，监控能发现备份缺失、过期或不可恢复。

### P0-05 监控、响应、隐私和合规

- 建立 SLO/SLI 仪表板和告警：请求量、p50/p95/p99、错误/拒答、队列、
  provider、Neo4j、连接池、备份新鲜度、成本、质量抽样及 ACL 异常；保持
  默认不记录受保护问题、来源文本、prompt 或回答。
- 明确 on-call、升级路径、事故 runbook、审计日志责任人、演练频率及供应商
  事故切换流程。
- 完成数据分类、保留、删除、数据驻留、隐私影响、访问审计及适用法规/合同
  审查。
- 完成标准：告警演练能够触发并闭环，审计保留经过批准，隐私与合规责任人
  对目标数据流签字。

## P1：验证深度与证据治理

### P1-01 同租户万级质量、偏斜与热点图

当前 Stage 9 在大数据库背景中运行 49 个质量案例，并证明全局拥挤不会造成
跨租户泄漏；仍应增加一个同一租户内至少万级活动 Chunks 的客户型质量集，
覆盖长尾查询、同名实体、高频词、超大访问组、时间冲突、异常 hub、单一
Document 占比过高和相似向量密集区。门槛继续使用 Recall@K、MRR、nDCG、
引用/回答/拒答和零暴露，并按数据偏斜分层报告，不能只看总体平均值。

### P1-02 真实增量吞吐与 time-to-query-ready

权威报告的 `69.16685307745486 chunks/s` 只度量原子 graph write；embedding
generation 激活和 full-text/vector index refresh 在计时区间外。增加完整的
source received → parsed → extracted → embedded → published → retrievable
时间线，验证 Stage 1 的每日 100 个版本、20 个版本/分钟突发、部分更新、
重试和 provider 限流。TTQR、积压深度、成功率和陈旧读取时间应成为独立
机器门槛，不能复用 graph-write throughput 的名称或数值。

### P1-03 大规模派生数据删除

Stage 9 固定删除目标覆盖 100 Chunks/embeddings/mentions，但目标本身没有
Assertions 或 `GraphGovernanceFinding`。增加包含大量 Assertions、治理发现、
共享 Entity、多个 embedding generations、失败任务和对象存储原文的删除
场景；验证目标数据完全消失、共享数据保留、tombstone/审计记录正确、搜索
索引不可召回，并在备份恢复后再次检查删除语义。

### P1-04 持续混合读写和切换竞态

现有测试已覆盖受控的并发 publication 和 ACL revocation，并在 corpus
revision/generation 变化时丢弃混合读取。下一步应在持续负载中并发执行创建、
更新、删除、撤权、embedding cutover、检索和回答；注入事务重试、连接中断
及索引 refresh 延迟。报告 revision 重试率、fail-closed 次数、陈旧引用、
授权暴露和吞吐影响，确保保护措施在真实混合流量下仍有界。

### P1-05 多轮、跨机器趋势与容量回归

当前资格覆盖一个五分钟窗口。至少执行多轮独立运行，记录中位数、方差、
置信区间、冷/热差异和跨日趋势；为延迟、吞吐、错误率、内存、GC/page
cache、成本和质量设置相对回归预算。任何硬件或配置改变都应产生新 envelope
版本，不能选择最好的一轮作为唯一证据。

### P1-06 机器可读的上线前置条件

把报告中的自然语言 `deployment_prerequisites` 扩展为版本化 schema，例如
`id`、`owner`、`status`、`required_evidence`、`evidence_digest`、`approved_by`、
`expires_at`、`blocking_scope` 和 `waiver`。生产发布门必须要求所有适用项有
有效证据；缺项、过期、未知状态或无责任人时 fail closed。人工例外不能把
机器失败的 Stage 9 报告改写为通过。

### P1-07 失败证据不可变保留

保留成功与失败运行，不覆盖、不挑选性删除，也不把 diagnostic 改名为
qualification。已记录的两 CPU 非合格探测（检索阶段 p95 `3,872.20 ms`、
吞吐 `2.3476 requests/s`）应与后续八 CPU 通过报告并列，带环境、提交、配置、
失败 gate、日志和 artifact digest。定义失败证据的保存期、访问控制和与修复
提交的链接，使容量决策可复盘。

### P1-08 CPU/cgroup、runner、冷启动和 JDK 证据

现有报告已绑定 8 CPU/3 GiB Neo4j cgroup、Docker daemon 最低 CPU、host CPU/
内存/平台及 transaction timeout。继续捕获 runner 镜像 ID、Docker Engine/
虚拟化层、CPU 型号与指令集、cpuset 和 throttling、磁盘 IOPS、网络、容器
启动/ready/index-online 时间，以及 Neo4j 内部 JDK build、JVM flags、GC 和
page-cache 状态。CI runner 标签只用于调度，不能替代这些观测。

### P1-09 baseline 版本治理和 artifact lock

- 为 baseline 建立 `candidate → reviewed → active → retired` 生命周期，规定
  semver 变化条件、双人复核、变更说明、逐 case/metric/test-ID diff 和回滚；
  阈值降低必须单独修改 Stage 1 contract 并说明原因，不能借 baseline 更新
  隐式放宽。
- 历史 Stage 8 完成证据与后续 baseline maintenance 永久分开；Stage 9 必须
  重放当前 active baseline，不能重写原始完成记录。
- 生成顶层 artifact lock，绑定 implementation commit、contract/profile/config、
  gold/corpus、prompt/schema/provider、Neo4j image、`uv.lock`、runner/action、
  构建脚本、每个 evidence artifact 和最终报告 SHA-256。未知、重复或未锁定
  输入应阻止资格生成。

### P1-10 报告签名、证明和长期归档

对 `report.json`、artifact lock 和关键 dump manifest 生成 Sigstore/in-toto
或等价签名证明，记录 CI workflow/run identity、构建者、提交和时间。将完整
合格证据存入不可变、访问受控、可执行 legal hold 的长期存储，定期验证签名
和可读取性；GitHub artifact 的短期保留不能成为唯一档案。

### P1-11 CI 供应链与依赖治理

- Stage 9 workflow 已按 commit SHA 固定 actions；Stage 8 仍使用 `@v4`、
  `@v5`、`@v6` 浮动标签，应改为 immutable SHA，并由受控自动化提交升级。
- 生成 CycloneDX/SPDX SBOM，扫描 Python 依赖、容器基础镜像和 CI actions 的
  已知漏洞、许可证和来源；记录扫描器/规则版本、例外责任人和到期日。
- 将 `uv.lock` digest、安装解析结果和构建产物 provenance 纳入报告，而不只
  依赖 Git commit 间接绑定依赖。

## P2：可维护性与工程质量

### P2-01 拆分评估核心和长脚本

`src/graphrag_prod/evaluation/production.py` 及 Stage 9 构建/负载脚本承担过多
schema、解析、重算、验证、投影和编排职责。按以下边界逐步拆分，同时用
golden report 和 mutation tests 保证 fail-closed 语义不变：

- evidence schema 与严格 parser；
- identity/digest/artifact-lock 校验；
- suite、质量、性能、生命周期和恢复验证器；
- metric 重算和 qualification policy；
- canonical report/attestation 输出；
- Docker/Neo4j 生命周期、负载生成和文件编排。

### P2-02 静态检查、覆盖率与测试生成

- 在现有 compilation 和 `git diff --check` 之外引入 Ruff、mypy 或 Pyright、
  ShellCheck，并固定工具版本。
- 记录总体和关键模块分支覆盖率，单独设置认证、ACL、摄取发布、删除、引用
  验证和报告资格代码的最低门槛；覆盖率不得替代行为测试。
- 使用 property-based/fuzz 测试覆盖 JSON/schema、JWT/header、Lucene 输入、
  Unicode/URI/时间、数值边界、citation ranges、图 ID、损坏 artifact、报告
  重算和并发状态机；所有崩溃输入都应得到有界、脱敏、fail-closed 结果。

### P2-03 Neo4j 通知和日志降噪

当前空库初始化会产生关系类型/标签尚不存在的预期通知。按 query fingerprint
和 notification code 聚合计数，只对已审查的初始化通知降级，并保留未知、
新增、性能和弃用通知为可见告警。禁止全局关闭通知，因为那会隐藏 schema
漂移、planner 变化或升级风险。

### P2-04 ANN 只能在比较评估后引入

当前 exact authorized cosine 是安全和质量基线。若未来引入 Neo4j ANN，必须
先证明 tenant、active Version、ACL 和 access-policy 过滤发生在候选窗口之前，
并在客户型偏斜语料上与 exact baseline 比较 Recall/MRR/nDCG、尾延迟、吞吐、
内存、构建/迁移成本、授权存在性信号和不同 top-N/overfetch 参数。只有经
版本化比较报告和明确批准后才能切换；“ANN 后再过滤”不能被视为等价方案。

## 跟踪规则

- 每项改进应有 owner、目标版本、验收命令、证据位置和状态；完成后记录实际
  结果，不用计划值代替观测值。
- 任何改变 corpus、阈值、provider、索引方法、资源、并发或持续时间的工作
  都创建新验证 envelope，并完整重跑受影响的回归与资格流程。
- P0 未关闭前，可以继续称当前提交为参考包络内的 validated production
  candidate，但不得批准真实上线。P1/P2 应进入常规路线图，不应通过改写
  历史报告来伪装为已经完成。
