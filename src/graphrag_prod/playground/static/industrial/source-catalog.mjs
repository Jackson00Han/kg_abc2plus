import {
  element, button, clear, tag, metadata, sourceTag, sourceLink,
  assetLabel, dateLabel, empty, safeError,
} from "./core.mjs";

const FAMILIES = Object.freeze({
  "canalis-kt": "Canalis KT · 母线槽",
  "evopact-hvx-up24": "EvoPacT HVX · 真空断路器",
});
const sourceKey = source => `${source.document_id}:${source.version_id}`;

/** Industrial metadata supplements an authorized document/version, never replaces it. */
export function mergeSourcePage(items, industrialSources = []) {
  const industrial = new Map(industrialSources.map(source => [sourceKey(source), source]));
  return items.map(source => {
    const supplement = industrial.get(sourceKey(source));
    return {
      ...source,
      family: supplement?.family || null,
      asset_keys: [...(supplement?.asset_keys || [])],
      source_kind: supplement?.source_kind || null,
      published_at: supplement?.published_at || null,
    };
  });
}

/** Original-document browsing; graph publication and source visibility stay separate. */
export class SourceCatalog {
  constructor({ client, list, summary, onSelect = null }) {
    this.client = client;
    this.list = list;
    this.summary = summary;
    this.onSelect = onSelect;
    this.serial = 0;
    this.detailSerial = 0;
    this.family = null;
    this.rows = [];
    this.after = null;
    this.history = [];
    this.next = null;
    this.dialog = null;
    this.pager = element("div", "form-footer");
    this.pager.setAttribute("aria-label", "来源资料分页");
    this.previousButton = button("← 上一页资料", () => {
      if (!this.history.length) return;
      const previous = this.history.slice(0, -1);
      void this.readPage(this.history.at(-1), previous);
    });
    this.nextButton = button("下一页资料 →", () => {
      if (this.next) void this.readPage(this.next, [...this.history, this.after]);
    });
    this.pager.append(this.previousButton, this.nextButton);
    list.insertAdjacentElement("afterend", this.pager);
    this.summary.setAttribute("role", "status");
    this.reset();
  }
  reset() {
    this.serial++;
    this.detailSerial++;
    this.dialog?.close();
    this.dialog = null;
    this.rows = [];
    this.after = null;
    this.history = [];
    this.next = null;
    this.family = null;
    clear(this.list);
    this.summary.textContent = "尚未加载当前身份的来源资料。";
    this.previousButton.disabled = true;
    this.nextButton.disabled = true;
    this.pager.hidden = true;
  }
  load({ family = null } = {}) {
    this.family = FAMILIES[family] ? family : null;
    return this.readPage(null, []);
  }
  async readPage(after, history) {
    const serial = ++this.serial, identity = this.client.epoch, family = this.family;
    clear(this.list);
    this.rows = [];
    this.next = null;
    this.summary.textContent = "正在读取当前身份可见的原始文档…";
    this.previousButton.disabled = true;
    this.nextButton.disabled = true;
    this.pager.hidden = true;
    try {
      let rows, hasMore, nextAfter = null, metadataNote = "";
      if (family) {
        const result = await this.client.request("/v1/industrial/sources:query", {
          family, asset_keys: [], limit: 100,
        });
        rows = result.sources;
        hasMore = result.has_more;
      } else {
        // Failure of the optional product catalogue must not hide ordinary uploads.
        const [documents, catalogue] = await Promise.allSettled([
          this.client.request("/v1/knowledge/sources:query", { limit: 30, ...(after ? { after } : {}) }),
          this.client.request("/v1/industrial/sources:query", { family: null, asset_keys: [], limit: 100 }),
        ]);
        if (documents.status === "rejected") throw documents.reason;
        const supplement = catalogue.status === "fulfilled" ? catalogue.value.sources : [];
        rows = mergeSourcePage(documents.value.items, supplement);
        hasMore = documents.value.has_more;
        nextAfter = documents.value.next_after;
        if (catalogue.status === "rejected") metadataNote = " · 产品范围信息暂未载入";
        else if (catalogue.value.has_more) metadataNote = " · 部分产品范围信息未在本次目录中返回";
      }
      if (serial !== this.serial || identity !== this.client.epoch) return null;
      this.rows = rows;
      this.after = after;
      this.history = history;
      this.next = nextAfter;
      this.summary.textContent = family
        ? `${FAMILIES[family]} · 当前可见 ${rows.length} 份资料${hasMore ? " · 目录还有更多资料，请切到全部资料分页查看" : ""} · 资料入库不等于知识已发布`
        : `第 ${history.length + 1} 页 · ${rows.length} 份可访问原始文档${hasMore ? " · 还有更多资料" : " · 已到列表末尾"}${metadataNote} · 资料入库不等于知识已发布`;
      if (!rows.length) this.list.append(empty(family
        ? "当前产品族没有可见资料。普通上传文档可在“全部资料”中查看。"
        : "当前身份没有可访问的活动原始文档。"));
      for (const source of rows) this.list.append(this.renderCard(source));
      this.previousButton.disabled = !history.length;
      this.nextButton.disabled = !nextAfter;
      this.pager.hidden = Boolean(family) || (!history.length && !nextAfter);
      return { sources: rows, has_more: hasMore, next_after: nextAfter };
    } catch (error) {
      if (serial !== this.serial || identity !== this.client.epoch) return null;
      const text = safeError(error);
      if (text) {
        this.summary.textContent = `来源资料读取失败：${text}`;
        clear(this.list).append(empty(text));
      }
      return null;
    }
  }
  renderCard(source) {
    const card = element("article", "source-card");
    const header = element("div", "source-card-header");
    const mark = element("div", "source-mark", "▤");
    mark.setAttribute("aria-hidden", "true");
    header.append(mark, source.source_kind ? sourceTag(source.source_kind) : tag("原始文档"));
    card.append(
      header,
      element("h3", "source-card-title", source.title || "未命名文档"),
      element("p", "source-card-scope", source.family ? FAMILIES[source.family] || source.family : "通用资料 · 产品范围未标注"),
    );
    if (source.asset_keys?.length)
      card.append(element("p", "source-card-scope", `适用设备：${source.asset_keys.map(assetLabel).join("、")}`));
    const summary = element("div", "source-card-summary");
    const facts = metadata([
      source.version_number ? `第 ${source.version_number} 版` : null,
      `${source.chunk_count} 个来源片段`,
      source.published_at ? dateLabel(source.published_at) : null,
    ]);
    facts.className = "metadata source-card-facts";
    summary.append(facts);
    if (typeof source.has_published_knowledge === "boolean")
      summary.append(element("p", "source-card-status", source.has_published_knowledge
        ? "包含当前可见的已发布知识" : "尚无当前可见的已发布知识"));
    card.append(summary);
    const details = element("details", "source-card-details");
    details.append(
      element("summary", "", "来源信息"),
      metadata([
        source.title ? `原始标题：${source.title}` : null,
        source.source_name ? `来源名称：${source.source_name}` : null,
        source.canonical_uri ? `来源地址：${source.canonical_uri}` : null,
      ]),
      element("p", "record-key", `文档 ${source.document_id}\n版本 ${source.version_id}`),
    );
    card.append(details);
    const actions = element("div", "evidence-actions source-card-actions");
    actions.append(button("查看原文 →", () => this.open(source), "text-button"));
    if (this.onSelect)
      actions.append(button("查看相关知识", () => this.select(source, "graph"), "text-button"));
    card.append(actions);
    return card;
  }
  async select(source, action) {
    const identity = this.client.epoch;
    try {
      await this.onSelect?.({ ...source, asset_keys: [...(source.asset_keys || [])] }, action);
      if (identity === this.client.epoch) this.dialog?.close();
    } catch (error) {
      if (identity !== this.client.epoch) return;
      const text = safeError(error);
      if (text) this.summary.textContent = text;
    }
  }
  async open(source, ordinal = 0) {
    this.dialog?.close();
    const modal = document.createElement("dialog");
    this.dialog = modal;
    modal.className = "source-dialog";
    modal.setAttribute("aria-label", "原始文档与来源片段");
    const heading = element("div", "dialog-heading");
    heading.append(element("h2", "", source.title || "原始文档"), button("关闭", () => modal.close()));
    const content = element("div", "source-dialog-content");
    modal.append(heading, content);
    document.body.append(modal);
    modal.addEventListener("close", () => {
      if (this.dialog === modal) { this.dialog = null; this.detailSerial++; }
      modal.remove();
    });
    modal.showModal();
    await this.readChunk(source, ordinal, modal, content);
  }
  async readChunk(source, ordinal, modal, content) {
    const serial = ++this.detailSerial, identity = this.client.epoch;
    clear(content).append(element("p", "muted", "正在授权读取指定版本的原文…"));
    try {
      const result = await this.client.request("/v1/knowledge/sources:read", {
        document_id: source.document_id, version_id: source.version_id, ordinal,
      });
      if (serial !== this.detailSerial || identity !== this.client.epoch || !modal.open) return;
      if (result.document_id !== source.document_id || result.version_id !== source.version_id || result.ordinal !== ordinal)
        throw new Error("资料版本或片段位置已变化，请刷新来源资料后重新打开。");
      clear(content);
      content.append(metadata([
        result.source_name, `第 ${result.version_number} 版`,
        result.section, result.page_number ? `原文第 ${result.page_number} 页` : null,
        `片段 ${result.ordinal + 1} / ${result.chunk_count}`,
        `字符 ${result.char_start}–${result.char_end}`,
      ]), element("pre", "source-quote", result.text));
      const provenance = element("details", "source-note");
      provenance.append(element("summary", "", "来源、版本与原文位置"),
        element("p", "", result.canonical_uri),
        element("p", "record-key", `文档 ${result.document_id}\n版本 ${result.version_id}\nChunk ${result.chunk_id}\n片段校验 ${result.checksum}`));
      const link = sourceLink(result.canonical_uri);
      if (link) provenance.append(link);
      content.append(provenance);
      const nav = element("div", "form-footer");
      const previous = button("← 上一片段", () => this.readChunk(source, ordinal - 1, modal, content));
      const next = button("下一片段 →", () => this.readChunk(source, ordinal + 1, modal, content));
      previous.disabled = ordinal === 0;
      next.disabled = ordinal + 1 >= result.chunk_count;
      nav.append(previous, next);
      if (this.onSelect) {
        nav.append(button("查看相关知识", () => this.select({ ...source, ...result }, "graph")));
        nav.append(button("在此资料中检索", () => this.select({ ...source, ...result }, "search")));
      }
      content.append(nav);
    } catch (error) {
      if (serial !== this.detailSerial || identity !== this.client.epoch || !modal.open) return;
      const text = error.status === 409
        ? "来源版本、完整性或访问权限已变化，请刷新资料列表。" : safeError(error);
      if (text) clear(content).append(empty(text));
    }
  }
  destroy() { this.reset(); this.pager.remove(); }
}
