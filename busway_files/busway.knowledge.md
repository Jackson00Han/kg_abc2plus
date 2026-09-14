# 母线槽工程结构与诊断知识说明

文档标识：`busway.knowledge`；文档版本：`1.0.0`；整理日期：2026-09-13。适用工程：`PBMBaseProj_2512`；用途：当前知识库建设模拟。来源等级：用户提供的组态与诊断整理材料，权威性未评估，待人工复核。本文没有实测时序或已确认故障案例。

本文保存设备结构、测点语义、现象与候选原因、适用范围及外部规则引用。规则执行逻辑、运行参数注册和诊断计算由外部服务负责。文中规则引用均限于 `rule_v2.md` 的 `document_version=2.0.0`、`profile_id=diagnostic64_v2`，不表示生产规则已部署。

## K01 来源与证据边界

本文的来源代号为：T=`new_files/topology.xml`；M=`new_files/mapping.topology_xml.yaml`；I=`new_files/instances.diagnostic.yaml`；R=`new_files/rule_v2.md`。T 的 JSON 业务投影为 `topology.source.json`，两份文件属于同一来源链，不能算作相互独立的佐证。精确来源版本见文末 SHA-256 清单。每节的来源定位均基于这些固定版本。

I 与 R 提到的《母线槽知识点记录 - 2026.7.10 - v1.3.md》没有随本次资料提供；本文没有读取或验证该原文，也没有将这些整理条目标为厂商标准、已核验业务原文或生产权威结论。本文发现的来源差异在 K23 中明确保留。

设备拓扑、端口启用配置和逻辑测点是工程组态知识。存在接头、温升测点或进水报警测点，并不表示该接头当前温升超限、发生进水、已经通电或运行正常。本文列出的候选根因均为知识类别，不是任何具体接头的故障事件。

来源：T `/Project/NodeTree/Root`；I `entities`、`relations`；R §0、§1.2、§8。

## K02 工程、母线槽与分区

工程 `PBMBaseProj_2512` 包含两条母线槽：`BUS1` 和 `BUS2`。`BUS1` 包含分区 `BUS1.S1`，`BUS2` 包含分区 `BUS2.S1`。这两个分区共包含 19 个构件；计入工程、母线槽和分区后，共有 24 个工程对象。19 个构件包括 9 个接头、7 个直身段、1 个法兰、1 个插接箱和 1 个始端单元。

`BUS1.S1` 包含 7 个构件：法兰 `BUS1.S1.FE1`，接头 `BUS1.S1.JPK1`、`BUS1.S1.JPK2`、`BUS1.S1.JPK3`，直身段 `BUS1.S1.ST1`、`BUS1.S1.ST2`，插接箱 `BUS1.S1.PIU4`。

`BUS2.S1` 包含 12 个构件：始端单元 `BUS2.S1.ETB1`，接头 `BUS2.S1.JPK1`、`BUS2.S1.JPK2`、`BUS2.S1.JPK3`、`BUS2.S1.JPK4`、`BUS2.S1.JPK5`、`BUS2.S1.JPK6`，直身段 `BUS2.S1.ST1`、`BUS2.S1.ST2`、`BUS2.S1.ST3`、`BUS2.S1.ST4`、`BUS2.S1.ST5`。本拓扑没有三通构件。

来源：T `/Project/NodeTree/Root/Children/Child[@FullCode='BUS1']` 与 `Child[@FullCode='BUS2']` 及其分区 `Children/Child`；M `materialization.topology_xml.entity_mappings`。以上数量由当前结构计数，仅描述本工程。

## K03 对象身份与连接语义

工程对象需要用工程 ID 和完整编码共同识别。例如，`PBMBaseProj_2512/BUS1.S1.JPK1` 与 `PBMBaseProj_2512/BUS2.S1.JPK1` 是不同接头，不能因为都叫 `JPK1` 而合并。`Code`、显示名称和 `FieldCode` 不能替代完整的工程内身份。

端口引用采用 `{工程ID}/{完整编码}/port/{端口序号}`；逻辑测点引用采用 `{工程ID}/{完整编码}#{Item}`。端口的 `Load` 指向构件，不是对端端口 ID；父子包含关系与端口连接是两种不同关系。

当前文件记录 51 个端口，其中 33 条端口记录含非空 `Load`，18 条没有 `Load`。33 是“带连接目标的端口记录数”，不是 33 条互不重复的物理链路。`Enabled=true` 仅是组态启用状态，不能证明线路当前带电、支路有电流或设备健康。

来源：M `materialization.topology_xml.identity_schemes`；T 所有构件 `Ports/Port`。对应 JSON 的定位是 `assets[*].asset_ref`、`ports[*].port_ref`、`measurement_points[*].point_ref`。

