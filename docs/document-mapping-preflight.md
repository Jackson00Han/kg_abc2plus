# 文档接入与映射预检

> 本文保留来源接入阶段的范围与历史验收结果。普通 XML 现已接通逐片本体抽取及自动复核；当前行为和边界见[实例构建](instance-construction.md)。历史文件数量与任务状态不代表用户重置后的知识库。

本阶段打通原始文件接入、精确位置、声明式映射预检和来源审计。预检中的实体、属性及关系是可复核的构建建议，**不是平台 ABox 候选，不代表审核或发布完成**。

上传已增加[可选切片配置与只读预览](document-chunking.md)。下文 V11 32 / 84 个 Chunk 为最初保存的来源版本；新配置不会重切这些既有片段。

## 接入边界

- 通用解析入口为 `construction/parser.py`。现有 UTF-8 TXT、Markdown、JSON、CSV 继续支持，新增 `application/xml` / `text/xml`。Markdown 按标题边界切块；XML 保留原文，只执行既有 Unicode 与换行规范化。
- XML 解析器拒绝 DTD、声明实体、外部资源、多根、重复属性及不支持的编码，并限制字节、深度、节点、属性与文本数量。
- PDF 仍是独立 opt-in 解析插件，尚未接入普通文档上传 API；DOCX 暂未实现。增加格式需要实现解析插件及明确能力，不按扩展名承诺任意文件可用。
- 页面现已支持 XML 选择、格式识别、切片预览和来源保存，自动显示并固定为 `SOURCE_ONLY`，恢复其他文件格式时恢复用户之前的处理选项。XML 当前只允许 `SOURCE_ONLY`。带声明式映射的构建也只允许 `SOURCE_ONLY`，避免多位置结构证据被误套入 LLM 的单范围事实校验。

## 版本化 API

复用认证、当前租户、来源 ACL、本体启用状态、资源预算和去重机制：

1. `POST /v1/knowledge:preflight`：沿用 `KnowledgeConstructionRequest`，增加可选 `mapping_profile` JSON 对象；响应增加 `mapping_preflight`。
2. `POST /v1/knowledge:construct`：相同请求，设置 `extraction_mode: SOURCE_ONLY`。重新解析并校验映射，来源保存进入原有摄取流程。
3. `GET /v1/knowledge/construction-jobs/{job_id}`：首个 Chunk 的 `document_preflight` 返回来源、本体、映射校验和、预检数量、语义上下文及有界诊断摘要。任务列表及任务读取继续执行租户和 ACL 过滤。

请求不接受映射文件路径、代码或远程 URL。`mapping_profile` 上限 256 KiB、深度 32，适配器可以采用更小上限。完整报告上限 4 MiB，遇到超限明确失败。新增字段可省略，原有构建请求兼容。

映射预检入口 `construction/mapping_preflight.py` 按实际 MIME 和版本选择适配器，不依赖业务文件名：

| 映射版本 | 能力 | 当前范围 |
| --- | --- | --- |
| `xml-declarative-mapping:v1` | 有界路径选择、类型分支、复合身份、属性转换、祖先归属、显式引用连接、字段覆盖 | 组态资料；不推导实时状态 |
| `markdown-section-mapping:v1` | 标题、表格、单元格定位，章节类型与身份、表头映射、上下文保留、端点解析 | 明确章节、每个映射事实章节一张表一行数据 |

未知格式、未知映射版本、未处置的章节或字段、缺少定义的引用都应报告。自然语言文档仍可使用既有本体约束抽取；本阶段没有实现任意文档的自动映射生成。

## 来源和审计

完整原始字节、规范文本的校验和分别保留。新 XML 上传即使未附映射配置，也会在首个来源 Chunk 的不可变审计制品中保存原始文件 base64（包括 BOM 与原始换行）；带映射构建另保存映射全文和完整预检报告。各位置锚绑定实际文档版本、Chunk ID、Chunk 校验和、全局位置及 Chunk 内位置；跨 Chunk 的证据拆成覆盖完整原范围的引用，不伪造实体 mention。

映射、本体、解析器及报告参与构建指纹。同一操作键重试沿用现有幂等机制，变更映射不能覆盖旧操作。完整报告只保存在现有审计边界，任务接口返回摘要。预检请求可以重新获得完整报告；本阶段未新增工作台预检详情界面。

`knowledge_scope: AUTHORITATIVE` 现在也支持 `SOURCE_ONLY`，继续要求 `knowledge:import` 权限。此时仅登记用户声明，不创建带权威等级的实体或断言；审计保存 `declared_knowledge_scope` 和 `scope_declared_by`。声明使用独立的来源处理签名参与幂等身份，不能与业务来源或权威抽取操作混用。人工补充仍不能自报权威来源。模拟文件的原文标记保留；用户声明不能代替模拟边界的语义审核。

