import { TYPE_LABELS, PREDICATE_LABELS } from "./core.mjs";

export const TYPE_COLORS = Object.freeze({
  Equipment: ["#dceedd", "#7faa8a"],
  Risk: ["#f5e4de", "#bd9183"],
  EquipmentClass: ["#ecf0e3", "#a9bb87"],
  ProductFamily: ["#e1eede", "#80a571"],
  ProductModel: ["#e1eee4", "#79a38b"],
  Site: ["#e3e7db", "#9aa78a"],
  IndustrialSystem: ["#e5eade", "#a3b492"],
  InstalledAsset: ["#d4eddf", "#438364"],
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
  all: { label: "实例图谱", direction: "LR" },
  ontology: { label: "本体模型", direction: "LR" },
});
export function ontologyGraphFilters(schema = {}) {
  const relations = (schema.relationship_types || []).slice(0, 128);
  const declared = new Set(relations.map(relation => relation.name));
  const hierarchies = (schema.hierarchies || []).slice(0, 32)
    .filter(hierarchy => declared.has(hierarchy.relationship_type));
  return [
    ...hierarchies.map(hierarchy => ({
      value: `hierarchy:${hierarchy.name}`, label: hierarchy.name,
      group: '本体层级', predicates: [hierarchy.relationship_type],
    })),
    ...relations.map(relation => ({
      value: `relation:${relation.name}`,
      label: PREDICATE_LABELS[relation.name] || relation.name,
      group: '关系类型', predicates: [relation.name],
    })),
  ];
}
export function graphEmptyState(view, pin = {}, filterLabel = null) {
  if (view === 'ontology') return {
    title: '当前范围没有可显示的本体模型',
    detail: '请核对本体是否已启用，以及当前知识库和访问范围。',
    showAll: false,
  };
  if (filterLabel) return {
    title: `“${filterLabel}”下没有匹配的关系`,
    detail: '此处只显示当前本体筛选的关系，不代表知识尚未发布。可切换到全部关系查看。',
    showAll: true,
  };
  return pin.publication_id ? {
    title: '当前筛选范围没有匹配的已发布节点',
    detail: '知识库已有发布版本，请核对事实范围、来源筛选和当前身份的访问权限。',
    showAll: false,
  } : {
    title: '当前知识库尚无可见的已发布知识',
    detail: '请在知识构建中审核并发布，然后刷新图谱。',
    showAll: false,
  };
}
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
  // Counts describe this authorized page only, never the unseen whole graph.
  const degree = new Map(page.nodes.map(node => [node.entity_id, 0]));
  for (const edge of page.edges) {
    degree.set(edge.source, degree.get(edge.source) + 1);
    if (edge.source !== edge.target)
      degree.set(edge.target, degree.get(edge.target) + 1);
  }
  return [
    ...page.nodes.map((node) => {
      const colors = TYPE_COLORS[node.entity_type] || ["#e9ece7", "#a9b1a3"];
      return {
        data: {
          id: `n:${node.entity_id}`,
          label: node.label,
          typeLabel: TYPE_LABELS[node.entity_type] || node.entity_type,
          displayLabel: `${node.label}\n${TYPE_LABELS[node.entity_type] || node.entity_type}`,
          searchText: [node.label, node.entity_id, node.canonical_key,
            TYPE_LABELS[node.entity_type], node.entity_type].filter(Boolean).join(" ").toLocaleLowerCase(),
          visibleDegree: degree.get(node.entity_id),
          fill: colors[0],
          border: colors[1],
          entity: node,
        },
        classes: `instance${node.entity_type === "InstalledAsset" ? " asset" : ""}`,
      };
    }),
    ...page.edges.map((edge) => ({
      data: {
        id: `r:${edge.fact_key || edge.revision_id}`,
        source: `n:${edge.source}`,
        target: `n:${edge.target}`,
        label: PREDICATE_LABELS[edge.predicate] || edge.predicate,
        displayLabel: `${PREDICATE_LABELS[edge.predicate] || edge.predicate}${edge.source_count > 1 ? ` · ${edge.source_count} 源` : ""}${edge.fact_distinction ? " · 独立" : ""}`,
        assertion: edge,
      },
      classes:
        ((edge.authority_levels || [edge.authority_level]).includes("AUTHORITATIVE")
          ? "authoritative"
          : "secondary") + (edge.fact_distinction ? " independent" : ""),
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
        searchText: `${TYPE_LABELS[type.name] || type.name} ${type.name}`.toLocaleLowerCase(),
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
              displayLabel: PREDICATE_LABELS[relation.name] || relation.name,
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