## K04 BUS1 端口链路与插接关系

在工程 `PBMBaseProj_2512` 中，BUS1 的主链路按源输出端口的 `Load` 依次为：`BUS1.S1.FE1 → BUS1.S1.JPK1 → BUS1.S1.ST1 → BUS1.S1.JPK2 → BUS1.S1.ST2 → BUS1.S1.JPK3`。这些主链路连接也有相应输入端口的回指。

直身段 `BUS1.S1.ST2` 的附件端口 3（`Attach1`）连接插接箱 `BUS1.S1.PIU4`。插接箱属于 `BUS1.S1` 分区，不属于接头 `BUS1.S1.JPK3`。插接箱在本文件中没有自己的 `Ports` 记录；不能据此猜出对端端口编号。

上述附件连接表明存在插接结构，不足以证明当前实际分流。是否已分流、支路是否工作以及电流适用范围，还需要外部运行证据。不能仅沿编码顺序把 `BUS1.S1.JPK2` 与 `BUS1.S1.JPK3` 判成满足所有 D3 条件的无分支接头对。

来源：T 各 `Child[@FullCode='BUS1.S1.…']/Ports/Port`，重点 `BUS1.S1.ST2` 的 `Port[@Ordinal='3']/@Load`；M `materialization.topology_xml.runtime_branch_state`；R §3.1“相邻接头”、§4 G3。

## K05 BUS2 连续链路与相邻接头

在工程 `PBMBaseProj_2512` 中，BUS2 的源输出端口形成连续主链路：`BUS2.S1.ETB1 → BUS2.S1.JPK1 → BUS2.S1.ST1 → BUS2.S1.JPK2 → BUS2.S1.ST2 → BUS2.S1.JPK3 → BUS2.S1.ST3 → BUS2.S1.JPK4 → BUS2.S1.ST4 → BUS2.S1.JPK5 → BUS2.S1.ST5 → BUS2.S1.JPK6`。相关输入端口回指一致，相关端口均配置为启用，直身段附件端口没有 `Load`。

其中 `BUS2.S1.JPK4` 的输入端口 1 指向 `BUS2.S1.ST3`，输出端口 2 指向 `BUS2.S1.ST4`；它与 JPK3、JPK5 之间分别隔着 ST3、ST4，不能把相邻接头直接写成端口的连接目标。

按 R 的 D3 结构条件，BUS2 中可从端口证据推导出 5 对“经单个直身段连接”的相邻接头：JPK1/JPK2（经 ST1）、JPK2/JPK3（经 ST2）、JPK3/JPK4（经 ST3）、JPK4/JPK5（经 ST4）、JPK5/JPK6（经 ST5）；这些简称均限定在 `BUS2.S1`。这是结构派生知识，不是已发生的相邻接头温差异常。实际比较仍需要两个接头在同一时间轴上的有效 `TempDiff`。

`BUS2.S1.JPK1` 与 `BUS2.S1.JPK3` 之间有 ST1、JPK2、ST2，不能作为 D3 的直接相邻接头对。BUS1 和 BUS2 之间也没有可用于 D3 的跨母线槽连接证据。

来源：T `BUS2.S1` 各构件的 `Ports/Port`；R §3.1“相邻接头”、§5.4.1 D3。本节明确标注的相邻对为基于这两类来源的整理派生，T 没有预存该诊断关系。

## K06 工程属性与未验证组态

工程 `PBMBaseProj_2512` 的 BUS1 接头 JPK1～JPK3、直身段 ST1～ST2 的扩展属性 `CurrentRating` 为 `1600 A`。BUS1 插接箱 `BUS1.S1.PIU4` 的 `CurrentRating` 为 `1250 A`，`BreakerInterruptingCapability` 为 `50kA`，`BreakerPolarity` 为 `3`。

BUS2 接头 JPK1～JPK6 和直身段 ST1～ST5 的扩展属性 `CurrentRating` 为 `1000 A`。这些接头和直身段的 `ProtectionDegree` 为 `IP54`。以上是对应构件的源工程属性；不能直接把某个构件的额定电流注册为全工程、所有母线槽或某次诊断的“额定总管电流”。I 的 `RATED_CURRENT_PARAMETER` 仍标记 `parameter_binding_status=unresolved`，诊断使用的额定参数需要外部权威绑定。

`BUS1.S1.JPK1`、`BUS1.S1.JPK2` 的 `AlgorithmType` 为 `ConfigError`。这表示来源算法路由配置需要复核，不能改写为设备发生了某种故障。`BUS2.S1.JPK4`、`BUS2.S1.JPK5`、`BUS2.S1.JPK6` 的路由为 `StaticTable`，但同时标有 `AlgorithmRouteStatus=staging_assumption_unverified`，依据为 `cloned_from_BUS2.S1.JPK3`，不能当作已验证生产配置。