来源保存与知识发布是两个阶段。`SOURCE_ONLY` 完成后，授权管理员通过 `GET /v1/knowledge/documents` 及构建任务查看来源元数据和处理结果；`/v1/knowledge/sources:query`、`:read` 继续只读取当前正式知识发布包含的来源。尚无知识发布时，正式来源目录为空是正确状态，不应为展示新上传资料而放宽发布过滤。现有公共接口没有未发布 `SOURCE_ONLY` 全文浏览入口；精确原文及完整预检报告保留在受控存储和构建审计中。

`semantic_context` 记录内容层、是否模拟、工业验证状态和运行事件边界，独立于来源等级与平台审核状态。它保存在报告及来源构建审计中；本阶段未扩展 ABox 断言和发布读取契约，因此不能把这些建议当作已完成语义治理的知识入图。

## V11 参考配置与两源边界

参考配置位于 `busway_files/v11-reference/`；字段、设备类型、代码枚举和章节身份属于配置，不进入公共解析逻辑。配置按已授权的 `merged/mapping.yaml`、`merged/mapping.diagnostic.json` 和本体审校整理，不复制实例事实值。

只接入 `merged/sources/topology.xml` 和 `merged/knowledge/母线槽异常现象与可能原因知识说明-模拟业务资料.md`。不回读 baseline、不导入既有实例 JSON、不使用历史来源补齐缺项。

- XML 预检：139 个工程对象/端口/测点，171 条直接归属或显式引用关系。18 个空地址测点保留。64 个测点的 `OBSERVES` 缺独立通道语义依据，继续暂缓。组态不能证明实测数据或已发生故障。
- Markdown 预检：83 章完整处置，20 个模拟概念、49 条关系；37 条关系的端点在当前文档中可解析，另外 12 条引用 7 个缺少定义的外部 `obs:` 概念。保留引用，不创建占位节点。案例和背景章节保留上下文，不生成运行事件。

以上计数仅用于这组参考输入验收，不是通用契约的必需数量。

## 后续切片

1. 增加受版本管理的多位置证据契约，把属性值、对象锚、XML 祖先证明和 Markdown 上下文章节接入候选写入及审核读取；模拟边界贯穿断言、检索与发布校验。
2. 将当前预检中来源充分的工程对象、概念和关系转换为平台候选。缺失端点和点语义映射继续待处理，不凭名称自动合并跨文件实体。
3. 接通工作台映射诊断、覆盖情况与证据查看，再按真实需求扩展格式和自动映射提议。沿用已有审核和发布入口，不自动批准或发布。

## 验证

2026-09-17 实际验证结果：

- `parser_checks.py`：8 组检查通过；`check_markdown.py`：27 项通过；`check_xml_mapping.py`：13 组通过。
- `check_transport_store.py`：74 项通过。HTTP 认证/契约与 Neo4j 审计存取、任务 ACL、重放和冲突检测使用真实组件；完整工作流中的来源管线及映射返回使用确定性夹具。随机测试租户已清理，剩余节点为 0。
- 实际已启用 V11 本体下，两份业务文件的 `/v1/knowledge:preflight` 均成功，结果保存在 `.local/generic-ingestion/*-preflight-live.json`。无补造缺失概念。
- 实际模型调用恢复后，两份文件通过真实来源摄取管线完成 `SOURCE_ONLY`：拓扑 `32 / 32` Chunk，模拟知识文档 `84 / 84` Chunk。同一操作键重复执行返回原文档版本及任务，未产生重复版本；没有自动审核或 ABox 写入。
- `check_live_source_state.py`：真实认证 API 核验通过。管理接口可见 2 份来源文档、116 个 Chunk，两项任务均为 `COMPLETED`；正式来源目录及知识发布均为 0。另一知识库看不到这两份管理文档，读取这两个任务返回 404，正式来源读取仍不可用。
- `check_live_audit_readonly.py`：32 项只读检查通过。通过现有审计服务及相同租户/ACL 路径的有界 Neo4j 查询，核对 116 个 Chunk、审计制品、原始字节、规范全文与校验和；拓扑 2090 处证据锚、模拟知识 585 处证据锚重新绑定后与持久化报告一致。错误权限组、跨租户及缺失构建权限均拒绝读取；两份来源关联的 ABox 及知识发布为 0。没有把此项表述为公共全文 API 验收。
- 独立 Chromium 在 1280 / 1440 / 1920 宽度及全屏验收通过，并实际检查截图。两项构建任务完成，来源维护显示 2 份资料、116 个片段，文档版本、访问组、模拟标记及工程组态边界正确；正式来源目录和发布历史均为空。无页面脚本错误或水平溢出，未执行知识写入。当前没有发布版本，原文弹窗及版本比较不适用于此次数据状态。该次来源接入验收尚未包含 XML 上传 UI；后续 XML 页面修复见下节。映射预检详情界面仍未实现。
- `compileall`、`git diff --check` 通过；环境没有安装 Ruff，未宣称通过 Ruff 检查。

