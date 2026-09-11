# 浏览器验收恢复：2026-09-11

## 原因与处理

CUA 返回空的浏览器列表，连接 Chrome/iab 失败，仅代表 CUA 通道不可用。项目在 `page-style-consistency-2026-09-10.md` 已记录经用户授权的备用方案：用 Playwright 独立启动真实 Chromium。此次未及时复用该方案，是流程遗漏。

旧 `/tmp/graphrag-industrial-browser-qa/node_modules` 目录仍存在，但 Playwright 包不完整；即使设置该目录为 `NODE_PATH`，仍出现 `MODULE_NOT_FOUND`。本机 Chromium 缓存完好。已将固定版本 Playwright 1.63.0 安装在 `$HOME/.local/share/graphrag-browser-qa/playwright-1.63.0/`，复用现有 Chromium 153.0.8010.12，实际启动成功。没有修改应用依赖或接管用户 Chrome 配置。CUA 通道本身没有恢复，独立 Chromium 已可完成浏览器验收。

## 固定入口

```sh
# 检查依赖、真实启动浏览器、打开 pump-only 工作台并截图
sh scripts/run_browser_qa.sh --check

# 至少存在两个可访问发布版本时，检查历史比较和弹窗滚动/关闭
sh scripts/run_browser_qa.sh --check --publication

# 使用同一工具环境运行项目已有专项脚本；先核对该脚本的范围
sh scripts/run_browser_qa.sh verify_construction_visual.cjs
```

入口自动安装缺失的固定版本模块；浏览器启动失败时使用官方安装器安装 Chromium，再实际启动一次。模块检查及启动使用工具目录的绝对路径，执行专项脚本前检查模块解析是否一致，避免静默混用应用目录或旧 `NODE_PATH` 中的版本。

`BROWSER_QA_TOOL_DIR` 可指定独立工具目录，`BROWSER_QA_OUTPUT` 可指定连接检查的截图/报告目录，`BROWSER_QA_URL` 可指定本地服务地址。默认地址为 8002；连接检查要求 bootstrap 明确声明 `pump-only`，只允许本地、列明的读取接口及建立测试会话。知识写入请求会被拦截并导致检查失败。专项脚本的范围和输出配置以各自实现为准。

安装模块与下载浏览器是两个步骤，浏览器缓存路径也独立于 npm 包目录，参见 [Playwright 官方浏览器安装文档](https://playwright.dev/docs/browsers)。若安装后仍无法启动，保留具体启动错误，按错误处理操作系统依赖或用对应工具目录内的 `node node_modules/playwright/cli.js install --force chromium` 修复浏览器缓存，再运行入口验证。不能只根据目录存在就判定环境正常。

## 实测结果

- `sh -n scripts/run_browser_qa.sh`、`node --check scripts/verify_browser_connection.cjs`：通过。
- 从仓库外调用入口，指定全新的空工具目录，并继承已有 Playwright 的 `NODE_PATH`：仍正确向指定目录安装 2 个包，随后启动 Chromium、打开工作台，检查通过。这验证了依赖丢失后的自动恢复，而不只是已安装环境的启动。
- 默认连接检查：真实工作台载入成功，认证后的图谱查询返回 200；1280×800、1440×1000、1920×1080 截图均已实际查看。
- 历史比较检查：三个尺寸均打开真实发布历史、选择第 2 版、调用真实比较接口、显示文档范围与实体/关系/属性范围，并成功关闭弹窗；窄屏弹窗底部可滚动到确认和关闭按钮，已查看滚动后截图。
- 两次检查报告均为 `passed`，无页面 JavaScript 异常、HTTP 错误或白名单外请求。当前发布版本没有切换，没有上传、抽取、审核决定、删除或来源撤回。
- 默认工作台截图位于 `.local/browser-qa/connection/`；比较及滚动后截图位于 `.local/browser-qa/recovery/`；空目录恢复报告位于 `.local/browser-qa/install-recovery/`。每个目录保存 `results.json`，上述本地生成产物已加入忽略规则。
- `git diff --check` 通过；新增脚本和恢复文档未发现私钥、Bearer 令牌或 API 密钥。

本次解决浏览器验收通道和版本比较的只读操作验证。没有通过浏览器执行实际历史切换、属性归属保存、外部模型调用或全屏图谱回归；这些不能由连接检查结果代替。版本比较的标题下仍保留“正在比较”文案，已记录为现有状态提示问题；本次浏览器恢复不修改产品逻辑。