来源：T 相应构件 `Extension/Data`、`@AlgorithmType`；I `entities.RATED_CURRENT_PARAMETER`；R §4 G4。

## K07 逻辑测点实际覆盖

工程 `PBMBaseProj_2512` 的源拓扑共记录 64 个逻辑测点。它们描述“配置了什么量”，不含任何采样值、采样时间或质量记录。下表每个 `Item` 都对应一个源逻辑测点。

| 测点所属构件 | 已配置的 Item | 测量系统引用 | 数量 |
|---|---|---|---:|
| BUS1.S1.JPK1 | Battery、Humidity、Temp | Etemp_1 | 3 |
| BUS1.S1.JPK2 | Battery、Humidity、Temp | Etemp_1 | 3 |
| BUS1.S1.JPK3 | Temp、TempDiff、TempDiffRate | EDock_1 | 3 |
| BUS1.S1.PIU4 | Ia、Ib、Ic、PFTot、kVArTot、kWTot | PM2125 | 6 |
| BUS2.S1.ETB1 | Ia、Ib、Ic、Hz、PFa、PFb、PFc、Van、Vbn、Vcn、Vab、Vbc、Vca、PFTot、kVArTot、kWTot、kVAra、kVArb、kVArc、kWa、kWb、kWc | PM5350 | 22 |
| BUS2.S1.ETB1 | AmbientTemp | Etemp_2 | 1 |
| BUS2.S1.ETB1 | PhaseImbalanceAlarm、TempRiseAlarm | Alarm_1 | 2 |
| BUS2.S1.JPK1、BUS2.S1.JPK2、BUS2.S1.JPK3、BUS2.S1.JPK4、BUS2.S1.JPK5、BUS2.S1.JPK6 | 每个接头各有 Temp、TempDiff、TempDiffRate、WaterAlarm | 前三项为 EDock_1；WaterAlarm 为 WaterAlarm_1 | 每接头 4，共 24 |

`BUS2.S1.ETB1` 合计有 25 个逻辑测点。`BUS1.S1.JPK1` 和 `BUS1.S1.JPK2` 没有 `TempDiff`、`TempDiffRate` 或 `WaterAlarm` 源逻辑测点。`BUS1.S1.JPK3` 没有 `WaterAlarm`。BUS2 六个接头都有 `WaterAlarm` 测点，但这不表示任何一个已经触发进水报警。

来源：T 各构件 `Variables/Variable[@Item='…']`；JSON `measurement_points` 按 `owner_asset_ref` 分组。64 是当前文件清单总数，不是知识图谱必然抽取完整的保证。

## K08 温度、温升与测量系统的区别

XML 的 `Variable@Unit`（JSON 的 `measurement_system_ref`）是测量系统引用，如 `Etemp_1`、`EDock_1`、`PM5350`，不是 °C、K 或 A 等计量单位。可观测量的规范计量单位取自诊断语义定义。

`Temp` 的语义需要结合测量系统与测温方案判断。当前映射把 Etemp 来源的 `Temp`，或 `TempType=2` 对应的 `Temp`，解释为“母线槽直身绝对温度” `busway_body_absolute_temperature`，单位 °C。因此，BUS1 的 JPK1、JPK2 虽然是测点所属构件，也不能把其 Etemp `Temp` 误标为接头温升或 EDock 接头绝对温度。

EDock/Ftemp 来源的 `Temp`，或 `TempType=1` 对应的 `Temp`，映射到“接头绝对温度” `joint_absolute_temperature`，单位 °C。当前实际对应 BUS1 的 JPK3 和 BUS2 的 JPK1～JPK6。`AmbientTemp` 对应“环境温度” `ambient_temperature`，单位 °C，当前位于 `BUS2.S1.ETB1`。

`TempDiff` 对应温升 `temperature_rise`，规范单位 K；`TempDiffRate` 对应温升变化率 `temperature_rise_rate`，规范单位 K/min。温度差 1 K 的数值大小等于温度差 1 °C，但绝对温度和温升仍是不同物理量，不能混用。

来源：M `materialization.topology_xml.observable_resolution.mappings` 中同名条目；I `entities.ambient_temperature`、`busway_body_absolute_temperature`、`joint_absolute_temperature`、`temperature_rise`、`temperature_rise_rate`；T K07 所列测点。

## K09 温升源值与数据不足

在规则版本 `2.0.0`、profile `diagnostic64_v2` 中，接头温升直接使用逻辑测点 `TempDiff` 的 EDock 源值。只有 `Temp` 和 `AmbientTemp`，或者只有 Etemp 直身温度时，不允许通过相减重算、回填或覆盖 `TempDiff`。I 中温升与绝对温度、环境温度的 `DERIVED_FROM` 关系表达物理量语义依赖，不授予运行时替代源值的权限。

