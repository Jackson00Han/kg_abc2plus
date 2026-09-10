# 图谱探索：电脑端视觉验收（2026-09-10）

> 后续调整：范围与刷新已收进图谱工具栏，最新截图和验证见 [图谱内置范围与刷新验收](../graph-scope-2026-09-10/README.md)。本文件保留上一轮设计状态。

用户授权使用 Playwright 后，已连接独立的无头 Chromium，访问运行中的 `http://127.0.0.1:8002/industrial`。使用临时浏览器上下文，不连接、关闭或改写用户日常 Chrome 的标签页、账户和配置。此次只设计和验收电脑端。

## 设计判断与最终改动

删掉重复说明的方向成立，但证据入口不能随之消失。最终采用全宽图谱，以及选择后出现的下方详情；选中不会自动滚动，“查看详情”才主动定位并移动焦点。属性记录和内部标识默认折叠，来源与事实依据保留。

- 移除图谱导航的常驻绿点及侧栏重复库名，保留顶部知识库选择器。
- 移除页头的通用实体档案链接、工业知识图谱标签、发布版本号和授权局部视图说明；具体实体档案仍可从选中详情打开。
- 收紧宣传标题、统计区和筛选区，隐藏当前身份不可用的设备下拉框，移除每次启动的重复身份提示。真实错误提示仍显示。
- 用确定性的层级布局作为起点，保留网络布局切换；减少画布留白，提高当前四节点图的文字可读性。
- 选中后的操作条位于画布上方；“适应画布”与缩放工具放在一起，全屏按钮准确显示进入/退出状态。
- 本体模式隐藏重复的第三项类型统计，关系标签默认按需显示；点选关系显示该关系标签与约束，也可主动打开全部标签。实例模式默认显示关系标签。
- 将独立设计判断、实际浏览器验收和只做电脑端的要求写入根目录 `AGENTS.md`。

主桌面 1440×1000 的画布起点由约 592 px 提前到 379 px；当前实例图节点显示字号由约 6.5 px 提升至 13.65 px。

## 视觉与交互结果

| 状态 | 画布高度 | 当前实例图最小节点字号 | 结果 |
| --- | ---: | ---: | --- |
| 笔记本 1280×720 | 380 px | 12.49 px | 无横向溢出；选中后的详情操作在视口内 |
| 桌面 1440×1000 | 520 px | 13.65 px | 默认、选中、详情、恢复与视图切换通过 |
| 宽屏 1920×1080 | 561.59 px | 13.65 px | 默认、选中、详情通过 |
| 全屏 1440×1000，详情展开 | 433.63 px | 13.65 px | 所有当前实例节点位于画布内，详情独立滚动 |

逐步操作使用真实浏览器的输入、按钮点击和画布坐标点击。覆盖名称搜索、节点/关系选择、关系标签开关、两种布局切换、适应画布、详情定位与折叠、恢复全图、实例/本体切换、全屏进入退出、全屏搜索聚焦及跳转实体档案。来源详情截图等待加载完成。浏览器未捕获未处理的页面异常。

已直接查看渲染截图进行视觉复核，不能仅凭测试输出判断布局。主要截图：

- [调整前桌面](00-before-desktop.png) / [调整后桌面](01-desktop.png)
- [选中状态](02-desktop-selected.png) / [来源详情](03-desktop-details.png)
- [本体模型](04-ontology.png) / [本体关系选中](04-ontology-selected.png)
- [全屏及详情](05-fullscreen-selected.png)
- [笔记本](06-laptop.png) / [笔记本详情](08-laptop-details.png)
- [宽屏](06-large-desktop.png) / [宽屏详情](08-large-desktop-details.png)
- [测量结果](results.json)

## 检查中发现并修复的问题

1. 来源详情调用 `sourceTag` 却未导入，真实浏览器出现运行时错误；已补齐导入并以来源加载流程复验。
2. 全屏展开详情会缩小画布并裁切节点；已在所属全屏画布尺寸稳定后重新适应。普通页面尺寸变化不重置手动视野。
3. 全屏搜索的一跳聚焦被随后的全图适应覆盖；重排现在保留用户请求的聚焦范围。
4. 全屏打开实体档案后，全屏状态仍指向隐藏的图谱；现在先退出图谱全屏再导航。
5. 图谱加载失败时残留上一筛选的计数、图例和耗时；加载开始即清空旧展示。
6. 本体图导出被实例数量错误阻止；改为判断实际画布节点。
7. 本体类型统计重复，跨级连线标签拥挤；减少重复统计并采用按需标签。

## 后续问题与验收边界

- 本体的部分跨级连线仍靠近中间节点。默认按需标签已减轻文字冲突，复杂图的边路由仍可继续优化；此次不引入自行设计的绕线路由算法。
- 当前资料的部分标题仍是 `authoritative_source` 等技术性名称。界面保留真实来源标题，没有编造中文文档名；资料命名可另行整理。
- 验收依据仅为允许的循环水泵知识：当前实例图 4 个节点、3 条聚合关系。更多节点时的可读性和性能不由本结果保证。
- 无头 Chromium 的系统级 Escape 快捷键未能完整模拟；已验证退出按钮，以及调用 Fullscreen API 后按钮与布局同步。没有声称验收了操作系统的原生快捷键行为。
- 浏览器读取了当前运行服务的图谱和证据；未运行独立的真实 Neo4j 集成套件、未调用外部模型、未上传或发布数据。本次没有后端查询、权限或持久化改动，不代表生产验收。

## 可复现检查

本机已存在的 Playwright 位于临时工具目录，不是项目运行依赖。其他环境可提供自己的 Playwright 安装路径；脚本要求可用的 Chromium。

```sh
NODE_PATH=/tmp/graphrag-industrial-browser-qa/node_modules PLAYGROUND_PUMP_ONLY=1 node scripts/verify_graph_visual.cjs
PLAYGROUND_PUMP_ONLY=1 uv run python -m unittest tests.unit.test_graph_result_state tests.unit.test_graph_viewport_controls tests.unit.test_graph_details_action tests.unit.test_graph_exploration_layout tests.unit.test_pump_graph_views tests.unit.test_pump_isolation tests.unit.test_industrial_web.IndustrialStaticRouteTests
node --check src/graphrag_prod/playground/static/industrial/app.mjs
node --check src/graphrag_prod/playground/static/industrial/graph.mjs
node --check scripts/verify_graph_visual.cjs
git diff --check
```

25 项相关离线测试通过，桌面浏览器流程通过，语法与差异检查通过。保留已有工作区改动，未提交、推送或重启服务；页面刷新即可加载静态资源修改。
