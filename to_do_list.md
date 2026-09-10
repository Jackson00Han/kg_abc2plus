# To-do List

## Deferred

- [ ] 实现知识库用户分组与访问授权管理。
  当前仅提供一个占位用户组“假想的用户组-1”，新文档访问组使用单选下拉框；
  当前知识库管理员默认可见，不作为用户需要选择的访问组。
  后续支持创建、编辑用户组，分配真实用户，设置知识库成员与文档访问范围；
  区分知识库访问资格与文档可见性。组权限变更应覆盖来源、候选、已发布事实、
  图谱、检索及证据读取，并保留审计；管理员权限严格限定在所属知识库内。
  当前占位组映射到现有 `members`，普通用户为该组成员；管理员的文档访问权限
  随上传隐式保留。未来按需支持多个用户组，避免同时维护两套含义不同的权限。

- [ ] 后期再实现构建工作台的角色分工（上传、审核、发布等）。
  当前工作台已取消右上角治理身份选择，全流程统一使用现有完整权限账号，
  无需切换角色；人工审核、发布及底层 JWT、租户和 ACL 校验保留。
  待确有多人协作需求时，再设计专门审核人员、职责权限与相关测试。

- [ ] 简化 06「发布业务知识」：统一为可勾选的待发布清单，取消 ID 输入。
  **状态：方案已确认，仅记录待办，后续实施。**

  **上下文：** 用户完成 05 审核后，在 06 看到四条 APPROVED 记录的勾选框
  均未勾选，但下方「APPROVED revision IDs」已经自动填入同四条记录。
  当前发布逻辑取勾选项与 ID 输入框的并集，导致未勾选的记录仍可能被发布，
  用户无法直观确认本次发布范围。

  **已确认方案：**
  - 发布范围只由一个待发布清单决定。本轮审核通过的记录默认勾选；取消勾选
    就表示本次不发布，记录仍保持「已审核、待发布」，以后可再选择。
  - 按钮显示「发布所选 N 条」，仅提交实际勾选的记录。刷新清单时不得悄悄
    恢复用户已取消的选择；没有选择时不能执行新增发布。
  - 每条展示可读的实体、属性值或完整关系，例如「循环水泵 → 存在风险 →
    机械密封渗漏风险」，避免仅显示 ASSERTION 和主语名称。
  - 若所选事实缺少必须一起发布的关联实体，明确说明并引导补选，不静默扩大
    发布范围；已有有效发布端点应按实际依赖检查处理。
  - 删除「APPROVED revision IDs」输入框，不再保留高级手动 ID 发布入口。
    ID 仅在可展开的技术详情中只读展示，供排查问题使用。
  - 将「从 active publication 移除的 record IDs」操作迁入「查看与维护」，
    改为选择已发布知识并明确确认移除，保留既有审计及来源数据。

  **验收：** 默认选择、取消选择、刷新、发布失败后重试的范围始终一致；
  未选项不被提交，已审核但未发布的记录可以恢复选择。覆盖关联实体缺失、
  版本变化和身份切换，保留后端权限、依赖及并发校验；同步更新人工测试指南。

- [ ] Design and evaluate evidence-safe identity propagation across multiple
  mentions of one proposed entity. Current authoritative matching only reads
  identity-property facts bound to the current mention revision; a matching
  name or shared model entity ref must not transfer another mention's code.
  The initial 185-character pump report exposed this boundary: the first
  equipment mention had EquipmentCode evidence, while later mentions could
  fail closed with missing-identity CONFLICT. The introductory report now uses
  one equipment mention; retain multi-mention, homonym, conflicting-code,
  tenant/ACL, document-version and revision-staleness cases for the future
  design. Any extension must preserve exact source provenance and auditable
  human decisions rather than bypassing current resolution checks.

- [ ] Evaluate and add an independent reranker after the graph-expansion
  tuning is validated. Compare the current embedding-cosine candidate rerank
  against an established cross-encoder or provider reranker on the versioned
  retrieval dataset before selecting a model or changing production ranking.
  The current external-provider smoke run meets MRR/evidence-recall floors but
  not the stricter production-reference Recall@5 and nDCG@5 targets.
  The 2026-09-05 ad-hoc run also found a temporal/multi-company coverage gap:
  a query requesting Meridian and Harbor FY2024 risk evidence selected
  Harbor FY2023 instead of Harbor FY2024 in the final five Chunks. Keep this
  original failure as a regression example when evaluating ranked-versus-
  adjacent context selection and the deferred reranker; do not count a
  successful explicit Document-filter diagnostic as an unfiltered-query pass.

Completed work and historical checks are maintained in the
[workbench validation](docs/validation/governance-workbench-completion.md),
[industrial completion](docs/validation/industrial-final.md) and
[final regression](docs/validation/industrial-final-regression.md) records.