规则要求的有效 `TempDiff` 必须质量为 `GOOD`、非空且为有限数值；缺测或无效质量会中断连续持续时间。没有这些运行数据时，不能根据本文或测点清单宣称某条温升规则已命中，也不能把“数据不足”说成“正常”。

温升变化率 `TempDiffRate` 有有效源值时优先使用源值。仅在没有有效源值且相邻计划采样时刻的 `TempDiff` 都有效、之间没有断点时，外部服务才可按 R §3.1 回算；首样本、缺测恢复后首样本或时间断点后首样本不能回算。本文没有进行此项计算。

来源：R §3.1、§4、§7 M7；I `relations.DER_001`、`DER_002`、`DER_003`。

## K10 电气量、环境量与报警量字典

下表是可观测量语义字典，来源为 I 的对应实体和 M 的 `observable_resolution.mappings`。表中“1”表示无量纲；报警状态是布尔量，不是测量值为 1 的当前事件。表中条目不表示每个接头都有这些测点，实际覆盖见 K07。

| Item | 可观测量 ID 与含义 | 规范单位 |
|---|---|---|
| Battery | `battery_voltage`：电池电压 | V |
| Humidity | `relative_humidity`：相对湿度 | % |
| Ia / Ib / Ic | `phase_a_current` / `phase_b_current` / `phase_c_current`：A/B/C 相电流 | A |
| Van / Vbn / Vcn | `phase_a_to_neutral_voltage` / `phase_b_to_neutral_voltage` / `phase_c_to_neutral_voltage`：A/B/C 相对中性线电压 | V |
| Vab / Vbc / Vca | `phase_ab_voltage` / `phase_bc_voltage` / `phase_ca_voltage`：AB/BC/CA 线电压 | V |
| Hz | `frequency`：电网频率 | Hz |
| PFa / PFb / PFc / PFTot | `phase_a_power_factor` / `phase_b_power_factor` / `phase_c_power_factor` / `total_power_factor`：A/B/C 相及总功率因数 | 1 |
| kWa / kWb / kWc / kWTot | `phase_a_active_power` / `phase_b_active_power` / `phase_c_active_power` / `total_active_power`：A/B/C 相及总有功功率 | kW |
| kVAra / kVArb / kVArc / kVArTot | `phase_a_reactive_power` / `phase_b_reactive_power` / `phase_c_reactive_power` / `total_reactive_power`：A/B/C 相及总无功功率 | kVAr |
| WaterAlarm | `water_ingress_alarm_state`：进水报警状态 | 1 |
| PhaseImbalanceAlarm | `phase_imbalance_alarm_state`：三相不平衡报警状态 | 1 |
| TempRiseAlarm | `busway_temperature_rise_alarm_state`：母线温升报警状态 | 1 |

在 R 的 D4、D6 中，有效报警信号 1 表示该报警触发，0 表示当前报警未触发；绑定、质量或信号无效时属于数据不足。信号 0 不等同于所有故障均不存在。本文没有任何报警实时值。

来源：I `entities` 上表各 ID；M `materialization.topology_xml.observable_resolution.mappings`；R §5.5.1 D4、§5.6.1 D6。

## K11 按需派生量

以下六种量在 I 中标记为 `computed_on_demand`。它们是计算概念，当前拓扑没有对应 `Item` 的直接源测点；具体计算需外部服务取得有效数据和适用参数后完成。

| 可观测量 ID | 名称与含义 | 规范单位 |
|---|---|---|
| `current_load_ratio` | 电流负载率；实际适用电流相对于适用额定电流的比例 | 1 |
| `adjacent_joint_temperature_difference` | 相邻接头温升差；由真实拓扑相邻接头的温升比较得到，D3 比较方向为后接头减前接头 | K |
| `joint_temperature_deviation_from_average` | 接头温升相对均值偏差；目标接头相对同范围可比较接头的均值偏差 | K |
| `temperature_current_correlation` | 温升与电流相关性；统计关系概念，D5 的具体口径见对应外部条款 | 1 |
| `temperature_stability_range` | 稳态温升波动范围；有效窗口中的温升波动范围 | K |
| `cooling_response_lag` | 降温响应滞后；负载下降后温升响应的时间滞后 | min |

`temperature_rise` 和 `temperature_rise_rate` 在 I 中也属于 `DerivedObservable`，但它们的可得性是 `source_point`，当前分别有 `TempDiff` 和 `TempDiffRate` 源测点。不能将“派生物理量”统一误解为“当前没有源测点”。

来源：I `entities` 本节八个 ID；M `observable_resolution.mappings.temperature_rise`、`temperature_rise_rate`；R §3.1、§5.4.1。

