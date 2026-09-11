# 属性审核中的实体归属更改

范围：只增加属性审核的当前归属展示、选择和保存；沿用现有自动匹配结果，不调整抽取算法或其他页面。

- 属性行直接显示当前实体及其可见身份属性，例如循环水泵 / EquipmentCode；点击“更改归属”后默认选中当前实体，可搜索名称、编号或应用实体 ID，并查看所选实体的来源依据。
- 可选目标限定为当前租户、访问组和本体下已确认或当前发布的同类型实体。最多返回 20 项，超过上限提示缩小搜索。身份编号属性本身不能归入已有不同编号的实体。
- 保存要求填写审核依据并携带属性版本、目标实体 ID 和目标提及版本。事务再次校验来源、目标和访问权限；过期请求失败，不静默覆盖。
- 更改仅追加当前属性的修订。它保留原文、文档版本、Chunk、位置、原始数值、单位、来源等级和形成方式，进入暂缓区重新检查，再由用户确认事实。
- 为保持事实和关联提及属于同一来源 Chunk 的约束，保存时建立本属性专用的来源提及，保留其形成和确认修订，并用 `assignment_source_revision_id` 记录所依据的属性修订。其导航标识按人工归属记录区分，避免相同原文范围与原有提及碰撞；原始抽取版本保持不变。其他属性、原来源提及及目标实体保持原状。审核记录保存前后实体 ID、源和目标版本、审核人、时间及理由。
- 本次不新增实体建档流程。没有合适目标时，可取消更改并沿用现有暂缓操作。

## 可复现检查

仅使用 `industrial-demo-v1` 的循环水泵材料，确定性离线构造错误归属，不调用外部模型。Neo4j 使用临时测试容器，与 8002 工作台数据库分离。

```sh
.venv/bin/python -m unittest tests.unit.test_playground_review_flow tests.unit.test_playground_resolution tests.unit.test_playground_property_assignment tests.unit.test_playground_identity_decisions tests.unit.test_api_backend tests.unit.test_api_runtime tests.e2e.test_property_assignment_api -q
PLAYGROUND_PUMP_ONLY=1 scripts/run_stage8_neo4j_tests.sh /tmp/property-assignment-suite.json /tmp/property-assignment-observations test_property_assignment_neo4j.py
node --check src/graphrag_prod/playground/static/industrial/governance.mjs
git diff --check
```

## 实际结果（2026-09-11）

- JavaScript 执行测试、HTTP 权限/请求校验与相关既有单元回归通过；包括默认选择、同名不同编号、填写依据、单条保存、取消、错误后保留输入、异步结果过期和未保存时禁止确认。
- 真实 Neo4j 集成检查 3 项通过（38.315 秒，0 跳过）：同名编号展示与搜索、已确认但错误的归属纠正、来源与修订保留、兄弟属性不迁移、重新确认与成功发布、重复/过期请求、编号冲突、跨租户和访问组隔离、不可见及失效来源目标拒绝。修正了测试中暴露的人工归属提及与原始提及导航标识碰撞。
- 数据模型、持久化序列化及审核逻辑的相关单元回归 44 项通过。测试容器已经移除。
- 浏览器视觉验收未完成：CUA 的浏览器清单为空；尝试创建 in-app browser 和 Chrome 页面均返回 `Browser is not available`。当前无法验证笔记本、宽屏和全屏的实际渲染，代码与 HTTP 检查不替代视觉验收。
- 当前 8002 本地工作台已重启并保留现有数据；HTTP 检查确认 `pump-only`、新静态资源与磁盘一致、新接口未登录返回 401。
- 未进行外部模型验证、生产容量验证或生产部署。
