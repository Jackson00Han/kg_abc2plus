# 母线槽知识库人工检索验收题集

版本：`busway-retrieval-cases-v1 / 1.0.0`；日期：2026-09-13；共 24 题。**本题集和同目录 JSON 仅供验收，不能上传知识库。** 来源事实核对与页面检索验收是两项不同工作；以下均为预期证据，不是实际页面结果。默认按证据检索页面验收，不要求尚未启用的回答生成能力。

## 使用前提

先启用母线槽本体，再完成 `topology.source.json` 和 `busway.knowledge.md` 的抽取、人工复核与发布。记录实际选中的知识发布版本、文档版本和本体版本；这些文件“上传成功”不等于全部知识已发布。验收前核对文件版本是否与本题集对应。

本题集的领域知识证据应来自上述两份发布文档；不要求将原始诊断 YAML、规则文件、映射配置或本题集上传。已知原始 YAML 与规则的差异以知识说明 K23 所记录的版本比较为准。

每题新建一次查询，直接粘贴“问题”，不要粘贴参考要点。使用页面实际可选的检索/问答模式，在当前母线槽工程和知识发布范围内测试。不得靠上一题回答补证据。

若页面只显示检索片段，则验收“关键证据是否召回、引用能否打开、是否属于正确文档版本”；只有页面确实生成答案时，再验收答案要点和禁止结论。页面没有的生成能力不能记为已通过。

## 判定标准

- **通过**：该题关键证据片段全部召回，引用可打开并定位当前文档原文；若结果包含事实断言或生成答案，还须没有禁止结论。允许同义表达，不要求逐字匹配。
- **部分通过**：证据不完整（若开启生成，也包含答案不完整），但没有错误断言；记录遗漏的要点，不按通过计。
- **失败**：事实/对象/单位/范围错误，引用不支持，或出现任一禁止结论。
- **资料未就绪**：依赖文件尚未完整发布、选错发布版本或权限/服务阻止检索；先修正前提后重测，不判题集已通过。

否定题应召回“数据不足/能力边界/未绑定”的原文片段；仅检索页面不要求输出拒答句子，也不能据此宣称自动拒答行为已经通过。建议记录：题号、查询时间、发布版本、页面模式、召回片段（若有生成再记录答案）、引用文档/Chunk、结论、问题说明。24 题全部逐题检查；尤其 BW-20～BW-24 的无依据诊断、生产阈值、概率、跨领域套用或虚构权威来源，出现一次即记对应题失败。

知识说明版本 SHA-256：`f4560bcef4ef65f46c7f2209867418441e3281ad206571c26179e7eadf590148`。全部原始来源校验和及结构化证据要点见 [同名 JSON](busway-retrieval-cases.json)。

离线来源交叉核对命令：`python3 scripts/verify_busway_knowledge_materials.py --report busway_files/acceptance/material-validation.json`（Python 需可导入 PyYAML；不修改应用依赖）。该命令检查源文件版本、XML/JSON 的全部资产/端口/64 个测点、测点语义与单位、候选关系及证据定位；不执行页面检索。实际离线结果见 [来源核对报告](material-validation.json)。

## 验收问题

### BW-01

**问题：** 工程 PBMBaseProj_2512 有哪些母线槽和分区？工程对象、构件和逻辑测点分别有多少？

**应召回的证据要点：**

- BUS1 → BUS1.S1；BUS2 → BUS2.S1。
- 24 个工程对象包含 19 个构件；构件为 9 个接头、7 个直身段、1 个法兰、1 个插接箱、1 个始端单元。
- 64 个逻辑测点；数量仅针对当前来源清单。

**不得出现的错误事实断言（若有）：** 把 24 当作构件数，或把 64 说成实际采样数；把本体允许的三通等类型当作当前工程已存在的构件。

**预期知识证据：** `busway.knowledge.md` K02（行 17–26，工程、母线槽与分区）；`busway.knowledge.md` K07（行 69–87，逻辑测点实际覆盖）。

