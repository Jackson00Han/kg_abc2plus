# 本体文件误选反馈验证（2026-09-13）

用户先选择水泵本体，再把 `topology.source.json` 放进本体入口，页面只显示通用参数错误。拓扑是实例构建的业务来源，不符合本体定义合同；已有水泵版本并非这次参数错误的原因。

本轮仅修改前端输入检查、提示及相关测试。按原生本体、版本化 source 合同和标准导出文件的内容结构识别，不按文件名或设备类型识别。业务 JSON 明确指向「实例构建 → 上传资料」；已识别但缺字段的本体显示本体格式问题。源内容、规则引用和校验码不被自动改写，服务端原有验证继续生效。

选择文件与直接编辑均更新固定位置的错误说明及保存按钮。错误文件不能继续提交先前水泵模板；重新选择或修正为有效本体后恢复操作。连续选择、手动修正和身份切换会使旧文件读取失效。初始化本体列表的迟到响应也不能覆盖用户已经选择或编辑的内容。重新选文件后编辑器回到正文开头。

## 验证

```sh
.venv/bin/python -m unittest tests.unit.test_ontology_source_ui tests.unit.test_playground_flow tests.unit.test_playground_reset_ui tests.unit.test_playground_demo_ui tests.unit.test_playground -q
sh scripts/run_browser_qa.sh verify_ontology_file_feedback.cjs
node --check src/graphrag_prod/playground/static/industrial/governance.mjs
node --check src/graphrag_prod/playground/static/industrial/governance-template.mjs
git diff --check
```

相关离线回归 73 项通过（5.677 秒）。覆盖实际本体与拓扑文件、改文件名不能改变类型判定、导出重放、旧规则引用清理、格式错误与手动修正、文件读取竞态及身份清理；原生类型与约束的完整校验仍由服务端负责。

随后增加初始化迟到响应的独立回归，本体输入测试文件最终 7 / 7 通过（0.612 秒，包含前述已通过的 6 项）。新增用例覆盖文件选择、有效手动修正、错误输入及无操作默认填入；在内存中还原旧覆盖逻辑后，该用例按预期失败。没有为此重复执行无关检查。

沿用已确认的独立 Chromium 连接，真实 8002 页面依次选择水泵文件、拓扑文件、母线槽本体，验证误选、纠正、直接粘贴及恢复模板。覆盖 1280、1440、1920 桌面宽度和滚动；实际查看全部截图，无横向溢出、脚本异常或 HTTP 错误。文件选择只改变独立临时浏览器中的编辑器，没有点击保存或提交业务请求。

浏览器脚本禁止本体导入、构建、审核、发布及重置等写入，允许服务端签发会话及已有只读接口。本轮没有替用户导入、启用、审核、发布、删除或重置任何内容；前后读取的知识库代次、五项数据计数和本体版本完整响应一致。没有重启 8002，没有调用外部抽取模型。

本地截图和结果在 `.local/browser-qa/ontology-file-feedback/`，不应提交。上述检查只证明本轮输入反馈与编辑器行为，不能当作重新执行了本体发布或知识抽取验收。