## K12 六类可由规则输出的现象

下表描述规则版本 `2.0.0` 的知识类别与外部规则引用。D 类条款产出的是现象证据，不能仅凭现象名称或候选原因关系确认根因；本文也没有记录任何现象已经发生。

| 现象 ID | 中文名称 | 规则引用 | 主要证据与定位边界 |
|---|---|---|---|
| `TEMP_THRESHOLD_EXCEEDED` | 温升阈值超限 | D1；R §5.1.1 | 单个目标接头的连续有效 TempDiff；母线级 TempRiseAlarm 仅作上下文，不能替代 D1 判据 |
| `CURRENT_LOAD_ABNORMAL` | 电流负载异常 | D2；R §5.3.1 | 一个目标接头的温升和适用电流、额定参数；干路电流段是电流适用基准，现象定位在目标接头 |
| `JOINT_TEMP_DIFF` | 相邻接头温差异常 | D3；R §5.4.1 | 拓扑合格的相邻接头对与同步有效 TempDiff；现象定位在后接头 |
| `WATER_ALARM` | 进水报警 | D4；R §5.5.1 | 有效进水报警测点；定位在目标接头，母线槽为随附上下文 |
| `TEMP_RISE_ABNORMAL` | 温升异常 | D5；R §5.2.1 | 目标接头温升与适用电流的统计关系；依赖 Phase 2 和对应数据门禁 |
| `THREE_PHASE_IMBALANCE` | 三相不平衡 | D6；R §5.6.1 | 有效三相电流不平衡报警测点；只确认现象，当前没有进一步根因分类或根因规则 |

来源：I `relations.EVL_001`～`EVL_006`、`CHR_001`～`CHR_014`、`SCP_001`～`SCP_007`、`SCP_015`～`SCP_019`；R 上表各节。I 中三相不平衡的范围标记存在差异，见 K23。

## K13 概念分类不等于规则连锁命中

I 把温升阈值超限、相邻接头温差异常、局部热点、分布式温升异常列为“温升异常”概念的特化。这是分类知识，不表示任一子概念出现就可以声称 D5 已执行或命中。

在规则 v2 中，D5 的“温升异常”有独立统计判据：目标接头 `TempDiff` 对适用代表电流平方的回归关系。温升变化率和降温响应滞后是可供解释的上下文，均不是 D5 直接读取的判据。D2 使用温升与负载对应的偏差口径，不能混用 D5 的回归指标。具体窗口、公式和临时阈值以外部固定版本条款为准。

来源：I `relations.TAX_001`～`TAX_004`、`CHR_004`、`CHR_006`、`CHR_008`；R §5.2.1、§5.3.1。

## K14 局部热点和分布式温升异常的能力边界

局部热点 `LOCAL_HOTSPOT` 表示某个接头相对同范围可比较接头的温升显著偏离。规则 v2 的热点口径比较“目标接头温升”与“同一诊断范围内全部可用连接点温升均值”，不同于 D3 的前后相邻接头温升差。

在规则 v2 中，局部热点是中间判据，没有任何 D 类规则单独输出它。C3 和 C4 使用“目标接头为局部热点”作为必要条件；C5 的条件则包含“固定集合不存在局部热点”。这些只是完整规则的一部分，单有热点不能确认螺栓未拧紧或接触面氧化。

分布式温升异常 `DISTRIBUTED_TEMP_RISE` 是 I 保存的知识概念，含义是多个接头或母线槽范围内出现非孤立的持续温升异常分布。但规则 `2.0.0` 没有判定该现象的条款，不能把 C5 输出的“散热问题”根因改写成该现象已被规则确认。

来源：I `entities.LOCAL_HOTSPOT`、`DISTRIBUTED_TEMP_RISE`、`relations.CHR_015`～`CHR_020`；R §3.1、§6.3 C3、§6.4 C4、§6.5 C5。关于 I 的过时条款注记，见 K23。

## K15 候选根因类别及机理

本文维护五个根因知识类别。它们来自用户提供的诊断整理稿，尚未核验该整理稿所引用的原始业务知识文件。表中机理不能直接证明具体设备已发生故障。

| 根因 ID | 中文名称 | 整理稿中的概念定义 | 外部规则引用 |
|---|---|---|---|
| `WATER_INGRESS` | 进水 | 水或潮气进入母线槽或相关部件并可能引发异常 | C1；R §6.1 |
| `OVERLOAD` | 负载过高 | 负载处于规则规定的高负载区并可能驱动发热；极端过载属于严重分支 | C2；R §6.2 |
| `LOOSE_BOLTS` | 螺栓未拧紧 | 接头紧固不足导致接触电阻和局部发热增加 | C3；R §6.3 |
| `CONTACT_OXIDATION` | 接触面氧化或污染 | 接触面退化或污染导致接触电阻和局部发热增加 | C4；R §6.4 |
| `HEAT_DISSIPATION` | 散热问题 | 散热条件或降温响应异常导致多点持续发热 | C5；R §6.5 |

