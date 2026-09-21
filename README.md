# Neo4j GraphRAG 知识工作台

当前项目提供本体管理、资料上传、知识构建、审核发布、图谱浏览和证据检索。
业务资料只保留母线槽知识包与循环水泵示例。来源、不可变文档版本、Chunk 和
受治理的图谱保持可追溯；模型抽取结果经审核与发布后才成为可用知识。

## 启动

需要 Python 3.12+、uv、Docker、现有 Neo4j 数据库和模型服务配置。

```sh
uv sync --locked
./quick_start.command
```

也可在 macOS 双击 `quick_start.command`。页面地址：
<http://127.0.0.1:8002/industrial>。

配置步骤、数据库恢复和日志位置见 [快速启动说明](quick_start.md)。入口复用现有
数据库并保持 `PLAYGROUND_PUMP_ONLY=1`；不会创建临时库或导入演示语料。
该兼容配置名表示当前隔离范围，允许使用母线槽和水泵知识库。

## 目录

| 路径 | 用途 |
| --- | --- |
| `src/graphrag_prod/` | API、本体、构建、知识治理、图谱、检索、生成及工作台代码 |
| `scripts/restart_workbench.py` | 当前 8002 服务的启动与重启 |
| `scripts/run_playground.py` | 服务装配和运行，不再依赖测试夹具 |
| `contracts/profiles/workbench-construction.v2.json` | 文档构建的资源与超时上限 |
| `new_files/` | 用户提供的母线槽原始资料与映射配置 |
| `busway_files/` | 面向工作台整理的母线槽本体、拓扑、知识说明和规则引用目录 |
| `src/graphrag_prod/playground/static/industrial-demo-v1/` | 可从页面使用的循环水泵知识构建示例 |
| `src/graphrag_prod/playground/static/demo-mini-zh-v1/` | 水泵示例及启动目录所用的元数据，不自动导入数据库 |
| `docs/` | 当前代码结构说明、平台边界与架构图 |

`.env`、`.env.workbench.local`、`.local/workbench/` 和 `.venv/` 是本地配置与
运行环境，不提交到 Git。知识库实际内容保存在现有 Neo4j 中，源码不替代数据库备份。

## 使用与维护

- [本体 YAML 上传、校验与规范化](docs/ontology-upload.md)
- [实例上传、自动复核与发布验收](docs/instance-construction.md)
- [母线槽材料使用指南](busway_files/README.md)
- [循环水泵示例说明](src/graphrag_prod/playground/static/industrial-demo-v1/README.md)
- [当前代码结构与运行依赖](docs/runtime-architecture.md)
- [平台目标架构与知识库职责](docs/platform-architecture.md)
- [项目开发约定](AGENTS.md)

`datasets/`、`tests/`、离线 `evaluation/`、历史验证报告、阶段 CI 和旧语料加载器
已移除；不再保留金融、法律等测试内容，也不再提供旧演示库启动与全库重置入口。
现有知识库的项目管理、受控重置、审核与发布功能仍由工作台提供。