复验命令从项目根目录执行，均使用 `.local/` 临时检查，不恢复历史测试目录：

```sh
PYTHONPATH=src:.local/generic-ingestion .venv/bin/python .local/generic-ingestion/check_live_source_state.py
PYTHONPATH=src:.local/generic-ingestion .venv/bin/python .local/generic-ingestion/check_live_audit_readonly.py
node .local/browser-qa/document-mapping/live.cjs
```

这些命令验证当前已保存来源和页面，不重新摄取或调用模型；浏览器仅允许认证会话及明确的只读 POST 接口。报告为 `live-source-verification.json`、`live-audit-readonly-verification.json` 和 `.local/browser-qa/document-mapping/result.json`。

**真实来源导入已完成，知识尚未发布。** 使用用户新提供的业务空间密钥及对应北京域名，`qwen3.8-flash` 实际聊天调用成功，`text-embedding-v4` 实际返回 1024 维向量；OpenAI 兼容接口和 DashScope 原生接口均核验成功。平台继续使用兼容接口，嵌入模型、索引维度及向量空间没有切换。密钥只更新在私密本地配置中，未写入源码或报告。

此前该业务空间曾返回 `403 AccessDenied.Unpurchased`，通用端点曾返回 `400 Arrearage`，旧密钥曾返回 `401 invalid_api_key`。这些是历史响应，不能代表当前可用性；状态变化原因未由账户控制台独立确认。没有因先前错误跳过来源管线、伪造向量或取消发布边界。

免费额度按模型、有效期及账户状态管理；当前调用成功并不能证明本次调用全部由免费额度抵扣。实际余量与到期时间应在用户百炼控制台核对，见[阿里云免费额度说明](https://help.aliyun.com/zh/model-studio/new-free-quota)。未调整账户计费或“免费额度用完即停”设置。

当前完成的是本阶段来源接入和映射预检，下一步按上面的“后续切片”接通多位置证据和候选治理。实际结果摘要：`.local/generic-ingestion/implementation-result.json`、`live-construction-summary.json`、`live-source-verification.json`。测试及运行材料均在 `.local/`，未恢复历史测试目录或隔离语料。


## XML 上传页面修复（2026-09-17）

修复文件选择器遗漏 XML、XML MIME 别名不兼容及权威来源仅保存请求被拒的问题。普通 Markdown 等格式的抽取入口保留。XML 来源保存不调用抽取模型、不自动审核或发布，拓扑多位置证据与候选构建仍是后续工作。

临时验证命令：

```sh
PYTHONPATH=src .venv/bin/python .local/xml-upload/backend_checks.py
node .local/xml-upload/browser.cjs
```

真实 Neo4j + HTTP 检查 59 项通过（包含原切片回归）：两种 XML MIME、两种资料声明、原始字节、重试、权威声明权限、跨租户隔离、畸形 XML / DTD / 非 XML 拒绝、无候选和发布写入。使用独立随机租户及确定性嵌入，本次没有调用外部模型，测试节点和新增配置均清理至 0。报告：`.local/xml-upload/backend-results.json`。


独立 Chromium 的 14 项界面检查通过，覆盖 1280 / 1440 / 1920 宽度、滚动和全屏，实际截图已查看。8 次预检访问真实服务，包括 V11 topology 的 32 片及已有来源识别；提交请求在浏览器层拦截核对，未写入用户知识库。验证了 XML MIME 别名、错误文件提示、切换 Markdown/清空文件恢复抽取选项及用户原处理偏好。报告：`.local/browser-qa/xml-upload/result.json`。CUA 返回空浏览器列表后，使用独立 Playwright Chromium 完成验收。

服务经 `./quick_start.command` 重启，8002 可访问且 `pump-only` 保持开启。用户库只读复核仍为 2 份来源、116 个片段、0 次知识发布；本轮未重新导入业务资料。Python 编译、JavaScript 语法、`git diff --check` 及修改文件凭据模式扫描通过。