来源：I `entities` 上表五个 ID、`relations.EVL_008`～`EVL_012`；R §6。

## K16 现象与候选原因的对应关系

以下每一行都是知识层的 `POSSIBLE_CAUSE` 候选关系，断言级别为“可能”，没有概率、排序或已确认故障的含义。必须取得实际数据并满足对应外部完整规则，才能输出具体对象的根因结论。

| 现象 | 候选根因集合 | I 中来源关系 |
|---|---|---|
| 温升阈值超限 `TEMP_THRESHOLD_EXCEEDED` | 进水、负载过高、螺栓未拧紧、接触面氧化或污染、散热问题 | PCL_001～PCL_005 |
| 温升异常 `TEMP_RISE_ABNORMAL` | 进水、负载过高、螺栓未拧紧、接触面氧化或污染、散热问题 | PCL_006～PCL_010 |
| 相邻接头温差异常 `JOINT_TEMP_DIFF` | 螺栓未拧紧、接触面氧化或污染 | PCL_011～PCL_012 |
| 电流负载异常 `CURRENT_LOAD_ABNORMAL` | 负载过高 | PCL_013 |
| 进水报警 `WATER_ALARM` | 进水 | PCL_014 |
| 局部热点 `LOCAL_HOTSPOT` | 螺栓未拧紧、接触面氧化或污染、进水 | PCL_015～PCL_017 |
| 分布式温升异常 `DISTRIBUTED_TEMP_RISE` | 散热问题、负载过高 | PCL_018～PCL_019 |

三相不平衡 `THREE_PHASE_IMBALANCE` 在当前资料中没有候选根因关系，R 的 M3 也明确没有对应根因能力。不能自行补入负载分配、缺相、接线等原因。局部热点和分布式温升异常的候选关系仍不代表已经有相应现象输出规则，能力边界见 K14。

来源：I `relations.PCL_001`～`PCL_019`；R §1.3、§5.1.2、§5.2.2、§5.3.2、§5.4.2、§5.5.2、§5.6.2 M3。

## K17 诊断范围与证据要求

| 范围 ID | 名称 | 知识含义与实例化边界 |
|---|---|---|
| `SINGLE_JOINT_SCOPE` | 单接头范围 | 以工程内一个确定接头为中心；可以由拓扑确定对象身份 |
| `ADJACENT_JOINT_PAIR_SCOPE` | 相邻接头对范围 | 需要实际端口连续链路证明的两个可测接头；D3 允许 JPK 直连 JPK 或经单个 ST 的无分支链路，相关端口均须启用 |
| `TRUNK_CURRENT_SEGMENT_SCOPE` | 干路电流段范围 | 使用同一适用干路电流且未跨越已确认分流点的范围；不能仅按所属母线槽全量扩张 |
| `PROVEN_UNSPLIT_EXTENSION_SCOPE` | 已证明未分流扩展范围 | 经过分支结构后，必须有外部运行证据证明支路未工作或断路，才可扩展电流适用范围；仅靠静态拓扑不能生成这一证明 |
| `BUSWAY_SYSTEM_SCOPE` | 母线槽系统范围 | 由同一工程和真实包含关系限定的母线槽范围；不自动等于每条诊断规则的计算集合 |

诊断比较基准、电流适用基准、最终定位对象和上报上下文应分别理解。D3 用两个接头作比较，但现象定位在后接头；D2 的电流来自适用电流段，但每次现象判断只针对一个明确接头；D4/C1 定位接头，随附母线槽上下文。

拓扑相邻不自动证明电流连续适用；同属干路电流段也不自动证明热学上可以比较。缺少点位、有效数据、连续窗口、参数绑定或必要运行证明时，需要报告具体缺项。

来源：I `entities` 上表五个范围 ID、`relations.SCP_004`、`SCP_006`、`SCP_015`～`SCP_019`；M `materialization.topology_xml.scope_derivations`、`scope_derivations.cross_scope_safety`；R §3.1、§4、§5.3.1、§5.4.1、§5.5。

## K18 外部规则版本与解释边界

规则引用实体 `DIAGNOSTIC_RULES_V2` 指向“母线槽诊断规则 v2”，文件 `rule_v2.md`，`document_version=2.0.0`，`profile_id=diagnostic64_v2`。图谱可以保存规则 ID、版本、适用对象和条款引用，用来回答“这个知识概念由哪条规则判断”。该引用不把知识库变成诊断执行引擎，也不证明任何规则在当前页面或生产环境中已接通。

