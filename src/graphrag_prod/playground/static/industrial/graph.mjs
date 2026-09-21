import { graphElements, ontologyElements, typeColors, typeLabel } from "./graph-model.mjs";

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
    this.layoutMode = "network";
    this.showLabels = false;
    this.focusItem = null;
    this.fitFrame = null;
    this.destroyed = false;
    this.fullscreenActive = false;
    this.cy = globalThis.cytoscape({
      container,
      elements: [],
      minZoom: 0.05,
      maxZoom: 2.8,
      wheelSensitivity: 0.16,
      boxSelectionEnabled: false,
      selectionType: "single",
      style: [
        {
          selector: "node",
          style: {
            shape: "round-rectangle", width: 156, height: 50,
            "background-color": "data(fill)",
            "border-color": "data(border)", "border-width": 1.2, "border-opacity": 0.7,
            label: "data(displayLabel)", color: "#e7f0f6",
            "font-family": "-apple-system, PingFang SC, Microsoft YaHei, sans-serif",
            "font-size": 13, "font-weight": 500,
            "text-wrap": "ellipsis", "text-max-width": 140,
            "text-valign": "center", "text-halign": "center",
            "text-outline-width": 0, "line-height": 1.6,
            padding: 2, "overlay-opacity": 0, "text-events": "yes",
            "z-index": 20, "z-index-compare": "manual",
          },
        },
        { selector: "node.schema", style: { width: 176, height: 76, "text-wrap": "wrap", "text-max-width": 168 } },
        { selector: "node.asset", style: { "border-width": 2, "font-weight": 600 } },
        {
          selector: "edge",
          style: {
            "curve-style": "bezier", "control-point-step-size": 65,
            width: 1.25, "line-color": "#526b82", "opacity": 0.7,
            "target-arrow-color": "#6b859a", "target-arrow-shape": "triangle",
            "arrow-scale": 0.8, label: "",
            "font-size": 12, color: "#b4c8d8",
            "text-background-color": "#0e1b2a", "text-background-opacity": 0.96,
            "text-background-padding": 4, "text-background-shape": "roundrectangle",
            "text-rotation": "autorotate", "text-margin-y": -2,
            "overlay-opacity": 0, "underlay-padding": 9, "z-index": 1, "z-index-compare": "manual",
          },
        },
        { selector: "edge.secondary", style: { "line-style": "dashed", "line-dash-pattern": [6, 4] } },
        { selector: "edge.authoritative", style: { "line-color": "#568f9b", "target-arrow-color": "#568f9b" } },
        { selector: "edge.schema-edge", style: { "line-style": "solid", "line-color": "#708ba4" } },
        { selector: "edge.label-visible", style: { label: "data(displayLabel)" } },
        { selector: "edge.labels-hidden", style: { label: "" } },
        { selector: "node.overview", style: { shape: "ellipse", width: 42, height: 42,
          label: "", "border-width": 3, "background-color": "data(border)", "background-opacity": 0.5 } },
        { selector: "node.overview.hub", style: { width: 62, height: 62, label: "data(overviewLabel)",
          "text-valign": "bottom", "text-margin-y": 18, "text-max-width": 550,
          "text-background-color": "#0d1726", "text-background-opacity": 0.85, "text-background-padding": 5 } },
        { selector: ".type-hidden", style: { display: "none" } },
        { selector: ".faded", style: { opacity: 0.12 } },
        {
          selector: "node.neighbor", style: { "border-width": 2, "border-color": "data(border)" },
        },
        {
          selector: "edge.neighbor", style: {
            width: 2.5, opacity: 1, "line-color": "#71ddc8", "target-arrow-color": "#71ddc8",
            color: "#c3f3e9", label: "data(displayLabel)", "z-index": 5,
          },
        },
        {
          selector: "node:selected", style: {
            "border-width": 2.5, "border-color": "#e0fff7", "background-color": "data(fill)",
            "underlay-color": "#62ddba", "underlay-opacity": 0.15, "underlay-padding": 10,
            "font-weight": 700, "z-index": 30,
          },
        },
        {
          selector: "edge:selected", style: {
            width: 3.5, opacity: 1, "line-color": "#71ddc8", "target-arrow-color": "#71ddc8",
            color: "#c3f3e9", label: "data(displayLabel)", "font-weight": 700,
            "text-background-color": "#193d3d", "z-index": 10,
          },
        },
        { selector: "edge.schema-edge.labels-hidden:selected", style: { label: "data(displayLabel)" } },
      ],
    });
    this.mountTools();
    this.groupLayer = control("div", "graph-group-layer");
    this.groupLayer.setAttribute("aria-hidden", "true");
    container.prepend(this.groupLayer);
    this.tooltip = control("div", "graph-hover-card");
    this.tooltip.hidden = true;
    container.append(this.tooltip);
    this.hideTooltip = () => { this.tooltip.hidden = true; };
    container.addEventListener("pointerleave", this.hideTooltip);
    this.cy.on("tap", "node, edge", event => this.select(event.target));
    this.cy.on("tap", event => {
      if (event.target === this.cy) this.clearFocus();
    });
    this.cy.on("zoom", () => this.updateZoom());
    this.cy.on("pan zoom resize", () => this.drawGroups());
    this.cy.on("mouseover", "node, edge", event => {
      const item = event.target, data = item.data();
      this.tooltip.replaceChildren(control("strong", "", data.label),
        control("span", "", data.typeLabel || "关系"),
        control("small", "", item.isNode() ? "点击查看关联与来源" : "点击追溯关系依据"));
      const position = item.isNode() ? item.renderedPosition() : item.renderedMidpoint();
      this.tooltip.style.left = `${Math.max(12, Math.min(position.x + 18, this.container.clientWidth - 270))}px`;
      this.tooltip.style.top = `${Math.max(12, Math.min(position.y + 25, this.container.clientHeight - 110))}px`;
      this.tooltip.hidden = false;
    });
    this.cy.on("mouseout", "node, edge", () => { this.tooltip.hidden = true; });
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
      this.layoutMode = this.layoutMode === "network" ? "grouped" : "network";
      this.layout();
    }, "切换已生成的关系网络与类型分组");
    this.labelsButton = this.makeButton("关系标签", () => {
      this.showLabels = !this.showLabels;
      this.labelsButton.setAttribute("aria-pressed", String(this.showLabels));
      this.updateZoom();
    }, "显示或隐藏关系标签，缩小时自动精简");
    this.labelsButton.setAttribute("aria-pressed", "false");
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
    this.zoomLabel = control("span", "graph-zoom-label");
    actions.prepend(this.zoomLabel);
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
      this.detailsButton = this.makeButton("查看来源 →", () => this.onViewDetails(), "查看所选内容的详情与来源");
      this.detailsButton.hidden = true;
    }
    this.focusBar.append(this.focusStatus);
    if (this.detailsButton) this.focusBar.append(this.detailsButton);
    this.focusBar.append(this.focusButton, this.restoreButton);
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
    const matches = this.cy.nodes(":visible").filter(node =>
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
    const artifact = page.visualization;
    if (!artifact || artifact.status !== "READY")
      throw new Error("该版本的图谱展示文件暂不可用，请稍后刷新。");
    this.page = page;
    this.view = view;
    this.selectedType = null;
    this.layoutMode = view === "ontology" ? "network" : !page.edges.length ? "grouped" : artifact.default_layout || "network";
    this.focusItem = null;
    this.cy.elements().remove();
    this.cy.add(this.transformElements(elements));
    this.resetTools();
    this.layout();
    return { nodes: this.cy.nodes().length, edges: this.cy.edges().length };
  }
  layoutData() {
    const artifact = this.page?.visualization;
    if (this.view === "ontology") {
      const ontology = artifact?.ontology;
      return ontology?.layouts?.[this.layoutMode] || ontology?.[this.layoutMode] || ontology;
    }
    return artifact?.layouts?.[this.layoutMode];
  }
  layout() {
    this.hideTooltip();
    if (!this.cy.nodes().length) return;
    const layout = this.layoutData();
    if (!layout?.positions || this.cy.nodes().some(node => {
      const point = layout.positions[node.id()];
      return !point || !Number.isFinite(point.x) || !Number.isFinite(point.y);
    })) throw new Error("图谱展示文件缺少有效坐标，请重新生成该版本展示文件。");
    this.layoutButton.textContent = this.layoutMode === "network" ? "类型分组" : "关系网络";
    this.layoutButton.setAttribute("aria-label", `切换到${this.layoutButton.textContent}`);
    // Applying saved positions is deliberately the only browser layout operation.
    this.cy.layout({ name: "preset", positions: layout.positions, animate: false, fit: false }).run();
    this.groupLayer.replaceChildren();
    this.groups = this.layoutMode === "grouped" ? layout.groups || [] : [];
    for (const group of this.groups) {
      const box = control("div", "graph-type-region");
      box.style.setProperty("--group-color", typeColors(group.type)[1]);
      box.append(control("span", "", `${typeLabel(group.type, this.page.schema)} · ${group.count}`));
      this.groupLayer.append(box);
    }
    this.fit();
    this.updateZoom();
  }
  drawGroups() {
    if (!this.groupLayer || !this.groups) return;
    const zoom = this.cy.zoom(), pan = this.cy.pan();
    this.groups.forEach((group, index) => {
      const box = this.groupLayer.children[index];
      if (!box) return;
      box.hidden = Boolean(this.selectedType && this.selectedType !== group.type);
      Object.assign(box.style, { left: `${group.x * zoom + pan.x}px`, top: `${group.y * zoom + pan.y}px`,
        width: `${group.width * zoom}px`, height: `${group.height * zoom}px` });
    });
  }
  filterType(type = null) {
    this.clearFocus();
    this.selectedType = type;
    this.cy.batch(() => {
      this.cy.nodes().forEach(node => node.toggleClass("type-hidden", Boolean(type && node.data("type") !== type)));
      this.cy.edges().forEach(edge => edge.toggleClass("type-hidden", edge.connectedNodes().some(node => node.hasClass("type-hidden"))));
    });
    this.fit();
    this.drawGroups();
    return { nodes: this.cy.nodes(":visible").length, edges: this.cy.edges(":visible").length };
  }
  fit(items) {
    this.fitToFocus = false;
    this.cy.resize();
    if (!this.cy.nodes().length) return;
    items ||= this.cy.elements(":visible");
    this.cy.fit(items, 48);
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
    const level = this.cy.zoom();
    const overview = this.view !== "ontology" && level < 0.35;
    // Semantic zoom changes marks and labels only; saved coordinates stay intact.
    this.cy.batch(() => {
      this.cy.nodes().toggleClass("overview", overview);
      this.cy.nodes().style("font-size", Math.max(13, Math.min(64, 11 / level)));
      this.cy.edges().toggleClass("label-visible", this.showLabels && this.cy.zoom() >= 0.65);
      this.cy.edges().toggleClass("labels-hidden", !this.showLabels);
    });
    if (this.zoomLabel) this.zoomLabel.textContent = `${Math.round(this.cy.zoom() * 100)}%`;
  }
  select(item) {
    this.hideTooltip();
    this.cy.elements().unselect();
    item.select();
    this.highlight(item);
    this.onSelect({ kind: item.isNode() ? "node" : "edge", ...item.data() });
  }
  highlight(item) {
    this.fitToFocus = false;
    this.focusItem = item;
    this.cy.elements().removeClass("neighbor").addClass("faded");
    const visible = item.isNode() ? item.closedNeighborhood().filter(":visible") : item.union(item.connectedNodes()).filter(":visible");
    visible.removeClass("faded").addClass("neighbor");
    this.focusStatus.textContent = item.isNode()
      ? `${item.data("label")} · 当前范围 ${visible.nodes().length - 1} 个直接关联节点`
      : `${item.data("displayLabel") || item.data("label")} · 查看所选关系的来源与依据`;
    this.focusButton.hidden = false;
    this.restoreButton.hidden = false;
    if (this.detailsButton) this.detailsButton.hidden = false;
    this.focusBar.hidden = false;
  }
  fitFocus() {
    if (!this.focusItem) return;
    const items = this.focusItem.isNode()
      ? this.focusItem.closedNeighborhood().filter(":visible") : this.focusItem.union(this.focusItem.connectedNodes()).filter(":visible");
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
    if (this.tooltip) this.tooltip.hidden = true;
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
    this.groups = [];
    this.groupLayer.replaceChildren();
    this.tooltip.hidden = true;
    this.cy.elements().remove();
    this.resetTools();
  }
  exportPNG() {
    return this.cy.png({ output: "blob", bg: "#0d1726", full: false, maxWidth: 3200, maxHeight: 2200, scale: 2 });
  }
  destroy() {
    this.destroyed = true;
    if (this.fitFrame !== null) cancelAnimationFrame(this.fitFrame);
    this.fitFrame = null;
    this.resizeObserver.disconnect();
    if (this.fullscreenHandler) document.removeEventListener("fullscreenchange", this.fullscreenHandler);
    this.container.removeEventListener("keydown", this.keyHandler);
    this.container.removeEventListener("pointerleave", this.hideTooltip);
    this.tools.remove();
    this.focusBar.remove();
    this.groupLayer.remove();
    this.tooltip.remove();
    this.cy.destroy();
  }
}
