# 母线槽输入与映射

`busway_files/` 保留以下三份 V11 输入。2026-09-22 同步并按 SHA-256 核对来源。
本体和知识说明保持原始字节，知识说明仅调整文件名；拓扑为用于 GitHub 分享的脱敏副本，
仅将 `ProjectBasePath`、`ProjectPath` 中的个人本机路径替换为中性占位符。
原始拓扑完整保存在 VPS 的 Git 忽略目录 `.local/private-inputs/busway-v11/topology.xml`，
不随仓库提交。脱敏副本与原文是不同的来源版本，不能共用校验和或原文位置证据。
V11 是资料包版本，本体内部 `metadata.version` 为 `0.1.1`，不要与已删除旧路线的
`4.0.0` / `5.0.0` 按数值比较新旧。

## 文件与来源

| 当前路径 | V11 包内原始路径 | 用途 |
| --- | --- | --- |
| [`busway_files/ontology.yaml`](../busway_files/ontology.yaml) | `input/diagnostic-package/merged/ontology.yaml` | 本体定义，16 类实体、15 类关系 |
| [`busway_files/topology.xml`](../busway_files/topology.xml) | `input/diagnostic-package/merged/sources/topology.xml` | 工程拓扑、设备、端口和组态测点（项目路径已脱敏） |
| [`busway_files/busway.knowledge.md`](../busway_files/busway.knowledge.md) | `input/diagnostic-package/merged/knowledge/母线槽异常现象与可能原因知识说明-模拟业务资料.md` | 异常现象与可能原因的模拟业务知识 |

上述类型数量是本包说明，不是平台公共约束。仓库内三份文件的 SHA-256：

```text
615ea9c25a0ad0224a911617650e72a2435f41b8f41c9b0318798a518afb0395  busway_files/ontology.yaml
f969ee85e2b90072e7e9be487d9edaf6beef416eb605615198459c3f5c541287  busway_files/topology.xml
89481f1a4597a9b2f94638783a9e5bc94765885119ea339f79a4e5be28541967  busway_files/busway.knowledge.md
```

原始拓扑的 SHA-256 为 `26ab661ceafa7ed4745cdb6241a69ae407acf23d9a4c6246db32c840e3610a9a`。
脱敏只涉及两个项目路径字段，其余 XML 内容与原文一致；占位符不是实际工程路径。

## 导入配置与操作

两份配置按原始字节从旧 `v11-reference` 目录移至契约目录；它们用于解释来源结构，
不是额外业务语料，不包含预先构建的实例库：

- [`contracts/mappings/busway-v11/topology-mapping.json`](../contracts/mappings/busway-v11/topology-mapping.json)：`xml-declarative-mapping:v1`。
- [`contracts/mappings/busway-v11/knowledge-mapping.json`](../contracts/mappings/busway-v11/knowledge-mapping.json)：`markdown-section-mapping:v1`。

1. 在本体入口选择 `ontology.yaml`，查看诊断，再按需要保存并启用本体。
2. 在实例构建入口上传 `topology.xml`，显式选择对应 XML 映射；上传知识 Markdown 时选择对应 Markdown 映射。
3. 检查预览、缺项、来源声明与访问权限，再决定正式构建。映射不会根据文件名自动采用。
4. 构建产生候选后，沿用身份确认、证据复核、发布预览和发布流程；上传成功不等于知识已发布。

映射提取与确定性重放本身不调用 LLM；文档向量化、普通模型抽取和后续检索仍有模型依赖。
构建流程和开发约束见 [架构与开发说明](architecture.md)。

## 已知范围与缺口

本体结构校验及两份映射预检在 VPS 通过。预检均有效但不完整：

- XML 可识别 139 个工程对象、171 条关系声明；测点到观测量的语义绑定仍待权威依据。
- Markdown 可识别 20 个本地对象、49 条关系声明；其中涉及 7 个外部观测量的 12 条关系不能补造端点。
- 端口角色数值枚举转换仍受字面量证明约束；51 项角色属性不能冒充原文直接给出的目标枚举。
- 64 个组态测点不能当作实时读数；缺少通道依据时不生成 `OBSERVES` 绑定。
- 模拟异常、候选原因和应有测点不能当作已发生故障或已验证根因。

上述为原文预检与已有构建约束，不是 VPS 的已导入或已发布统计。
本次没有执行知识库写入、真实模型调用或完整构建验收。

水泵示例继续完整保留在 `src/graphrag_prod/playground/static/industrial-demo-v1/` 和
`src/graphrag_prod/playground/static/demo-mini-zh-v1/`。旧 JSON 本体/拓扑、规则引用目录、
模板、重复原始包及旧实例 YAML 已清理，不作为新任务的输入。