外部规则区分四种结果：`MATCHED` 为前提完整且命中；`NOT_MATCHED` 为前提完整但未命中；`INSUFFICIENT_DATA` 为必要数据或证据不足；`NOT_APPLICABLE` 为对象、阶段或业务范围不适用。数据不足不能改写成正常，不适用也不能当作已排除全部故障。

规则 v2 的 D1、D2、D3 至少一项命中才启动 Phase 2；D4、D5、D6 不能反向启动该阶段。D6 只确认三相不平衡现象，其结果交给 G5：D6 命中时 D5、C2 不适用；D6 数据不足时不能假设三相平衡继续这两条规则；C3～C5 仍按各自前提独立判断。本文仅记录这些职责边界与版本关联，完整执行定义保持外置。

来源：I `entities.DIAGNOSTIC_RULES_V2`、`relations.EVL_001`～`EVL_012`；R §1、§2、§4 G5。

## K19 进水报警与进水根因的版本化解释

在规则 `2.0.0`、profile `diagnostic64_v2` 中，D4 只确认 `WATER_ALARM`（进水报警现象）；C1 负责输出 `WATER_INGRESS`（进水根因）。在 Phase 2 已启动且进水报警测点绑定、质量有效的前提下，D4 命中后，C1 根据同一有效报警证据确认进水根因，并沿用接头定位。该版本中现场检查属于处置和复核，不是 C1 输出的前置条件。

以上是对外部规则版本的解释，不是对 BUS2 六个接头的现状判断。拓扑只有 `WaterAlarm` 测点清单，没有有效 0/1 采样信号、运行时间或规则执行记录，因此不能根据本知识文件判断任何接头已经进水。

来源：R §0“v2 进水口径”、§5.5.1 D4、§5.5.2 C1、§6.1；I `relations.PCL_014`、`EVL_004`、`EVL_008`；T BUS2 六个接头的 `Variable[@Item='WaterAlarm']`。

## K20 临时参数与固定集合的适用范围

本文没有登记可执行的阈值配置。R §3.2 把 D2 的额定电流理想温升标为 `staging_synthetic`、仅离线 mock；D5 的回归窗口、计算口径和判断阈值是 v2 临时试用定义，待项目数据校准；C3～C5 参数仅适用于当前 mock profile。不能把这些参数转述成所有母线槽的生产阈值。

R 明确当前 mock 工程为 `PBMBaseProj_2512`，范围为 `BUS2.S1`。C5 使用由拓扑确定、降流前后保持一致的固定集合：`BUS2.S1.JPK1`、`BUS2.S1.JPK2`、`BUS2.S1.JPK3`、`BUS2.S1.JPK4`、`BUS2.S1.JPK5`、`BUS2.S1.JPK6`，共 6 个接头。六个接头是当前场景数据，不是“散热问题”的通用本体定义，也不是该集合已经发生散热故障的证据。

I 的诊断参数条目均为 `parameter_binding_status=unresolved`，数值来源标为外部参数注册。缺少参数、版本不匹配或超出相应使用边界时，必须报告不足，不能从相似项目、构件名或本文的工程属性猜参数。

来源：R §3.2、§6.5 C5；I `entities` 中 `DiagnosticParameter` 类型的 11 个条目；T `BUS2.S1` 下 JPK1～JPK6。

## K21 参数知识与历史统计的边界

当前诊断知识定义了 11 类待绑定的外部参数：额定电流 `RATED_CURRENT_PARAMETER`（A）、温升预警阈值 `TEMP_RISE_WARNING_THRESHOLD`（K）、温升严重阈值 `TEMP_RISE_CRITICAL_THRESHOLD`（K）、持续时间阈值 `SUSTAINED_DURATION_THRESHOLD`（min）、相邻温升差阈值 `ADJACENT_TEMP_DIFF_THRESHOLD`（K）、局部热点偏差阈值 `LOCAL_HOTSPOT_DEVIATION_THRESHOLD`（K）、负载率分档阈值 `LOAD_RATIO_BAND_THRESHOLD`（1）、电流稳定性阈值 `CURRENT_STABILITY_THRESHOLD`（1）、温升电流相关性阈值 `TEMP_CURRENT_CORRELATION_THRESHOLD`（1）、温升变化率阈值 `TEMP_RISE_RATE_THRESHOLD`（K/min）、降温响应滞后阈值 `COOLING_LAG_THRESHOLD`（min）。这些定义用于说明规则依赖什么参数，不包含已批准的运行参数值。

参考统计条目 `BUSWAY_INCIDENT_CAUSE_DISTRIBUTION` 名为“母线槽故障原因历史占比”，但 `statistic_binding_status=unresolved`，本文没有提供任何比例、样本量、统计时间范围或原始统计文件。它明确标记 `not_for_diagnostic_scoring=true`，不能用于给当前设备候选根因打分、排序或输出概率。