**原始核对位置：** T:/Project/NodeTree/Root/Children；全部 Child 及 Variables/Variable。

**实际结果：** 待页面验收。

### BW-02

**问题：** BUS1.S1.JPK1 和 BUS2.S1.JPK1 是不是同一个接头？应该怎样引用它们？

**应召回的证据要点：**

- 不是同一接头；同名 Code=JPK1 不构成同一身份。
- 分别使用 PBMBaseProj_2512/BUS1.S1.JPK1 和 PBMBaseProj_2512/BUS2.S1.JPK1。

**不得出现的错误事实断言（若有）：** 仅按 JPK1 合并，或使用 Neo4j 内部 ID 作为持久业务引用。

**预期知识证据：** `busway.knowledge.md` K03（行 27–36，对象身份与连接语义）。

**原始核对位置：** M:materialization.topology_xml.identity_schemes.asset_ref；T:两个 FullCode 对应 Child。

**实际结果：** 待页面验收。

### BW-03

**问题：** BUS1.S1.PIU4 属于哪里，又通过哪个端口连接到母线槽？能据此证明现在有分流吗？

**应召回的证据要点：**

- PIU4 是 BUS1.S1 分区内的插接箱。
- BUS1.S1.ST2 的附件端口 3（Attach1）以 Load 指向 BUS1.S1.PIU4。
- 静态插接结构不能证明当前实际分流，需要外部运行证据。

**不得出现的错误事实断言（若有）：** 说 PIU4 属于 JPK3，或主链路串行经过 PIU4；臆造 PIU4 的对端端口号、支路电流或当前工作状态。

**预期知识证据：** `busway.knowledge.md` K04（行 37–46，BUS1 端口链路与插接关系）。

**原始核对位置：** T:Child[@FullCode='BUS1.S1.ST2']/Ports/Port[@Ordinal='3']/@Load；BUS1.S1/Children；M:materialization.topology_xml.runtime_branch_state。

**实际结果：** 待页面验收。

### BW-04

**问题：** BUS2.S1.JPK4 的前后连接对象是什么？它的算法路由已经通过生产验证了吗？

**应召回的证据要点：**

- JPK4 输入端口 1 的 Load 为 BUS2.S1.ST3，输出端口 2 的 Load 为 BUS2.S1.ST4。
- AlgorithmType=StaticTable；AlgorithmRouteStatus=staging_assumption_unverified；依据 cloned_from_BUS2.S1.JPK3，未证明生产验证通过。

**不得出现的错误事实断言（若有）：** 把前后物理连接对象直接写成 JPK3、JPK5 而遗漏中间直身段；将未验证路由称为已验证或已运行诊断结果。

**预期知识证据：** `busway.knowledge.md` K05（行 47–58，BUS2 连续链路与相邻接头）；`busway.knowledge.md` K06（行 59–68，工程属性与未验证组态）。

**原始核对位置：** T:Child[@FullCode='BUS2.S1.JPK4']/Ports/Port 与 Extension/Data。

**实际结果：** 待页面验收。

### BW-05

**问题：** 按当前资料，BUS2.S1.JPK1 与 JPK2、JPK1 与 JPK3，哪一对满足 D3 的相邻结构条件？是否已经能判断温差异常？

**应召回的证据要点：**

- JPK1/JPK2 经单个 ST1 连接，相关端口启用且无分支连接，满足 D3 的结构条件。
- JPK1/JPK3 经 ST1、JPK2、ST2，不能作为 D3 直接相邻接头对。
- 满足结构条件不等于异常；还缺同步有效 TempDiff、持续时间等实际诊断证据。

**不得出现的错误事实断言（若有）：** 按编码或图中可达性把 JPK1/JPK3 当直接相邻；将结构关系自动变成 JOINT_TEMP_DIFF 已发生。

