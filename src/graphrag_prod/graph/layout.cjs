/* Backend-only, deterministic layouts using the shipped Cytoscape.js bundle.
 * Input/output are bounded JSON over stdio. No source text or evidence is needed.
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const cytoscape = require(path.join(__dirname, '../playground/static/industrial/vendor/cytoscape.min.js'));
if (Number(process.versions.node.split('.')[0]) < 18) throw new Error('Node.js 18 or newer is required');
if (cytoscape.version !== '3.34.2') throw new Error('Unsupported graph layout engine');
const MAX_NODES = 20000, MAX_EDGES = 20000, MAX_FORCE_NODES = 300;
const STEP_X = 192, STEP_Y = 96, PAD = 42, HEADER = 36;
const round = value => Math.round(value * 1000) / 1000;
const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;

function grid(nodes) {
  const columns = Math.max(1, Math.ceil(Math.sqrt(nodes.length * 0.8)));
  const positions = Object.fromEntries(nodes.map((node, index) => [node.id, {
    x: PAD + STEP_X / 2 + index % columns * STEP_X,
    y: PAD + HEADER + STEP_Y / 2 + Math.floor(index / columns) * STEP_Y,
  }]));
  return { positions, width: columns * STEP_X + PAD * 2,
    height: Math.max(1, Math.ceil(nodes.length / columns)) * STEP_Y + PAD * 2 + HEADER };
}

// Shelf packing uses a fixed aspect target; viewport resizing only changes zoom.
function pack(tiles, groups = false) {
  const positions = {}, bounds = [];
  const ordered = tiles.slice().sort((a, b) => b.height - a.height || b.width - a.width || compare(a.key, b.key));
  const area = ordered.reduce((sum, tile) => sum + (tile.width + 40) * (tile.height + 40), 0);
  const target = Math.max(1, ...ordered.map(tile => tile.width), Math.sqrt(area * 1.7));
  let x = 0, y = 0, rowHeight = 0;
  for (const tile of ordered) {
    if (x && x + tile.width > target) { x = 0; y += rowHeight + 40; rowHeight = 0; }
    for (const [id, point] of Object.entries(tile.positions)) positions[id] = { x: round(point.x + x), y: round(point.y + y) };
    if (groups) bounds.push({ type: tile.key, x: round(x), y: round(y), width: round(tile.width), height: round(tile.height), count: tile.count });
    x += tile.width + 40;
    rowHeight = Math.max(rowHeight, tile.height);
  }
  return { positions, groups: bounds };
}

function grouped(nodes) {
  const byType = new Map();
  for (const node of nodes) {
    if (!byType.has(node.type)) byType.set(node.type, []);
    byType.get(node.type).push(node);
  }
  return { ...pack([...byType].map(([key, members]) => ({ ...grid(members), key, count: members.length })), true), engine: 'type-grid-pack.v1' };
}

function network(nodes, edges) {
  if (!edges.length || nodes.length > MAX_FORCE_NODES) return { ...grouped(nodes), engine: nodes.length > MAX_FORCE_NODES ? 'bounded-type-grid.v1' : 'isolated-type-grid.v1' };
  const initial = grid(nodes).positions;
  // CoSE can jitter coincident points internally. Seed every random call in this
  // isolated process so identical inputs produce identical coordinates.
  let seed = 2166136261;
  for (const character of JSON.stringify([nodes, edges])) seed = Math.imul(seed ^ character.charCodeAt(0), 16777619) >>> 0;
  Math.random = () => { seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5; return (seed >>> 0) / 4294967296; };
  const cy = cytoscape({ headless: true, styleEnabled: true, layout: { name: 'preset' },
    style: [{ selector: 'node', style: { width: 168, height: 64, padding: 8 } }],
    elements: [ ...nodes.map(node => ({ data: { id: node.id }, position: initial[node.id] })),
      ...edges.map((edge, index) => ({ data: { id: `layout-edge:${index}`, source: edge.source, target: edge.target } })) ] });
  try {
    const tiles = [], isolated = new Map();
    const byId = new Map(nodes.map(node => [node.id, node]));
    for (const component of cy.elements().components()) {
      const members = component.nodes();
      if (members.length === 1) {
        const node = byId.get(members[0].id());
        if (!isolated.has(node.type)) isolated.set(node.type, []);
        isolated.get(node.type).push(node);
        continue;
      }
      component.layout({ name: 'cose', animate: false, fit: false, randomize: false,
        nodeDimensionsIncludeLabels: false, nodeRepulsion: () => 900000,
        idealEdgeLength: () => 160, edgeElasticity: () => 140, nestingFactor: 1.2,
        gravity: 0.12, numIter: 450, initialTemp: 160, coolingFactor: 0.97,
        minTemp: 1, componentSpacing: 120, refresh: 0 }).run();
      let points = members.map(node => ({ id: node.id(), ...node.position() }));
      // Axis scaling preserves the CoSE structure while fitting the fixed
      // desktop card aspect. A sweep then removes any remaining card overlap.
      const xs = points.map(point => point.x), ys = points.map(point => point.y);
      const width = Math.max(...xs) - Math.min(...xs), height = Math.max(...ys) - Math.min(...ys);
      const ratio = width / Math.max(height, 1), factor = Math.sqrt(1.7 / Math.max(ratio, 0.1));
      points = points.map(point => ({ ...point, x: point.x * Math.min(2, Math.max(0.6, factor)), y: point.y / Math.min(2, Math.max(0.6, factor)) }));
      const placed = [];
      for (const point of points.sort((a, b) => a.y - b.y || a.x - b.x || compare(a.id, b.id))) {
        for (let step = 0; step <= placed.length; step++) {
          const collision = placed.find(other => Math.abs(point.x - other.x) < 184 && Math.abs(point.y - other.y) < 86);
          if (!collision) break;
          point.y = collision.y + 86;
        }
        placed.push(point);
      }
      const minX = Math.min(...points.map(point => point.x)), minY = Math.min(...points.map(point => point.y));
      const positions = Object.fromEntries(points.map(point => [point.id, { x: point.x - minX + PAD + 96, y: point.y - minY + PAD + 48 }]));
      tiles.push({ key: members[0].id(), positions,
        width: Math.max(...points.map(point => point.x)) - minX + PAD * 2 + 192,
        height: Math.max(...points.map(point => point.y)) - minY + PAD * 2 + 96 });
    }
    for (const [key, members] of isolated) tiles.push({ ...grid(members), key });
    return { ...pack(tiles), engine: 'cytoscape-3.34.2-cose-pack.v1' };
  } finally { cy.destroy(); }
}

function build(input) {
  if (!input || !Array.isArray(input.nodes) || !Array.isArray(input.edges) || input.nodes.length > MAX_NODES || input.edges.length > MAX_EDGES) throw new Error('Layout budget exceeded');
  const nodes = input.nodes.slice().sort((a, b) => compare(a.type, b.type) || compare(a.id, b.id));
  const ids = new Set(nodes.map(node => node.id));
  if (ids.size !== nodes.length || nodes.some(node => typeof node.id !== 'string' || typeof node.type !== 'string') || input.edges.some(edge => !ids.has(edge.source) || !ids.has(edge.target))) throw new Error('Invalid layout identity');
  const edges = input.edges.slice().sort((a, b) => compare(a.source, b.source) || compare(a.target, b.target));
  return { network: network(nodes, edges), grouped: grouped(nodes) };
}

try {
  const source = fs.readFileSync(0, 'utf8');
  if (source.length > 8000000) throw new Error('Layout input too large');
  const input = JSON.parse(source);
  process.stdout.write(JSON.stringify({ layouts: build(input.graph), ontology: build(input.ontology) }));
} catch (_) { process.stderr.write('Graph layout generation failed\n'); process.exitCode = 1; }