来源：I `entities.RATED_CURRENT_PARAMETER` 至 `COOLING_LAG_THRESHOLD`、`BUSWAY_INCIDENT_CAUSE_DISTRIBUTION`。

## K22 当前不能由这些资料回答的问题

仅凭本工程拓扑和本诊断知识说明，不能回答任一接头“现在温升多少”“最近两小时是否超温”“是否已经进水”“哪一个根因已确诊”，因为没有实测时序、有效质量、明确诊断时间和外部规则执行记录。可以回答设备组成、逻辑测点清单、知识概念、候选原因及缺少哪些证据。

当前规则还明确没有三相不平衡的进一步根因推理（M3）、异物影响的事前根因推理（M2）、绝缘故障根因推理（M4）以及可用的温升预测模型及已训练参数、验证准则（M5）。这些能力缺失不能由大模型补猜。本文也没有真空断路器、水泵或其他项目的设备资料，不能把本工程阈值、对象或规则适用范围迁移过去。

来源：T 的静态组态属性；R §7 M2～M7、§8；I `entities.DISTRIBUTED_TEMP_RISE` 与参数、统计条目的未绑定状态。

## K23 已发现的来源差异及本说明采用的口径

本说明不修改原始资料。下列差异必须随知识保留；当解释规则 `2.0.0` 的实际条款含义时，本说明使用 R 的明确条款，并把与 I/M 的差异公开记录，等待源资料维护者复核。

| 差异编号 | 原资料中的差异 | 本说明的处理 |
|---|---|---|
| SRC-01 | I `entities.THREE_PHASE_IMBALANCE.diagnostic_scope_status` 为 `outside_current_product_scope`，但同实体的 `rule_evaluation_status=produced_by_rule`，EVL_006 指向 D6；R §5.6.1 确实定义 D6 | 保留三相不平衡现象可由 D6 判断、M3 根因能力缺失的区分；不沿用“整个现象都不支持”的解释 |
| SRC-02 | I `entities.LOCAL_HOTSPOT.rule_evaluation_note` 称只作为“C5 条件1”的中间判据；EVL_007、CHR_015、CHR_017 也只关联 C5 | R §6.3 C3 条件1、§6.4 C4 条件1 均需要局部热点；R §6.5 C5 条件4 要求不存在热点。保留其“中间判据、无独立 D 类输出”的定位，不把错误条款注记当作规则事实 |
| SRC-03 | I `relations.SCP_020.rule_basis` 把“热点数量小于 5”归为 C5 条件1；M 的母线槽系统范围依据也误引 C5 条件1的热点计数 | 热点数量限制实际位于 R §6.4 C4 条件1。C5 条件1是降流窗口要求，C5 的固定集合和无热点要求见 R §6.5；不照搬错引 |
| SRC-04 | I `relations.SCP_010` 给分布式温升异常关联范围时带 `rule_clause=C5`，但同一现象实体与 CHR_018～CHR_020 明确没有可判定条款 | 保留概念及其母线槽范围知识，不把这条范围注记解释为 C5 能输出分布式温升异常；R §6.5 的实际输出是散热问题根因 |

I 中 `LOCAL_HOTSPOT` 的相邻对比较范围与母线槽范围注记，只能作为概念上下文；R §3.1 的具体热点计算需要先确定“同一诊断范围内全部可用连接点”，不能擅自缩减为任意两个相邻点。此处仍需外部执行方根据版本规则确定计算集合。

## K24 固定来源版本

下列 SHA-256 标识本说明实际核对的原始字节版本；本文整理稿不提升这些来源的权威等级。来源定位中的 I/M 路径为 YAML 键路径；R 使用原 Markdown 的章节与条款 ID；T 使用 XML 元素路径、完整编码和属性定位。

| 来源 | 文件 | SHA-256 |
|---|---|---|
| T | `new_files/topology.xml` | `26ab661ceafa7ed4745cdb6241a69ae407acf23d9a4c6246db32c840e3610a9a` |
| M | `new_files/mapping.topology_xml.yaml` | `7117c27a1031ad54e33ac6de1fcd5485d7c4e4e8247b509475b1629d78cf5b21` |
| I | `new_files/instances.diagnostic.yaml` | `5d8f4ea81f15bf783ceb987bb822b13f18537d80856ffafb7798f936a7628520` |
| R | `new_files/rule_v2.md` | `0ceb3c80312c6fa3b01be81ef421d90fdd45be760a693bb853fd5f61b6d4755a` |

来源清单只用于版本与证据追溯，不是设备实体、故障现象或诊断结果。
