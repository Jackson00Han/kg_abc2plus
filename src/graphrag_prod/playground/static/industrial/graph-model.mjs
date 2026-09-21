import { TYPE_LABELS, PREDICATE_LABELS } from "./core.mjs";

// A stable palette also covers types introduced by future ontology packages.
const PALETTE = [
  ["#173c3c", "#5ee0c0"], ["#263453", "#85b9ff"],
  ["#393050", "#bc9bfa"], ["#443824", "#ebc276"],
  ["#432c3c", "#ed99b4"], ["#263e40", "#79d2dd"],
  ["#35422c", "#b6d78c"], ["#423329", "#eda979"],
];
export function typeColors(type = "") {
  let hash = 2166136261;
  for (const char of type) hash = Math.imul(hash ^ char.codePointAt(0), 16777619);
  return PALETTE[(hash >>> 0) % PALETTE.length];
}
export const TYPE_COLORS = new Proxy(Object.freeze({}), { get: (_, type) => typeColors(String(type)) });
export function typeLabel(type, schema = {}) {
  const definition = (schema.entity_types || []).find(item => item.name === type);
  return definition?.display_name || definition?.label || TYPE_LABELS[type] || type;
}
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
    page.nodes.length > 200 ||
    page.edges.length > 300 ||
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
// Keep the shortest unique path suffix on the canvas; identity and full label
// remain available in search, hover and the evidence inspector.
export function compactGraphLabels(nodes) {
  const suffixes = nodes.map(node => [String(node.label),
    ...String(node.label).matchAll(/[\/#]/g)].map(part => typeof part === "string" ? part : String(node.label).slice(part.index + 1)).filter(Boolean).reverse());
  return nodes.map((node, index) => suffixes[index].find(suffix =>
    suffixes.every((other, offset) => offset === index || !other.includes(suffix))) || node.label);
}
export function wrapTypeLabel(label) {
  const words = label.replace(/([a-z0-9])([A-Z])/g, "$1 $2").split(" ").filter(Boolean);
  if (words.length < 2) return label;
  let split = 1, best = Infinity;
  for (let index = 1; index < words.length; index++) {
    const width = Math.max(words.slice(0, index).join("").length, words.slice(index).join("").length);
    if (width < best) { best = width; split = index; }
  }
  return `${words.slice(0, split).join("")}\n${words.slice(split).join("")}`;
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
  const labels = compactGraphLabels(page.nodes);
  const hubs = new Set([...degree].filter(([, value]) => value >= 4).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 12).map(([id]) => id));
  return [
    ...page.nodes.map((node, index) => {
      const colors = typeColors(node.entity_type);
      const name = typeLabel(node.entity_type, page.schema);
      return {
        data: {
          id: `n:${node.entity_id}`,
          label: node.label,
          typeLabel: name,
          type: node.entity_type,
          displayLabel: labels[index],
          searchText: [node.label, node.entity_id, node.canonical_key,
            name, node.entity_type].filter(Boolean).join(" ").toLocaleLowerCase(),
          visibleDegree: degree.get(node.entity_id),
          overviewLabel: labels[index].length > 18 ? labels[index].slice(0, 17) + "…" : labels[index],
          fill: colors[0],
          border: colors[1],
          entity: node,
        },
        classes: `instance${node.entity_type === "InstalledAsset" ? " asset" : ""}${hubs.has(node.entity_id) ? " hub" : ""}`,
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
    const colors = typeColors(type.name);
    return {
      data: {
        id: `t:${type.name}`,
        label: typeLabel(type.name, schema),
        typeLabel: typeLabel(type.name, schema),
        type: type.name,
        displayLabel: wrapTypeLabel(typeLabel(type.name, schema)),
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
