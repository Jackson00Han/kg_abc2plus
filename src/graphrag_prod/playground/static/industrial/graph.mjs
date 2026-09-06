import { graphElements, ontologyElements, VIEWS } from "./graph-model.mjs";

/** Reusable renderer: callers own authentication, scope, fetching and evidence UI. */
export class IndustrialGraph {
  constructor(container, { onSelect = () => {} } = {}) {
    if (typeof globalThis.cytoscape !== "function")
      throw new Error("图谱组件未加载，请刷新页面。");
    this.container = container;
    this.onSelect = onSelect;
    this.view = "composition";
    this.page = null;
    this.cy = globalThis.cytoscape({
      container,
      elements: [],
      minZoom: 0.15,
      maxZoom: 2.5,
      wheelSensitivity: 0.16,
      boxSelectionEnabled: false,
      selectionType: "single",
      style: [
        {
          selector: "node",
          style: {
            shape: "round-rectangle",
            width: 164,
            height: 66,
            "background-color": "data(fill)",
            "border-color": "data(border)",
            "border-width": 1,
            label: "data(displayLabel)",
            color: "#355343",
            "font-family":
              "-apple-system, PingFang SC, Microsoft YaHei, sans-serif",
            "font-size": 12,
            "text-wrap": "wrap",
            "text-max-width": 148,
            "text-valign": "center",
            "text-halign": "center",
            "text-outline-width": 0,
            "line-height": 1.6,
            padding: 4,
            "overlay-opacity": 0,
          },
        },
        {
          selector: "edge",
          style: {
            "curve-style": "bezier",
            width: 1.3,
            "line-color": "#b4c2b0",
            "target-arrow-color": "#9dad98",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.65,
            label: "data(label)",
            "font-size": 10,
            color: "#84947d",
            "text-background-color": "#fcfdfa",
            "text-background-opacity": 0.95,
            "text-background-padding": 4,
            "text-background-shape": "roundrectangle",
            "text-rotation": "autorotate",
            "overlay-opacity": 0,
          },
        },
        {
          selector: "edge.secondary",
          style: { "line-style": "dashed", "line-dash-pattern": [5, 3] },
        },
        {
          selector: "edge.authoritative",
          style: { "line-color": "#7da485", "target-arrow-color": "#7da485" },
        },
        {
          selector: "node:selected",
          style: {
            "border-width": 2.5,
            "border-color": "#237b4e",
            "background-color": "#d9edcf",
          },
        },
        {
          selector: "edge:selected",
          style: {
            width: 2.8,
            "line-color": "#427b56",
            "target-arrow-color": "#427b56",
            color: "#2a653c",
            "font-weight": 600,
          },
        },
        { selector: ".faded", style: { opacity: 0.38 } },
        {
          selector: "edge.schema-edge",
          style: { "line-style": "solid", "line-color": "#bec9b6" },
        },
      ],
    });
    this.cy.on("tap", "node", (event) => {
      this.highlight(event.target);
      this.onSelect({ kind: "node", ...event.target.data() });
    });
    this.cy.on("tap", "edge", (event) => {
      this.highlight(event.target);
      this.onSelect({ kind: "edge", ...event.target.data() });
    });
    this.cy.on("tap", (event) => {
      if (event.target === this.cy) {
        this.cy.elements().removeClass("faded");
        this.onSelect(null);
      }
    });
    this.resizeObserver = new ResizeObserver(() => {
      if (
        container.clientWidth &&
        container.clientHeight &&
        this.cy.nodes().length
      )
        this.fit();
      else this.cy.resize();
    });
    this.resizeObserver.observe(container);
  }
  setPage(page, view = "composition") {
    const elements =
      view === "ontology" ? ontologyElements(page.schema) : graphElements(page);
    this.page = page;
    this.view = view;
    this.cy.elements().remove();
    this.cy.add(elements);
    this.layout();
    return { nodes: this.cy.nodes().length, edges: this.cy.edges().length };
  }
  layout() {
    if (!this.cy.nodes().length) return;
    this.cy
      .layout({
        name: "dagre",
        rankDir: VIEWS[this.view]?.direction || "LR",
        rankSep: this.view === "ontology" ? 65 : 36,
        nodeSep: 30,
        edgeSep: 20,
        animate: false,
        fit: false,
        ranker: "network-simplex",
        padding: 40,
      })
      .run();
    this.fit();
  }
  fit() {
    this.cy.resize();
    this.cy.fit(undefined, 45);
    if (this.cy.zoom() > 1)
      this.cy.zoom({
        level: 1,
        renderedPosition: {
          x: this.container.clientWidth / 2,
          y: this.container.clientHeight / 2,
        },
      });
    this.cy.center();
  }
  zoom(factor) {
    this.cy.zoom({
      level: Math.max(
        this.cy.minZoom(),
        Math.min(this.cy.maxZoom(), this.cy.zoom() * factor),
      ),
      renderedPosition: {
        x: this.container.clientWidth / 2,
        y: this.container.clientHeight / 2,
      },
    });
  }
  highlight(item) {
    this.cy.elements().addClass("faded");
    const visible = item.isNode()
      ? item.closedNeighborhood()
      : item.union(item.connectedNodes());
    visible.removeClass("faded");
  }
  clear() {
    this.page = null;
    this.cy.elements().remove();
  }
  exportPNG() {
    return this.cy.png({
      output: "blob",
      bg: "#fcfdfa",
      full: true,
      maxWidth: 2600,
      maxHeight: 1800,
      scale: 2,
    });
  }
  destroy() {
    this.resizeObserver.disconnect();
    this.cy.destroy();
  }
}
