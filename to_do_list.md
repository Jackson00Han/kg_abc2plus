# To-do List

## Deferred

- [ ] 重设计知识构建第 04 步：按实体分组，先确认身份，再审核事实。
  **状态：仅记录后续改进，本次不实施。** 上下文见
  [人工测试指南第 6 步](docs/industrial_demo_walkthrough.md)。

  **用户反馈与问题背景：** 用户实际测试泵站示例时，认为指南第 6 步过于
  复杂、缺少清晰顺序。当前页面将实体提及（ENTITY_MENTION）与事实
  （ASSERTION）平铺展示；设备编码、额定功率、包含部件、存在风险这四条
  事实卡片都以“北辰一号循环水泵”为标题，容易被误解为多个重复实体。
  “机械密封渗漏风险”已是候选，但按钮和说明也容易让人把“审核批准”误解
  为“加入候选”。此外，对正确但重复的编码和功率点击“拒绝”不符合用户直觉。

  **建议交互：** 同一实体显示一张主卡片，卡片内先展示身份匹配依据，提供
  “确认使用已有实体”或“确认新实体”，以及拒绝、暂缓操作；再以表格展示
  该实体的属性和关系，每行显示内容、原文证据、检查结果及审核操作。
  泵卡片中集中展示 EquipmentCode、RatedPower、CONTAINS、EXPOSED_TO。
  风险实体拥有自己的身份确认卡片；泵与风险的关系显示关联实体的确认状态，
  关系审核与实体确认仍是独立动作。同一关系的审核状态应同步，避免重复操作。

  **重复与冲突处理：** 增加与已有权威事实的比对。完全一致时提示“已有
  相同的权威事实”，提供“保留已有事实，不重复入图”；保留本次来源及处理
  原因的审计，不把正确事实描述为抽取错误。比对需考虑实体、属性、规范化
  值、单位及适用时间等语义，不能只比较显示文本。值或适用条件不一致时，
  并排展示双方事实与来源，明确标记冲突，交由用户判断。

  **展示与顺序：** 默认突出业务名称、匹配依据、属性、关系和可展开的原文
  证据；JSON、record ID、revision、模型标识折叠到“技术详情”。顶部展示
  实体身份确认和事实处理进度。准确区分“已经是候选”“审核通过”“已发布”，
  审核完成后引导用户前往第 05 步明确发布，并同步简化人工测试指南。

  **必须保留的边界与验收：** 按实体 ID 分组，不按同名直接合并；每条提及
  和事实保留独立来源、revision 与审核记录。界面分组不等于跨提及传播身份
  属性，相关能力仍由下一项独立设计。自动建议不能自动链接或发布，实体
  确认不能自动批准相关事实，模型来源不能升级为专家权威。保留权限过滤、
  过期结果保护与不可变审计。以后实施时，用泵站示例验证用户能区分一个
  实体及其四条事实、按顺序完成审核，并覆盖同名不同编码、重复属性、冲突、
  关联实体未确认和身份切换场景。

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

## Current work

- [x] Bound model validation feedback to one corrective call per Chunk, with
  per-call budget/deadline checks, immutable attempt audit and safe UI summaries.
  Provider failures and oversized output do not auto-retry. Preserve default
  single-attempt compatibility, exact evidence, review and publication gates.


- [x] Provide a versioned pump-maintenance exercise, downloadable source files,
  source-only construction, exact runtime-ID binding for expert import, and a
  Chinese step-by-step browser walkthrough. Automatically compute bounded
  entity-resolution suggestions while preserving explicit linking, review
  and publication.

- [x] Tighten the local Playground graph-expansion limits and verify retrieval
  quality, tenant/access isolation, bounded execution, and Retrieval Trace
  behavior. Completed with 49/49 authenticated HTTP paths, Gold-aware provider
  smoke metrics, and zero unauthorized exposure under the external
  `text-embedding-v4` profile.

- [x] Complete the industrial property-graph construction loop: versioned
  T-Box, authoritative A-Box import, document upload, ontology-constrained LLM
  extraction, candidate review, publication/rollback, trust-aware subgraph
  retrieval, permissions, audit, and Playground management views.

- [x] Add evidence-backed typed entity-property facts with server-normalized
  datatypes, units, and explicit validity/observation times.

- [x] Add versioned extraction quality and drift gates covering exact evidence,
  entities, relationships, typed properties, entity resolution, trust
  contamination, and human-review policy.

