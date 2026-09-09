"""Bounded, deliberately public locations for authorized publication failures."""
from dataclasses import dataclass
from types import MappingProxyType


PUBLICATION_ISSUE_MESSAGES = MappingProxyType({
    "PROPERTY_VALUES_DIFFER": "该属性只允许一个值，但这些来源的值、单位或适用时间不一致。请核对标记的记录后返回修改。",
    "PROPERTY_REQUIRED": "该实体缺少必须填写的属性，请返回补充。",
    "PROPERTY_INVALID": "该属性的值、类型、单位或时间不符合当前知识模型，请返回修改。",
    "SCHEMA_INVALID": "该实体、属性或关系不符合当前知识模型，请核对后返回修改。",
    "ENDPOINT_MISSING": "这条事实依赖的实体来源未包含在本次发布中，请勾选对应来源；尚未确认时请返回审核。",
    "RELATIONSHIP_REQUIRED": "该实体缺少知识模型要求的关系，请返回补充。",
    "RELATIONSHIP_CARDINALITY": "这些关系连接了多个不同实体，超过知识模型允许的数量，请核对后返回修改。",
    "RELATIONSHIP_PROPERTY_INVALID": "这条关系的附加属性缺失、格式不正确或有多个不同值，请返回修改。",
    "REPLACEMENT_REQUIRED": "这条记录已有发布版本，请刷新候选并按替换版本重新预览。",
    "ACTIVE_VERSION_CHANGED": "生效发布版本已变化，请刷新可发布候选和发布历史，再重新生成预览。",
    "PREVIEW_CHANGED": "内容或预览已变化，请重新生成完整发布预览。",
    "ALREADY_PUBLISHED": "这批内容已经发布，请刷新可发布候选。",
})


@dataclass(frozen=True, slots=True)
class PublicationIssueTarget:
    record_id: str | None = None
    entity_id: str | None = None
    entity_name: str | None = None
    predicate: str | None = None

    def __post_init__(self):
        for value in (self.record_id, self.entity_id, self.entity_name, self.predicate):
            if value is not None and (not isinstance(value, str) or not value or len(value) > 500):
                raise ValueError("invalid publication issue location")
        if self.record_id is None and self.entity_id is None:
            raise ValueError("publication issue location requires a record or entity")


@dataclass(frozen=True, slots=True)
class PublicationIssue:
    reason: str
    targets: tuple[PublicationIssueTarget, ...] = ()
    property_name: str | None = None
    truncated: bool = False

    def __post_init__(self):
        if self.reason not in PUBLICATION_ISSUE_MESSAGES:
            raise ValueError("unknown publication issue reason")
        if not isinstance(self.targets, tuple) or len(self.targets) > 50 or any(
            not isinstance(item, PublicationIssueTarget) for item in self.targets
        ):
            raise ValueError("publication issue targets exceed their bound")
        if self.property_name is not None and (not isinstance(self.property_name, str) or not 1 <= len(self.property_name) <= 128):
            raise ValueError("invalid publication issue property")
        if type(self.truncated) is not bool:
            raise ValueError("invalid publication issue truncation flag")

    @property
    def message(self) -> str:
        return PUBLICATION_ISSUE_MESSAGES[self.reason]
