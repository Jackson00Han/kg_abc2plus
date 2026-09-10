import { graphElements, ontologyElements } from "./graph-model.mjs";

function control(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text) node.textContent = text;
  return node;
}

/** Renderer uses only the caller's authorized, version-bound graph page. */
export class IndustrialGraph {
  constructor(container, { onSelect = () => {}, onViewDetails = null, layoutOptions = {}, transformElements = items => items } = {}) {
    if (typeof globalThis.cytoscape !== "function")
      throw new Error("图谱组件未加载，请刷新页面。");
    this.container = container;
    this.onSelect = onSelect;
    this.onViewDetails = onViewDetails;
    this.layoutOptions = layoutOptions;
    this.transformElements = transformElements;
    this.view = "all";
    this.page = null;
    this.layoutMode = "hierarchy";
    this.showLabels = true;
    this.focusItem = null;
    this.fitFrame = null;
    this.destroyed = false;
    this.fullscreenActive = false;
    this.cy = globalThis.cytoscape({
      container,
      elements: [],
      minZoom: 0.08,
      maxZoom: 2.8,
      wheelSensitivity: 0.16,
      boxSelectionEnabled: false,
      selectionType: "single",
      style: [
        {
          selector: "node",
          style: {
            shape: "round-rectangle", width: 182, height: 70,
            "background-color": "data(fill)",
            "border-color": "data(border)", "border-width": 1.4,
            label: "data(displayLabel)", color: "#254737",
            "font-family": "-apple-system, PingFang SC, Microsoft YaHei, sans-serif",
            "font-size": 13, "font-weight": 500,
            "text-wrap": "wrap", "text-max-width": 162,
            "text-valign": "center", "text-halign": "center",
            "text-outline-width": 0, "line-height": 1.6,
            padding: 6, "overlay-opacity": 0,
          },
        },
        { selector: "node.asset", style: { "border-width": 2, "font-weight": 600 } },
        {
          selector: "edge",
          style: {
            "curve-style": "bezier", "control-point-step-size": 65,
            width: 1.6, "line-color": "#a0b6a9",
            "target-arrow-color": "#8ca797", "target-arrow-shape": "triangle",
            "arrow-scale": 0.8, label: "",
            "font-size": 11, color: "#627e6e",
            "text-background-color": "#f9fcfa", "text-background-opacity": 0.96,
            "text-background-padding": 4, "text-background-shape": "roundrectangle",
            "text-rotation": "autorotate", "text-margin-y": -2,
            "overlay-opacity": 0, "underlay-padding": 9,
          },
        },
        { selector: "edge.secondary", style: { "line-style": "dashed", "line-dash-pattern": [6, 4] } },
        { selector: "edge.authoritative", style: { "line-color": "#73a28a", "target-arrow-color": "#73a28a" } },
        { selector: "edge.schema-edge", style: { "line-style": "solid", "line-color": "#a8b7a2" } },
        { selector: "edge.label-visible", style: { label: "data(displayLabel)" } },
        { selector: ".faded", style: { opacity: 0.16 } },
        {
          selector: "node.neighbor", style: { "border-width": 2, "border-color": "#579477" },
        },
        {
          selector: "edge.neighbor", style: {
            width: 2.5, "line-color": "#468567", "target-arrow-color": "#468567",
            color: "#2e664a", label: "data(displayLabel)", "z-index": 5,
          },
        },
        {
          selector: "node:selected", style: {
            "border-width": 3, "border-color": "#176044", "background-color": "#bfe4d0",
            "underlay-color": "#70b794", "underlay-opacity": 0.15, "underlay-padding": 10,
            "font-weight": 700, "z-index": 10,
          },
        },
        {
          selector: "edge:selected", style: {
            width: 3.5, "line-color": "#1d7150", "target-arrow-color": "#1d7150",
            color: "#1d6045", label: "data(displayLabel)", "font-weight": 700,
            "text-background-color": "#e7f3eb", "z-index": 10,
          },
        },
        { selector: "edge.labels-hidden", style: { label: "" } },
        { selector: "edge.schema-edge.labels-hidden:selected", style: { label: "data(displayLabel)" } },
      ],
    });
    this.mountTools();
    this.cy.on("tap", "node, edge", event => this.select(event.target));
    this.cy.on("tap", event => {
      if (event.target === this.cy) this.clearFocus();
    });
    this.cy.on("zoom", () => this.updateZoom());
    this.cy.on("mouseover", "node, edge", event => {
      const data = event.target.data();
      this.container.title = data.entity
        ? `${data.label} · ${data.typeLabel} · 当前视图 ${data.visibleDegree} 条关系`
        : data.displayLabel || data.label;
    });
    this.cy.on("mouseout", "node, edge", () => { this.container.title = ""; });
    this.resizeObserver = new ResizeObserver(() => {
      // Preserve normal exploration; fullscreen details can shrink the canvas and clip nodes.
      this.cy.resize();
      if (this.isFullscreen()) this.scheduleFit();
    });
    this.resizeObserver.observe(container);
    this.keyHandler = event => {
      if (event.key === "Escape") this.clearFocus();
      if (event.target !== container) return;
      if (["+", "="].includes(event.key)) { event.preventDefault(); this.zoom(1.2); }
      if (event.key === "-") { event.preventDefault(); this.zoom(1 / 1.2); }
      if (event.key.toLowerCase() === "f") { event.preventDefault(); this.fit(); }
    };
    container.tabIndex = 0;
    container.addEventListener("keydown", this.keyHandler);
    this.updateZoom();
  }
  mountTools() {
    const tools = control("div", "graph-local-tools");
    tools.setAttribute("aria-label", "当前图谱探索工具");
    const searchBox = control("div", "graph-local-search");
    const search = control("input", "graph-node-search");
    search.type = "search";
    search.placeholder = "定位当前图中的节点…";
    search.setAttribute("aria-label", "按名称、编号或类型定位当前图中节点");
    search.autocomplete = "off";
    this.searchResults = control("div", "graph-search-results");
    this.searchResults.hidden = true;
    search.addEventListener("input", () => this.search(search.value));
    search.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        this.searchResults.querySelector("button")?.click();
      }
      if (event.key === "Escape") {
        search.value = "";
        this.searchResults.hidden = true;
      }
    });
    searchBox.append(search, this.searchResults);
    this.searchInput = search;
    tools.append(searchBox);
    const actions = control("div", "graph-display-actions");
    this.layoutButton = this.makeButton("网络布局", () => {
      this.layoutMode = this.layoutMode === "network" ? "hierarchy" : "network";
      this.layout();
    }, "切换层级布局与网络布局；布局不代表事实的因果顺序");
    this.labelsButton = this.makeButton("关系标签", () => {
      this.showLabels = !this.showLabels;
      this.labelsButton.setAttribute("aria-pressed", String(this.showLabels));
      this.updateZoom();
    }, "显示或隐藏关系标签，缩小时自动精简");
    this.labelsButton.setAttribute("aria-pressed", "true");
    this.fullscreenButton = this.makeButton("全屏", async () => {
      const workspace = this.container.closest(".graph-workspace") || this.container.parentElement;
      try {
        if (document.fullscreenElement) await document.exitFullscreen();
        else await workspace.requestFullscreen();
      } catch {
        this.focusStatus.textContent = "浏览器未允许全屏，可继续在当前画布探索。";
        this.focusBar.hidden = false;
      }
    }, "全屏查看图谱与证据");
    this.fullscreenButton.setAttribute("aria-pressed", "false");
    actions.append(this.layoutButton, this.labelsButton);
    if (document.fullscreenEnabled) {
      actions.append(this.fullscreenButton);
      this.fullscreenHandler = () => {
        const active = this.isFullscreen();
        this.fullscreenButton.textContent = active ? "退出全屏" : "全屏";
        this.fullscreenButton.title = active ? "退出全屏" : "全屏查看图谱与证据";
        this.fullscreenButton.setAttribute("aria-pressed", String(active));
        if (active || this.fullscreenActive) this.scheduleFit();
        this.fullscreenActive = active;
      };
      document.addEventListener("fullscreenchange", this.fullscreenHandler);
      this.fullscreenHandler();
    }
    tools.append(actions);
    this.container.insertAdjacentElement("beforebegin", tools);
    this.tools = tools;
    this.focusBar = control("div", "graph-focus-bar");
    this.focusBar.hidden = true;
    this.focusStatus = control("span", "graph-focus-status", "点击节点突出直接关联；空白处恢复全图");
    this.focusStatus.setAttribute("aria-live", "polite");
    this.focusButton = this.makeButton("聚焦关联", () => this.fitFocus(), "放大当前选中节点的一跳关联");
    this.restoreButton = this.makeButton("恢复全图", () => { this.clearFocus(); this.fit(); });
    this.focusButton.hidden = true;
    this.restoreButton.hidden = true;
    if (typeof this.onViewDetails === "function") {
      this.detailsButton = this.makeButton("查看详情 ↓", () => this.onViewDetails(), "查看所选内容的详情与来源");
      this.detailsButton.hidden = true;
    }
    this.zoomLabel = control("span", "graph-zoom-label");
    this.focusBar.append(this.focusStatus);
    if (this.detailsButton) this.focusBar.append(this.detailsButton);
    this.focusBar.append(this.focusButton, this.restoreButton, this.zoomLabel);
    this.container.insertAdjacentElement("beforebegin", this.focusBar);
  }
  makeButton(label, action, title = label) {
    const button = control("button", "graph-tool-button", label);
    button.type = "button";
    button.title = title;
    button.addEventListener("click", action);
    return button;
  }
  search(query) {
    this.searchResults.replaceChildren();
    const text = query.trim().toLocaleLowerCase();
    this.searchResults.hidden = !text;
    if (!text) return;
    const matches = this.cy.nodes().filter(node =>
      String(node.data("searchText") || node.data("label")).toLocaleLowerCase().includes(text));
    const summary = control("p", "graph-search-summary", matches.length
      ? `${matches.length} 个当前可见匹配${matches.length > 8 ? "，显示前 8 个" : ""}`
      : "当前视图中没有匹配，可调整上方范围后重试。");
    this.searchResults.append(summary);
    matches.slice(0, 8).forEach(node => {
      const button = this.makeButton(node.data("label"), () => {
        this.searchResults.hidden = true;
        this.select(node);
        this.fitFocus();
      });
      button.append(control("small", "", node.data("typeLabel") || "本体类型"));
      this.searchResults.append(button);
    });
  }
  setPage(page, view = "all") {
    const elements = view === "ontology" ? ontologyElements(page.schema) : graphElements(page);
    if (this.view !== view) {
      this.showLabels = view !== "ontology";
      this.labelsButton.setAttribute("aria-pressed", String(this.showLabels));
    }
    if (this.view !== view || !this.page)
      this.layoutMode = "hierarchy";
    this.page = page;
    this.view = view;
    this.focusItem = null;
    this.cy.elements().remove();
    this.cy.add(this.transformElements(elements));
    this.resetTools();
    this.layout();
    this.updateZoom();
    return { nodes: this.cy.nodes().length, edges: this.cy.edges().length };
  }
  layout() {
    if (!this.cy.nodes().length) return;
    this.layoutButton.textContent = this.layoutMode === "network" ? "切换层级" : "切换网络";
    this.layoutButton.setAttribute("aria-label", this.layoutMode === "network" ? "当前网络布局，切换为层级布局" : "当前层级布局，切换为网络布局");
    // Dagre gives deterministic starting coordinates, including for the bounded CoSE pass.
    this.cy.layout({
      name: "dagre", rankDir: "LR",
      rankSep: 120,
      nodeSep: 28, edgeSep: 20, nodeDimensionsIncludeLabels: true, animate: false, fit: false,
      ranker: "network-simplex", padding: 50, ...this.layoutOptions,
    }).run();
    if (this.layoutMode === "network" && this.cy.nodes().length > 1) {
      this.cy.layout({
        name: "cose", animate: false, randomize: false, fit: false,
        nodeDimensionsIncludeLabels: true,
        nodeRepulsion: () => 650000,
        idealEdgeLength: () => 135,
        edgeElasticity: () => 150,
        nestingFactor: 1.2, gravity: 0.45,
        componentSpacing: 110, nodeOverlap: 30,
        numIter: 700, initialTemp: 120, coolingFactor: 0.96, minTemp: 1,
      }).run();
    }
    this.fit();
  }
  fit(items) {
    this.fitToFocus = false;
    this.cy.resize();
    if (!this.cy.nodes().length) return;
    this.cy.fit(items, 40);
    if (this.cy.zoom() > 1.05) this.cy.zoom({
      level: 1.05,
      renderedPosition: { x: this.container.clientWidth / 2, y: this.container.clientHeight / 2 },
    });
    this.cy.center(items);
  }
  isFullscreen() {
    const workspace = this.container.closest(".graph-workspace") || this.container.parentElement;
    return Boolean(workspace && document.fullscreenElement === workspace);
  }
  scheduleFit() {
    if (this.fitFrame !== null || this.destroyed) return;
    this.fitFrame = requestAnimationFrame(() => {
      this.fitFrame = null;
      if (!this.destroyed) {
        if (this.fitToFocus && this.focusItem) this.fitFocus();
        else this.fit();
      }
    });
  }
  zoom(factor) {
    this.cy.zoom({
      level: Math.max(this.cy.minZoom(), Math.min(this.cy.maxZoom(), this.cy.zoom() * factor)),
      renderedPosition: { x: this.container.clientWidth / 2, y: this.container.clientHeight / 2 },
    });
  }
  updateZoom() {
    this.cy.batch(() => {
      this.cy.edges().toggleClass("label-visible", this.showLabels && this.cy.zoom() >= 0.6);
      this.cy.edges().toggleClass("labels-hidden", !this.showLabels);
    });
    if (this.zoomLabel) this.zoomLabel.textContent = `${Math.round(this.cy.zoom() * 100)}%`;
  }
  select(item) {
    this.cy.elements().unselect();
    item.select();
    this.highlight(item);
    this.onSelect({ kind: item.isNode() ? "node" : "edge", ...item.data() });
  }
  highlight(item) {
    this.fitToFocus = false;
    this.focusItem = item;
    this.cy.elements().removeClass("neighbor").addClass("faded");
    const visible = item.isNode() ? item.closedNeighborhood() : item.union(item.connectedNodes());
    visible.removeClass("faded").addClass("neighbor");
    this.focusStatus.textContent = item.isNode()
      ? `${item.data("label")} · 当前页 ${visible.nodes().length - 1} 个直接关联节点`
      : `${item.data("displayLabel") || item.data("label")} · 查看所选关系的来源与依据`;
    this.focusButton.hidden = false;
    this.restoreButton.hidden = false;
    if (this.detailsButton) this.detailsButton.hidden = false;
    this.focusBar.hidden = false;
  }
  fitFocus() {
    if (!this.focusItem) return;
    const items = this.focusItem.isNode()
      ? this.focusItem.closedNeighborhood() : this.focusItem.union(this.focusItem.connectedNodes());
    this.fit(items);
    this.fitToFocus = true;
  }
  clearFocus() {
    this.fitToFocus = false;
    this.focusItem = null;
    this.cy.elements().removeClass("faded neighbor").unselect();
    this.resetTools();
    this.onSelect(null);
  }
  resetTools() {
    this.container.title = "";
    this.searchInput.value = "";
    this.searchResults.hidden = true;
    this.searchResults.replaceChildren();
    this.focusStatus.textContent = "点击节点突出直接关联；空白处恢复全图";
    this.focusButton.hidden = true;
    this.restoreButton.hidden = true;
    if (this.detailsButton) this.detailsButton.hidden = true;
    this.focusBar.hidden = true;
  }
  clear() {
    this.page = null;
    this.focusItem = null;
    this.cy.elements().remove();
    this.resetTools();
  }
  exportPNG() {
    return this.cy.png({ output: "blob", bg: "#f8fbf9", full: true, maxWidth: 3200, maxHeight: 2200, scale: 2 });
  }
  destroy() {
    this.destroyed = true;
    if (this.fitFrame !== null) cancelAnimationFrame(this.fitFrame);
    this.fitFrame = null;
    this.resizeObserver.disconnect();
    if (this.fullscreenHandler) document.removeEventListener("fullscreenchange", this.fullscreenHandler);
    this.container.removeEventListener("keydown", this.keyHandler);
    this.tools.remove();
    this.focusBar.remove();
    this.cy.destroy();
  }
}
