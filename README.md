# Neo4j GraphRAG Workbench

当前项目是基于 Neo4j 的知识构建、审核发布、图谱浏览和证据检索工作台。
应用代码位于 `src/graphrag_prod/`，通过版本化文档、精确 Chunk 坐标、
受治理的实体和关系，将检索结果追溯到原文。

默认中文演示使用 4 份文档、6 个 Chunk。独立工业工作台支持 Canalis KT
和 EvoPacT HVX 的代表性语料、图谱探索与受控上传。模型抽取产生待审核候选，
人工审核和发布后才能用于相应的已发布图谱。

## 本地启动

需要 Python 3.12+、uv、Docker，以及已配置的 OpenAI-compatible embedding
和 chat provider。默认启动器使用真实 provider，并将 Neo4j 限制为 1 CPU / 1.5 GiB。

```sh
uv sync --locked
# 尚未创建 .env 时复制；已有配置不要覆盖。
cp -n .env.example .env
# 按下方运行说明配置 provider、模型、向量维度和凭据。
./scripts/run_playground.sh
```

默认入口：<http://127.0.0.1:8000/playground>。启动器创建独立临时 Neo4j，
退出时移除自己的临时数据库。运行中的工业库与该演示分开管理。

- [中文最小演示、数据库隔离与重置](docs/playground_mini_demo.md)
- [运行配置、provider 限制和故障处理](docs/local_playground.md)
- [知识构建流程与来源等级](docs/knowledge_construction_workflow.md)
- [实体档案、关系图谱、来源资料与质量维护](docs/knowledge_browsing.md)
- [知识构建操作指南](docs/industrial_demo_walkthrough.md)
- [工业工作台使用指南](docs/industrial_workbench.md)
- [复用已有工业数据库与上传恢复](docs/industrial_runtime_and_uploads.md)

默认启动器校验官方 DashScope HTTPS endpoint，`.env.example` 与此保持一致。
请填写自己的密钥并确认模型权限；API 核心通过依赖注入接入 provider。
Playground 的检索页面返回 Chunk、出处、子图和检索轨迹；最终回答生成未在该页面启用。
构建页面按需调用 chat model 抽取候选。

知识管理分为“建立专家基准、扩充业务知识、知识浏览、质量与维护”。知识浏览
提供实体档案、可探索的关系图谱和原文资料；维护提供问题核查、知识修正、
移除影响预览以及发布版本比较。原文按需授权读取，修正经审核和发布后生效。

## 项目结构

| 目录 | 当前用途 |
| --- | --- |
| `src/graphrag_prod/` | 领域模型、摄取、知识治理、检索、生成、API、工作台及评估实现 |
| `scripts/` | 启动、语料构建、导入、专项检查和自动化验证入口 |
| `tests/` | 单元、真实 Neo4j 集成、HTTP E2E、安全及回归测试和固定夹具 |
| `contracts/` | 验收、治理、工业领域契约与工作负载上限 |
| `datasets/` | 可复现的开发、工业及生产参考验证语料 |
| `evaluation/` | Gold 标注、冻结预测、质量门槛及已审核基线 |
| `docs/` | 当前设计、操作说明及历史验证证据 |

默认应用只加载最小演示语料。其他语料仍被集成测试、工业导入或质量评估使用，
并非默认发送给模型的内容。其隔离范围由
[语料隔离清单](docs/playground-demo-isolation.v1.json)和自动化测试核对。

## 验证

不需要 provider 或数据库的检查：

```sh
uv run --locked python -m unittest discover -s tests/unit -t . -q
uv run --locked python -m unittest discover -s tests/e2e -t . -q
uv run --locked python -m unittest discover -s tests/security -t . -q
uv run --locked python -m unittest discover -s tests/regression -t . -q
uv run --locked python scripts/build_dev_corpus.py --check
uv run --locked python scripts/build_evaluation_gold.py --check
uv run --locked python scripts/validate_acceptance_contract.py
```

完整开发回归需要 Docker，使用临时 Neo4j，并生成两轮可比较报告：

```sh
./scripts/run_stage8_validation.sh \
  --repeat 2 \
  --baseline evaluation/baselines/dev-mini.v1.json \
  --output-dir /tmp/sample-graphrag-stage8
```

详见[自动评估说明](docs/automated_evaluation.md)。`dev-mini` 缩小规模、时长和
重复次数，保留身份、权限、出处、正确性及生命周期检查；结果不代表生产规模验证。
`run_stage2`–`run_stage7` 是仍可执行的阶段专项 Neo4j 检查；工业评估中的
`industrial_legacy_retrieval_worker.py` 用于独立基线比较，仍是评估依赖。

生产参考验证从干净的已提交版本运行，使用新的仓库外输出目录：

```sh
./scripts/run_stage9_validation.sh \
  --output-dir /tmp/sample-graphrag-stage9
```

该流程包括 24,000-Chunk 语料、持续负载和备份恢复，详见
[生产候选验证说明](docs/production_candidate_validation.md)。历史 Stage 9 资格绑定
`7142fa331f74ecd868a5ba20d343c787e2f9d367`，不自动覆盖后续改动，也不等同于上线许可。
现有证据和限制见 [Stage 9 报告](docs/validation/stage-9.md)。

## 设计与维护

- [来源与数据模型](docs/provenance_model.md)、[增量摄取](docs/incremental_ingestion.md)
- [知识治理](docs/industrial_knowledge_governance.md)、[治理 API](docs/industrial_knowledge_api.md)
- [检索引擎](docs/production_retrieval.md)、[有依据的回答生成](docs/grounded_answer_generation.md)
- [API、安全与可靠性](docs/api_security_reliability.md)、[图谱浏览](docs/graph_browsing.md)
- [抽取质量门槛](docs/knowledge_extraction_quality_gates.md)、[验收契约](docs/acceptance_contract.md)
- [未完成事项](to_do_list.md)、[开发规则与阶段记录](AGENTS.md)

2026-09-08 按维护范围清理：移除独立编号教程及其配置、Apple 样例和流程图；
删除仅供教程使用的 `neo4j-graphrag` 依赖，直接声明当前 provider 使用的 OpenAI SDK。
过时展示报告和零散修复记录已删除或合并，保留关键约束与验证证据。

本次清理检查通过：1,007 unit、16 HTTP E2E、54 security、2 regression；
开发语料与 Gold 确定性重建、验收契约、Python/shell 语法、文档链接、依赖锁定、
差异格式及新增内容凭据模式扫描通过。现存依赖版本未升级。
未重跑真实 Neo4j 集成、外部 provider 或 Stage 9 负载验证；历史资格不变。