**预期知识证据：** `busway.knowledge.md` K05（行 47–58，BUS2 连续链路与相邻接头）；`busway.knowledge.md` K17（行 215–230，诊断范围与证据要求）。

**原始核对位置：** T:BUS2.S1.JPK1/ST1/JPK2/ST2/JPK3 的 Ports/Port；R:§3.1 相邻接头、§5.4.1 D3。

**实际结果：** 待页面验收。

### BW-06

**问题：** BUS1.S1.JPK1 配置了哪些测点？能用它的 Temp 配合环境温度代替 TempDiff 做接头温升诊断吗？

**应召回的证据要点：**

- 只有 Battery、Humidity、Temp，测量系统引用为 Etemp_1。
- 没有 TempDiff；映射将这个 Temp 解释为母线槽直身绝对温度。
- 规则 v2 不允许用 Temp-AmbientTemp 重算或替代 TempDiff，依赖温升的诊断需要报告数据不足。

**不得出现的错误事实断言（若有）：** 从“接头类型应有测点”生成实际不存在的 TempDiff、WaterAlarm；把 Etemp Temp 当作接头温升，或许可相减回填。

**预期知识证据：** `busway.knowledge.md` K07（行 69–87，逻辑测点实际覆盖）；`busway.knowledge.md` K08（行 88–99，温度、温升与测量系统的区别）；`busway.knowledge.md` K09（行 100–109，温升源值与数据不足）。

**原始核对位置：** T:Child[@FullCode='BUS1.S1.JPK1']/Variables；M:observable_resolution.mappings.busway_body_absolute_temperature；R:§3.1、§7 M7。

**实际结果：** 待页面验收。

### BW-07

**问题：** BUS2.S1.ETB1 的三相电流、环境温度和两类报警分别是什么 Item、什么测量系统？

**应召回的证据要点：**

- Ia、Ib、Ic 对应 PM5350；AmbientTemp 对应 Etemp_2。
- PhaseImbalanceAlarm、TempRiseAlarm 对应 Alarm_1。
- ETB1 共 25 个逻辑测点，这些仅是组态清单。

**不得出现的错误事实断言（若有）：** 把 WaterAlarm 放在 ETB1，或声称某个报警已触发；把测量系统名当计量单位。

**预期知识证据：** `busway.knowledge.md` K07（行 69–87，逻辑测点实际覆盖）；`busway.knowledge.md` K10（行 110–132，电气量、环境量与报警量字典）。

**原始核对位置：** T:Child[@FullCode='BUS2.S1.ETB1']/Variables/Variable。

**实际结果：** 待页面验收。

### BW-08

**问题：** 测点中的 Unit=EDock_1 是计量单位吗？Temp、TempDiff、TempDiffRate 分别表示什么，规范单位是什么？

**应召回的证据要点：**

- Unit=EDock_1 是测量系统引用，不是计量单位。
- EDock Temp 为接头绝对温度，单位 °C；TempDiff 为温升，单位 K；TempDiffRate 为温升变化率，单位 K/min。
- Temp 还需结合系统/测温方案，Etemp Temp 的语义不同。

**不得出现的错误事实断言（若有）：** 将温升写成当前绝对温度，或单位写为 EDock_1。

**预期知识证据：** `busway.knowledge.md` K08（行 88–99，温度、温升与测量系统的区别）。

**原始核对位置：** M:materialization.topology_xml.observable_resolution.mappings；I:entities.joint_absolute_temperature、busway_body_absolute_temperature、temperature_rise、temperature_rise_rate。

**实际结果：** 待页面验收。

### BW-09

**问题：** 温升是 DerivedObservable，是否说明拓扑里没有直接温升测点？哪些派生量需要按需计算？

**应召回的证据要点：**

- 不是；temperature_rise、temperature_rise_rate 是派生物理量，但 availability=source_point，实际有 TempDiff、TempDiffRate。
- 按需计算六类：电流负载率、相邻接头温升差、接头温升相对均值偏差、温升与电流相关性、稳态温升波动范围、降温响应滞后。
- 定义计算概念不表示当前已有计算结果。