- [x] Connect conservative entity-resolution suggestions to the governed
  review workflow and Playground. Exact values for every declared
  `identity_property` can propose a unique authoritative link; ambiguous,
  incomplete, stale, or unauthorized matches fail closed. Applying a link
  creates immutable revisions and atomically rebinds dependent candidate
  assertions without approving those assertions.

## Live-acceptance checks

- [x] Diagnose and correct provider-backed upload/extraction/review/publication
  acceptance for `qwen3.8-max`. Default deep thinking exhausted the unchanged
  30-second provider bound. Explicit non-thinking execution and mechanical
  source-position hints passed a fresh real-model workflow: three entities,
  five mentions, two relationships and one typed property; 22 checks cover
  exact evidence, review/publication isolation, preserved SECONDARY authority,
  source-linked retrieval and tenant isolation. No model/key change, timeout
  extension, fabricated candidates or expert-import substitution was used.
  See `docs/validation/extraction-timeout-correction.md`; the earlier failed
  attempt remains historical evidence, not silently relabelled as a pass.

- [x] Complete browser visual/click checks of the industrial-demo preparation
  flow using standalone Chromium: real file downloads, source hashes, identity,
  T-Box/metadata prefill and desktop/narrow layouts passed. Isolated mocked
  queue responses additionally verified bounded matching and preserved edits.
  The construction/review/publication lifecycle is checked separately over
  authenticated HTTP, not claimed as browser-click evidence. See
  `docs/validation/industrial-demo-workbench.md`.

## Completed follow-ups

- [x] Recover once from a cold-start retrieval-store timeout during Playground
  warm-up, preserving the same read-only request, vector, ACL, limits and
  per-transaction deadline. No extra Embedding call or LLM retry is introduced;
  persistent failures still refuse startup.

- [x] Correct literal-object handling in the active A-Box inventory after a
  real mixed-publication smoke test exposed Cypher null-equality rejection.
  Valid literals require absent entity IDs and no OBJECT edge; forged IDs,
  empty-string substitutes and malformed entity objects still fail closed.
  See `docs/validation/inventory-literal-correction.md`.

- [x] Expose immutable published-graph quality audit history. The Playground
  offers explicit recording, publication-filtered history, and isolated
  historical details with original observer/time metadata. Live reads never
  persist automatically; request and identity guards prevent stale output.

- [x] Expose a bounded, ACL-complete active A-Box inventory in the Playground.
  It shows the exact publication/T-Box binding, trust and instance structure,
  relationship properties, and evidence locations without source text; an
  optional Document filter, immutable revision history, and stable-record
  publication-removal handoff make the active graph operationally inspectable.
  Executable UI checks cover request reordering, denied refresh, identity
  changes, invalid filters, and publication/rollback snapshot invalidation.

- [x] Separate immutable startup-fixture counters from runtime state in the
  Playground, show a fresh ACL-complete active Documents/Chunks summary for the
  selected JWT identity, and make readiness fail closed on missing/stale/multiple
  active generations or unavailable Neo4j vector indexes.

- [x] Make complete T-Box versions reusable from the Playground: load an exact
  checksum-bound definition, copy it to the next version for expert edits, or
  export a self-describing property-graph JSON artifact.

- [x] Add the independently authored and reviewed `semantic-holdout-v1` beyond
  the 49 builder-coupled questions. Its 14 balanced cases passed the recorded
  real-provider run with complete-evidence Recall@5 0.90, evidence-ID Recall@5
  0.8667, MRR 0.7583, and zero forbidden Chunk exposure.

- [x] Add a bounded active-publication graph-quality audit with complete ACL and
  exact T-Box binding, expose it through an independent `knowledge:quality`
  API/Playground card, and keep source text out of the response.

- [x] Expose governed active-document inventory and logical retirement through
  an independent `knowledge:lifecycle` API/Playground card. Retirement uses an
  active-snapshot/source-generation CAS plus a stable operation key, blocks on
  live knowledge or jobs, preserves immutable audit data, and refreshes the
  active vector generation before the local API returns.

- [x] Expose auditable active-publication record removal in the Playground,
  including removal-only change sets without directly deleting source data.

- [x] Add resumable knowledge-construction operations: bounded ACL-safe job
  status/list reads, immutable governed-record revision history, recoverable
  publication candidates, and Playground selection/retry controls with stable
  same-input operation keys.

- [x] Extend T-Box relationship-property definitions into evidence-backed,
  typed/unit/time-normalized extraction, authoritative import, review,
  publication, retrieval, API, and Playground instances. Publication also
  enforces relationship-property and closed-world endpoint cardinality.
