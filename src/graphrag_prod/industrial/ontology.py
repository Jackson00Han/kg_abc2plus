"""Composed electrical-maintenance ontology for Canalis and EvoPacT examples.

This is an application schema, not an official Schneider Electric ontology or
a set of approved diagnostic instructions. Product configurations, site assets
and observations remain distinct, individually evidenced identities.
"""

from __future__ import annotations

from graphrag_prod.ontology import (
    Cardinality,
    EntityTypeDefinition,
    HierarchyDefinition,
    HierarchyKind,
    PropertyDataType,
    PropertyDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)


INDUSTRIAL_TBOX_KEY = "industrial-electric-v1"


def _property(name: str, description: str, *, datatype: PropertyDataType = PropertyDataType.STRING) -> PropertyDefinition:
    return PropertyDefinition(
        name, datatype, False, Cardinality.ZERO_OR_ONE,
        description=description,
    )


def build_industrial_tbox(tenant_id: str, *, version: int = 1) -> TBoxVersion:
    """Return an unpublished composed T-Box with explicit hierarchy contracts.

Canonical keys use ``industrial:<stable-domain-key>``; automatic extraction
uses the existing candidate namespace. Optional literals must still have exact
source evidence. Neither alias text nor a model-family label is an asset ID.
    """
    types = (
        ("EquipmentClass", "分类概念；SUBTYPE_OF 表示更具体概念到更一般概念，不执行属性继承。", ()),
        ("ProductFamily", "产品系列；不代表一个具体型号或现场设备。", (
            _property("FamilyCode", "有来源依据的产品系列标识。"),
        )),
        ("ProductModel", "产品型号或明确标注的项目模拟配置；不得冒充厂商目录型号。", (
            _property("ModelCode", "型号或项目配置的明确标识。"),
            _property("ModelKind", "记录声明的型号性质，例如 CATALOG_MODEL 或 PROJECT_CONFIGURATION。"),
        )),
        ("Site", "有稳定标识的物理场站；同名场站不得默认合并。", ()),
        ("IndustrialSystem", "场站内的低压或中压系统；区别于现场可维护设备。", ()),
        ("InstalledAsset", "具有场站和稳定设备标识的现场设备实例。", (
            _property("AssetTag", "设备台账中的稳定设备编码，不以简称替代。"),
        )),
        ("Component", "有明确所属对象的部件实例，不以部件名称全局合并。", (
            _property("ComponentTag", "明确标识所属设备和部件的台账编码。"),
        )),
        ("Symptom", "异常现象概念；观察到现象并不意味着故障原因已确定。", ()),
        ("FaultMode", "候选故障模式；需适用性、支持和排除证据才能形成诊断判断。", ()),
        ("DiagnosticCondition", "候选原因或设备状态；可包含正常闭锁条件，不暗示损坏或故障。", ()),
        ("DiagnosticTest", "有来源依据的检查项目或检查依据，不自动执行现场操作。", ()),
        ("MaintenanceAction", "有来源依据且受适用条件限制的维护活动概念。", ()),
        ("Observation", "一次有来源、对象和时间语境的观察记录。", (
            _property("ObservedAt", "来源记录明确给出的观察时间。", datatype=PropertyDataType.DATETIME),
            _property("Assessment", "来源对观察的判断；不得将未知状态改写成确定结论。"),
        )),
        ("InspectionEvent", "一次巡检或检修事件，与设备及观测概念分开。", (
            _property("EventAt", "来源明确给出的事件时间。", datatype=PropertyDataType.DATETIME),
        )),
        ("SourceEdition", "来源文档的具体版本；门户版本和文档内版本分别记录。", (
            _property("SourceDocumentReference", "官方文档标识或项目资料标识。"),
            _property("EmbeddedRevision", "文档内标注的版本号。"),
            _property("PortalVersion", "下载门户标注的版本，不等同于文档内版本。"),
            _property("RegionalScope", "来源声明的地区适用范围。"),
        )),
    )
    entities = tuple(
        EntityTypeDefinition(
            name, ("industrial", "llm-candidate"), properties=properties,
            description=description,
        )
        for name, description, properties in types
    )

    def relationship(
        name: str, source: tuple[str, ...], target: tuple[str, ...], description: str,
        *, single_parent: bool = False,
    ) -> RelationshipTypeDefinition:
        return RelationshipTypeDefinition(
            name=name, source_types=source, target_types=target,
            source_cardinality=Cardinality.ZERO_OR_ONE if single_parent else Cardinality.ZERO_OR_MORE,
            target_cardinality=Cardinality.ZERO_OR_MORE, description=description,
        )

    relationships = (
        relationship("SUBTYPE_OF", ("EquipmentClass",), ("EquipmentClass",), "更具体的分类概念指向更一般的概念；无环，不产生 OWL 推理或属性继承。"),
        relationship("IN_FAMILY", ("ProductModel",), ("ProductFamily",), "明确型号或项目配置所属产品系列。", single_parent=True),
        relationship("CLASSIFIED_AS", ("ProductModel",), ("EquipmentClass",), "型号或项目配置所对应的设备分类。"),
        relationship("INSTANCE_OF", ("InstalledAsset",), ("ProductModel",), "现场设备对应的明确型号或项目配置。", single_parent=True),
        relationship("PART_OF", ("InstalledAsset", "Component"), ("IndustrialSystem", "InstalledAsset", "Component"), "子对象指向物理组成的父对象；无环、最多一个父对象；不表示电气连接或因果。", single_parent=True),
        relationship("INSTALLED_AT", ("InstalledAsset",), ("Site",), "现场设备所属场站。", single_parent=True),
        relationship("LOCATED_AT", ("IndustrialSystem",), ("Site",), "工业系统所在场站。", single_parent=True),
        relationship("CONNECTS_TO", ("InstalledAsset", "Component"), ("InstalledAsset", "Component"), "有来源支持的电气连接；连接网络不执行组成层级的无环规则。"),
        relationship("HAS_SYMPTOM", ("InstalledAsset", "Component"), ("Symptom",), "来源记录设备或部件出现某种现象；不确立原因或当前持续状态。"),
        relationship("MAY_INDICATE", ("Symptom",), ("FaultMode", "DiagnosticCondition"), "现象可能关联的故障模式或设备状态；正常闭锁条件不等同于设备故障。"),
        relationship("CHECKED_BY", ("FaultMode", "DiagnosticCondition"), ("DiagnosticTest",), "候选故障模式或设备状态的检查依据；仍需来源中的适用条件。"),
        relationship("ADDRESSED_BY", ("FaultMode", "DiagnosticCondition"), ("MaintenanceAction",), "故障模式或设备状态相关的有条件维护活动；不代表当前可执行指令。"),
        relationship("OBSERVED_ON", ("Observation",), ("InstalledAsset", "Component"), "一次观察明确关联的现场对象。", single_parent=True),
        relationship("OBSERVES", ("InspectionEvent",), ("Observation",), "巡检或检修事件包含的观察。"),
        relationship("DESCRIBES", ("Observation",), ("Symptom",), "观察所描述的异常现象概念。"),
        relationship("APPLIES_TO", ("SourceEdition",), ("ProductFamily", "ProductModel"), "来源版本明示的适用系列或型号；仅凭同品牌或相似名称不能建立此关系。"),
    )
    return TBoxVersion(
        tenant_id=tenant_id, key=INDUSTRIAL_TBOX_KEY, version=version,
        status=TBoxStatus.DRAFT, entity_types=entities,
        relationship_types=relationships,
        description="项目电气运维应用本体：Canalis 母线槽与 EvoPacT HVX 真空断路器；分类、组成、连接、候选原因和证据适用性分别建模。",
        hierarchies=(
            HierarchyDefinition(
                "EquipmentClassification", "SUBTYPE_OF", HierarchyKind.CLASSIFICATION,
                ("EquipmentClass",), description="分类概念有向无环图；更具体概念到更一般概念。",
            ),
            HierarchyDefinition(
                "PhysicalComposition", "PART_OF", HierarchyKind.COMPOSITION,
                ("IndustrialSystem", "InstalledAsset", "Component"),
                description="现场系统、设备与部件的物理组成；子对象到父对象。",
            ),
        ),
    )