**不得出现的错误事实断言（若有）：** 把全部派生量视为直接源测点或视为测点全部缺失。

**预期知识证据：** `busway.knowledge.md` K11（行 133–149，按需派生量）。

**原始核对位置：** I:entities.temperature_rise、temperature_rise_rate、current_load_ratio、adjacent_joint_temperature_difference、joint_temperature_deviation_from_average、temperature_current_correlation、temperature_stability_range、cooling_response_lag。

**实际结果：** 待页面验收。

### BW-10

**问题：** 温升阈值超限有哪些候选根因？仅有这个现象能不能直接确认螺栓未拧紧？

**应召回的证据要点：**

- 候选集合完整且仅为：进水、负载过高、螺栓未拧紧、接触面氧化或污染、散热问题。
- 不能直接确认；需要对应 C 类完整规则及实际证据。候选关系没有概率或优先级。

**不得出现的错误事实断言（若有）：** 把候选根因作为具体接头已确诊事实；无来源增加候选原因、概率或排名。

**预期知识证据：** `busway.knowledge.md` K15（行 183–196，候选根因类别及机理）；`busway.knowledge.md` K16（行 197–214，现象与候选原因的对应关系）。

**原始核对位置：** I:relations.PCL_001～PCL_005；R:§5.1.2、§1.2。

**实际结果：** 待页面验收。

### BW-11

**问题：** 相邻接头温差异常的候选根因有哪些？比较用两个接头，结果应定位在哪里？

**应召回的证据要点：**

- 候选根因为螺栓未拧紧、接触面氧化或污染。
- 相邻对是比较基准；D3 比较后接头减前接头，现象定位在后接头。
- D3 命中不能替代 C3/C4 的完整根因判定。

**不得出现的错误事实断言（若有）：** 把温升阈值超限的五个候选原样套给相邻接头温差异常；定位到两个接头均已故障或整个工程。

**预期知识证据：** `busway.knowledge.md` K11（行 133–149，按需派生量）；`busway.knowledge.md` K12（行 150–164，六类可由规则输出的现象）；`busway.knowledge.md` K16（行 197–214，现象与候选原因的对应关系）；`busway.knowledge.md` K17（行 215–230，诊断范围与证据要求）。

**原始核对位置：** I:relations.PCL_011、PCL_012、SCP_004、SCP_017；R:§5.4.1、§5.4.2。

**实际结果：** 待页面验收。

### BW-12

**问题：** “温升阈值超限”是“温升异常”的一种，能因此认定 D1 命中就等于 D5 命中吗？

**应召回的证据要点：**

- 不能；分类特化关系不表示规则连锁命中。
- D1 读取目标接头有效 TempDiff 的阈值和持续条件；D5 有独立的温升与适用电流平方回归口径及前置条件。

**不得出现的错误事实断言（若有）：** 以概念包含关系声称 D5 已执行或命中；把温升变化率当 D5 直接判据。

**预期知识证据：** `busway.knowledge.md` K12（行 150–164，六类可由规则输出的现象）；`busway.knowledge.md` K13（行 165–172，概念分类不等于规则连锁命中）。

**原始核对位置：** I:relations.TAX_001、CHR_001、CHR_003～CHR_006；R:§5.1.1、§5.2.1。

**实际结果：** 待页面验收。

### BW-13

**问题：** 当前六类诊断现象分别由哪条规则判断，引用的规则版本是什么？

**应召回的证据要点：**

- D1 温升阈值超限；D2 电流负载异常；D3 相邻接头温差异常；D4 进水报警；D5 温升异常；D6 三相不平衡。
- 规则引用 DIAGNOSTIC_RULES_V2，文件 rule_v2.md，document_version=2.0.0，profile_id=diagnostic64_v2。
- 这些是规则知识引用，不能证明页面或生产诊断执行已经接通。

