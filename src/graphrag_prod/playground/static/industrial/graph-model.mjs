import { TYPE_LABELS, PREDICATE_LABELS } from "./core.mjs";

export const TYPE_COLORS = Object.freeze({
  EquipmentClass: ["#ecf0e3", "#a9bb87"],
  ProductFamily: ["#e1eede", "#80a571"],
  ProductModel: ["#e1eee4", "#79a38b"],
  Site: ["#e3e7db", "#9aa78a"],
  IndustrialSystem: ["#e5eade", "#a3b492"],
  InstalledAsset: ["#d7edda", "#71a27b"],
  Component: ["#e3edf5", "#8ba6bf"],
  Symptom: ["#f6e9d7", "#c8a071"],
  FaultMode: ["#f4e0db", "#c69583"],
  DiagnosticCondition: ["#f6e7d6", "#bfa076"],
  DiagnosticTest: ["#e6e3f1", "#a799c3"],
  MaintenanceAction: ["#e0eceb", "#86aca6"],
  Observation: ["#ece5f0", "#b19abb"],
  InspectionEvent: ["#e9e4f1", "#a599bd"],
  SourceEdition: ["#e6edef", "#98b1b4"],
});
export const VIEWS = Object.freeze({
  composition: {
    label: "组成层级",
    direction: "BT",
    predicates: ["PART_OF", "INSTALLED_AT", "LOCATED_AT"],
  },
  classification: {
    label: "分类层级",
    direction: "BT",
    predicates: ["SUBTYPE_OF", "IN_FAMILY", "CLASSIFIED_AS", "INSTANCE_OF"],
  },
  diagnostic: {
    label: "诊断关系",
    direction: "LR",
    predicates: [
      "HAS_SYMPTOM",
      "MAY_INDICATE",
      "CHECKED_BY",
      "ADDRESSED_BY",
      "OBSERVED_ON",
      "OBSERVES",
      "DESCRIBES",
    ],
  },
  connection: {
    label: "电气连接",
    direction: "LR",
    predicates: ["CONNECTS_TO"],
  },
  all: { label: "全部关系", direction: "LR", predicates: [] },
  ontology: { label: "本体模型", direction: "LR", predicates: [] },
});
export function validateGraphPage(page) {
  if (
    !page ||
    !Array.isArray(page.nodes) ||
    !Array.isArray(page.edges) ||
    !Array.isArray(page.literals) ||
    page.nodes.length > 150 ||
    page.edges.length > 200 ||
    page.literals.length > 200 ||
    !page.view_token
  )
    throw new Error("图谱响应超出约定范围。");
  const ids = new Set(page.nodes.map((n) => n.entity_id));
  const revisions = [...page.edges.flatMap(e => e.revision_ids || [e.revision_id]), ...page.literals.map(e => e.revision_id)];
  if (new Set(page.edges.map(e => e.fact_key || e.revision_id)).size !== page.edges.length)
    throw new Error("图谱关系未按事实聚合。");
  if (
    ids.size !== page.nodes.length ||
    new Set(revisions).size !== revisions.length ||
    page.edges.some((e) => !ids.has(e.source) || !ids.has(e.target)) ||
    page.literals.some((e) => !ids.has(e.subject))
  )
    throw new Error("图谱节点或事实身份不一致。");
  if (
    page.page?.returned_nodes !== page.nodes.length ||
    page.page?.returned_edges !== page.edges.length ||
    page.page?.returned_literals !== page.literals.length
  )
    throw new Error("图谱响应数量不一致。");
  return page;
}
export function graphElements(page) {
  validateGraphPage(page);
  return [
    ...page.nodes.map((node) => {
      const colors = TYPE_COLORS[node.entity_type] || ["#e9ece7", "#a9b1a3"];
      return {
        data: {
          id: `n:${node.entity_id}`,
          label: node.label,
          typeLabel: TYPE_LABELS[node.entity_type] || node.entity_type,
          displayLabel: `${node.label}\n${TYPE_LABELS[node.entity_type] || node.entity_type}`,
          fill: colors[0],
          border: colors[1],
          entity: node,
        },
        classes: "instance",
      };
    }),
    ...page.edges.map((edge) => ({
      data: {
        id: `r:${edge.fact_key || edge.revision_id}`,
        source: `n:${edge.source}`,
        target: `n:${edge.target}`,
        label: PREDICATE_LABELS[edge.predicate] || edge.predicate,
        assertion: edge,
      },
      classes:
        (edge.authority_levels || [edge.authority_level]).includes("AUTHORITATIVE")
          ? "authoritative"
          : "secondary",
    })),
  ];
}
export function ontologyElements(schema) {
  const types = schema?.entity_types || [],
    relations = schema?.relationship_types || [];
  if (types.length > 64 || relations.length > 128)
    throw new Error("本体响应超出约定范围。");
  const ids = new Set(types.map((t) => t.name));
  const nodes = types.map((type) => {
    const colors = TYPE_COLORS[type.name] || ["#e9ece7", "#a9b1a3"];
    return {
      data: {
        id: `t:${type.name}`,
        label: TYPE_LABELS[type.name] || type.name,
        displayLabel: `${TYPE_LABELS[type.name] || type.name}\n${type.name}`,
        fill: colors[0],
        border: colors[1],
        definition: type,
      },
      classes: "schema",
    };
  });
  const edges = [];
  for (const relation of relations)
    for (const source of relation.source_types || [])
      for (const target of relation.target_types || [])
        if (ids.has(source) && ids.has(target)) {
          if (edges.length >= 200)
            throw new Error("本体关系组合超出可视化预算，请缩小范围。");
          edges.push({
            data: {
              id: `t-edge:${relation.name}:${source}:${target}`,
              source: `t:${source}`,
              target: `t:${target}`,
              label: PREDICATE_LABELS[relation.name] || relation.name,
              definition: relation,
            },
            classes: "schema-edge",
          });
        }
  return [...nodes, ...edges];
}
export function samePin(a, b) {
  return [
    "publication_id",
    "publication_generation",
    "activation_generation",
    "ontology_version_id",
    "tbox_checksum",
    "corpus_revision",
  ].every((k) => a?.[k] === b?.[k]);
}