**不得出现的错误事实断言（若有）：** 遗漏 D6，或把 C1～C5 说成现象规则；将 lifecycle_status=active 等价为当前服务已部署并执行。

**预期知识证据：** `busway.knowledge.md` K12（行 150–164，六类可由规则输出的现象）；`busway.knowledge.md` K18（行 231–240，外部规则版本与解释边界）。

**原始核对位置：** I:entities.DIAGNOSTIC_RULES_V2、relations.EVL_001～EVL_006；R:§0、§5。

**实际结果：** 待页面验收。

### BW-14

**问题：** 仅解释规则 v2：在 Phase 2 已启动、报警绑定及质量有效且 D4 命中的前提下，C1 是否还必须先做现场检查才能输出进水根因？

**应召回的证据要点：**

- 按 2.0.0/diagnostic64_v2 的明确口径，C1 根据 D4 的有效进水报警证据确认 WATER_INGRESS，沿用接头定位。
- 该版本中现场检查是处置和复核，不是 C1 输出前置条件。
- 这是给定前提下的规则解释，不说明真实接头已经进水。

**不得出现的错误事实断言（若有）：** 添加该版本不存在的“必须先现场确认”门槛；遗漏有效证据/阶段前提，或由此声称 BUS2 接头已进水。

**预期知识证据：** `busway.knowledge.md` K19（行 241–248，进水报警与进水根因的版本化解释）。

**原始核对位置：** R:§0、§5.5.1、§5.5.2、§6.1。

**实际结果：** 待页面验收。

### BW-15

**问题：** 当前三相不平衡究竟是不支持判断现象，还是只没有根因能力？可不可以由大模型补出缺相等根因？

**应召回的证据要点：**

- D6 可以按有效不平衡报警信号判断 THREE_PHASE_IMBALANCE 现象。
- M3 表示没有进一步根因分类与规则，不能补猜缺相、负载分配或接线等根因。
- 实例文件 outside_current_product_scope 标记与 D6 存在范围差异，新知识说明已在 SRC-01 显式保留。

**不得出现的错误事实断言（若有）：** 说整个现象完全不支持，或假装已有对应根因规则。

**预期知识证据：** `busway.knowledge.md` K12（行 150–164，六类可由规则输出的现象）；`busway.knowledge.md` K16（行 197–214，现象与候选原因的对应关系）；`busway.knowledge.md` K23（行 275–287，已发现的来源差异及本说明采用的口径）。

**原始核对位置：** I:entities.THREE_PHASE_IMBALANCE、relations.EVL_006；R:§5.6.1 D6、§5.6.2 M3。

**实际结果：** 待页面验收。

### BW-16

**问题：** 局部热点是一个可以独立确诊的故障现象吗？它在 C3、C4、C5 中分别起什么作用？

**应召回的证据要点：**

- 局部热点是中间判据，没有 D 类条款单独输出它。
- C3、C4 要求目标接头为局部热点；C5 要求固定集合不存在局部热点。
- 局部热点口径是目标 TempDiff 相对同范围均值，不能与 D3 前后相邻温升差混用。
- 实例把它写为仅 C5 条件1 的注记与规则不一致，应保留差异。

**不得出现的错误事实断言（若有）：** 称 C5 用“有局部热点”确认散热问题，或单凭热点就确诊松动；沿用错误条款编号而不说明差异。

**预期知识证据：** `busway.knowledge.md` K14（行 173–182，局部热点和分布式温升异常的能力边界）；`busway.knowledge.md` K23（行 275–287，已发现的来源差异及本说明采用的口径）。

**原始核对位置：** I:entities.LOCAL_HOTSPOT、relations.EVL_007；R:§3.1、§6.3 条件1、§6.4 条件1、§6.5 条件4。

**实际结果：** 待页面验收。

### BW-17

**问题：** 知识里有“分布式温升异常”，是否说明规则 v2 能独立判定它？它有哪些候选原因？

**应召回的证据要点：**

- 它是知识概念；规则 2.0.0 没有判定该现象的条款。
- 候选原因是散热问题、负载过高；候选关系不补足缺失的现象规则。
- C5 输出散热问题根因，不能冒充分布式温升异常的独立判定条款。

**不得出现的错误事实断言（若有）：** 仅凭 SCP_010 的 C5 注记声称 C5 输出该现象。

**预期知识证据：** `busway.knowledge.md` K14（行 173–182，局部热点和分布式温升异常的能力边界）；`busway.knowledge.md` K16（行 197–214，现象与候选原因的对应关系）；`busway.knowledge.md` K23（行 275–287，已发现的来源差异及本说明采用的口径）。

**原始核对位置：** I:entities.DISTRIBUTED_TEMP_RISE、relations.PCL_018、PCL_019、SCP_010、CHR_018～CHR_020；R:§6.5。

**实际结果：** 待页面验收。

### BW-18

**问题：** 拓扑中 BUS1 接头是 1600 A，BUS1.S1.PIU4 是多少？能把 BUS2 构件的 1000 A 直接作为全工程诊断额定总管电流吗？

**应召回的证据要点：**

- BUS1.S1.PIU4 的 CurrentRating 为 1250 A；BUS2 接头/直身段记录 1000 A。
- 不能；这些是对应构件的源工程属性，诊断额定参数需要外部适用绑定。
- RATED_CURRENT_PARAMETER 仍为 unresolved。

**不得出现的错误事实断言（若有）：** 把各构件额定值混成全工程统一值，或宣称运行参数绑定已完成。

**预期知识证据：** `busway.knowledge.md` K06（行 59–68，工程属性与未验证组态）；`busway.knowledge.md` K20（行 249–258，临时参数与固定集合的适用范围）。

**原始核对位置：** T:BUS1.S1.PIU4、BUS2 接头/直身段的 Extension/Data[@Data1='CurrentRating']；I:entities.RATED_CURRENT_PARAMETER。

**实际结果：** 待页面验收。

### BW-19

**问题：** 当前 C5 散热问题 mock 使用哪些接头，适用于什么工程和范围？这能作为所有母线槽的固定要求吗？

**应召回的证据要点：**

- 工程 PBMBaseProj_2512，范围 BUS2.S1，profile diagnostic64_v2。
- 固定集合为 BUS2.S1.JPK1～JPK6，共 6 个接头，降流前后保持同一集合。
- 这是当前 mock 数据和参数适用范围，不是通用本体要求，也不表示这六个接头发生散热故障。

**不得出现的错误事实断言（若有）：** 把六个接头扩张为所有项目必须固定六点，或套用到 BUS1。

**预期知识证据：** `busway.knowledge.md` K20（行 249–258，临时参数与固定集合的适用范围）。

**原始核对位置：** R:§3.2、§6.5 C5；T:BUS2.S1 下接头清单。

**实际结果：** 待页面验收。

### BW-20

**问题：** 仅凭目前入库的拓扑和知识说明，BUS2.S1.JPK4 现在是否已经进水，当前温升是多少？

**应召回的证据要点：**

- 证据不足，无法判断当前进水与温升数值。
- 来源只有 Temp、TempDiff、TempDiffRate、WaterAlarm 的配置，无当前采样时间、有效质量、值或诊断执行记录。
- 可以说明存在这些测点以及进一步需要什么证据。

**不得出现的错误事实断言（若有）：** 捏造任何当前温升/报警值、正常状态、时间或已确诊根因。

**预期知识证据：** `busway.knowledge.md` K01（行 7–16，来源与证据边界）；`busway.knowledge.md` K07（行 69–87，逻辑测点实际覆盖）；`busway.knowledge.md` K19（行 241–248，进水报警与进水根因的版本化解释）；`busway.knowledge.md` K22（行 267–274，当前不能由这些资料回答的问题）。

**原始核对位置：** T:Child[@FullCode='BUS2.S1.JPK4']/Variables；R:§8。

**实际结果：** 待页面验收。

### BW-21

**问题：** 请给出可直接部署到生产的 D5、C3、C4、C5 阈值配置。

**应召回的证据要点：**

- 当前资料不足以提供可直接部署生产的配置。
- D5 为临时试用口径，须项目数据校准；C3～C5 仅当前 mock profile，不能推广。
- 生产值需要外部参数注册、适用范围及版本验证；本知识说明未登记运行阈值。

**不得出现的错误事实断言（若有）：** 把 mock 数字、窗口或固定集合包装成生产推荐配置。

**预期知识证据：** `busway.knowledge.md` K18（行 231–240，外部规则版本与解释边界）；`busway.knowledge.md` K20（行 249–258，临时参数与固定集合的适用范围）；`busway.knowledge.md` K21（行 259–266，参数知识与历史统计的边界）。

**原始核对位置：** R:§3.2、§5.2.1 说明、§6.3～§6.5；I:DiagnosticParameter 条目。

**实际结果：** 待页面验收。

### BW-22

**问题：** 螺栓未拧紧在母线槽故障中的历史占比是多少？能据此给 BUS2.S1.JPK4 的候选原因排概率吗？

**应召回的证据要点：**

- 没有提供比例、样本量、统计时段或原始统计文件，无法给出历史占比。
- BUSWAY_INCIDENT_CAUSE_DISTRIBUTION 未绑定，not_for_diagnostic_scoring=true，不能用它给当前设备打分或排概率。

**不得出现的错误事实断言（若有）：** 编造百分比、厂商统计来源、概率或候选排名。

**预期知识证据：** `busway.knowledge.md` K21（行 259–266，参数知识与历史统计的边界）；`busway.knowledge.md` K22（行 267–274，当前不能由这些资料回答的问题）。

**原始核对位置：** I:entities.BUSWAY_INCIDENT_CAUSE_DISTRIBUTION。

**实际结果：** 待页面验收。

### BW-23

**问题：** 可以把 BUS2.S1 的温升规则直接用于另一个项目的真空断路器或水泵吗？

**应召回的证据要点：**

- 不能根据现有资料证明适用；当前包限定母线槽工程 PBMBaseProj_2512 与相应规则/profile。
- 没有其他项目、真空断路器或水泵的适用模型、生产参数与证据，需要相应领域资料和版本化适用依据。

**不得出现的错误事实断言（若有）：** 把母线槽阈值、构件身份、固定集合直接迁移到其他设备或项目。

**预期知识证据：** `busway.knowledge.md` K20（行 249–258，临时参数与固定集合的适用范围）；`busway.knowledge.md` K22（行 267–274，当前不能由这些资料回答的问题）。

**原始核对位置：** R:§0、§3.2 适用边界；I:规则/参数引用；本包无该跨设备领域资料。

**实际结果：** 待页面验收。

### BW-24

**问题：** 这份知识说明的诊断机理是否已经核验了厂商业务原文？拓扑 JSON 和 XML 能否算两个独立权威来源？

**应召回的证据要点：**

- 不能声称已核验；被引用的《母线槽知识点记录 - 2026.7.10 - v1.3.md》没有随本次资料提供。
- 当前使用的是用户提供的诊断整理材料，权威性未评估；人工确认抽取不会自动提升来源等级。
- topology.source.json 是 topology.xml 的业务投影，二者属于同一来源链，不能当独立佐证。

**不得出现的错误事实断言（若有）：** 声称已读未提供原文、厂商标准背书或生产验证通过。

**预期知识证据：** `busway.knowledge.md` K01（行 7–16，来源与证据边界）；`busway.knowledge.md` K24（行 288–299，固定来源版本）。

**原始核对位置：** I:entities.*.provenance.source_refs；R:§0 规则来源说明；topology.source.json:metadata.source、conversion。

**实际结果：** 待页面验收。
