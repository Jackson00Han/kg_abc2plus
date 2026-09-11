export const $ = (id) => document.getElementById(id);
export const TYPE_LABELS = Object.freeze({
  Equipment: "设备",
  Risk: "风险",
  EquipmentClass: "设备类别",
  ProductFamily: "产品系列",
  ProductModel: "产品型号",
  Site: "场站",
  IndustrialSystem: "工业系统",
  InstalledAsset: "设备实例",
  Component: "部件",
  Symptom: "异常现象",
  FaultMode: "候选故障",
  DiagnosticCondition: "诊断条件",
  DiagnosticTest: "检查项目",
  MaintenanceAction: "维护活动",
  Observation: "观测记录",
  InspectionEvent: "巡检事件",
  SourceEdition: "来源版本",
});
export const PREDICATE_LABELS = Object.freeze({
  SUBTYPE_OF: "细分于",
  IN_FAMILY: "属于系列",
  CLASSIFIED_AS: "归类为",
  INSTANCE_OF: "对应型号",
  PART_OF: "组成于",
  CONTAINS: "包含部件",
  EXPOSED_TO: "存在风险",
  INSTALLED_AT: "安装于",
  LOCATED_AT: "位于",
  CONNECTS_TO: "电气连接",
  HAS_SYMPTOM: "出现现象",
  MAY_INDICATE: "可能关联",
  CHECKED_BY: "检查依据",
  ADDRESSED_BY: "相关维护",
  OBSERVED_ON: "观测对象",
  OBSERVES: "包含观测",
  DESCRIBES: "描述现象",
  APPLIES_TO: "适用于",
});
export const SOURCE_LABELS = Object.freeze({
  OFFICIAL_PUBLICATION: "官方手册节选",
  CURATED_REFERENCE: "项目整理参考",
  SYNTHETIC_FIELD_RECORD: "合成设备记录",
  USER_UPLOAD: "用户上传",
});
export const ORIGIN_LABELS = Object.freeze({
  EXPERT_IMPORT: "项目人工导入",
  EXPERT_CREATED: "人工建立",
  LLM_EXTRACTED: "模型自动抽取",
  RULE_DERIVED: "规则衍生",
  FIXTURE: "测试样例",
});
export const STATUS_LABELS = Object.freeze({
  CANDIDATE: "待复核",
  APPROVED: "已批准",
  PUBLISHED: "已发布",
  QUARANTINED: "待处理",
  REJECTED: "已拒绝",
  SUPERSEDED: "已替代",
  SUCCEEDED: "已完成",
  FAILED: "已失败",
  RUNNING: "处理中",
  PREPARING: "准备中",
});
export function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
export function tag(text, kind = "") {
  return element("span", `tag ${kind}`, text);
}
export function button(text, action, className = "button secondary") {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}
// Each visible operation owns one button; transport requests never create UI.
const pendingButtons = new Map();
export function beginButtonFeedback(button, {disable = true} = {}) {
  if (!button) return () => {};
  let entry = pendingButtons.get(button);
  if (!entry) {
    entry = {count:0, disabled:Boolean(button.disabled), disable:false};
    pendingButtons.set(button, entry);
  }
  entry.count++;
  entry.disable ||= disable;
  if (disable) button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  button.classList?.add('button-pending');
  let finished = false;
  return () => {
    if (finished || pendingButtons.get(button) !== entry) return;
    finished = true;
    if (--entry.count) return;
    pendingButtons.delete(button);
    button.removeAttribute?.('aria-busy');
    button.classList?.remove('button-pending');
    if (entry.disable) button.disabled = entry.disabled;
  };
}
export function clearButtonFeedback(root = document) {
  for (const [button, entry] of pendingButtons) {
    if (!root.contains(button)) continue;
    pendingButtons.delete(button);
    button.removeAttribute('aria-busy');
    button.classList?.remove('button-pending');
    if (entry.disable) button.disabled = entry.disabled;
  }
}
export function clear(node) {
  node.replaceChildren();
  return node;
}
export function shortId(value) {
  return value ? String(value).slice(0, 8) : "—";
}
export function dateLabel(value) {
  if (!value) return "未记录日期";
  const d = new Date(value);
  return Number.isNaN(d.valueOf())
    ? "未记录日期"
    : d.toLocaleDateString("zh-CN");
}
export function sourceTag(kind) {
  return tag(
    SOURCE_LABELS[kind] || "来源资料",
    kind === "OFFICIAL_PUBLICATION"
      ? "official"
      : kind === "USER_UPLOAD"
        ? "secondary"
        : "",
  );
}
export function authorityTag(value) {
  return tag(
    value === "AUTHORITATIVE" ? "权威导入" : "次权威",
    value === "AUTHORITATIVE" ? "official" : "secondary",
  );
}
export function externalURL(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}
export function sourceLink(value, label = "查看来源 ↗") {
  const href = externalURL(value);
  if (!href) return null;
  const link = element("a", "quiet-link", label);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}
export function parseAssetKey(value) {
  const text = String(value || "").trim();
  if (!text) return null;
  const key = text
    .replace(/^industrial:/i, "")
    .replace(/^asset-/i, "")
    .toLowerCase();
  if (!/^[a-z][a-z0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*$/.test(key) || key.length > 56)
    throw new Error("请填写稳定设备编码，例如 BKT-A01。");
  return `asset-${key}`;
}
export function assetLabel(value) {
  return String(value)
    .replace(/^asset-/, "")
    .toUpperCase();
}
export function graphScope(family = "", asset = "") {
  const key = parseAssetKey(asset);
  return {
    family: family || null,
    asset_keys: key ? [key] : [],
    include_references: true,
    source_kinds: [],
  };
}
export function exactEvidenceParts(citation, evidence) {
  const text = String(citation.chunk_text || "");
  const chars = Array.from(text);
  const start = evidence.char_start - citation.char_start,
    end = evidence.char_end - citation.char_start;
  if (
    !Number.isInteger(start) ||
    !Number.isInteger(end) ||
    start < 0 ||
    end > chars.length ||
    end <= start ||
    chars.slice(start, end).join("") !== evidence.quoted_text
  )
    throw new Error("引用位置与来源片段不一致，请刷新资料。");
  return [
    chars.slice(0, start).join(""),
    chars.slice(start, end).join(""),
    chars.slice(end).join(""),
  ];
}
export function evidenceQuote(citation, evidence) {
  const pre = element("pre", "source-quote");
  const parts = exactEvidenceParts(citation, evidence);
  pre.append(
    document.createTextNode(parts[0]),
    element("mark", "", parts[1]),
    document.createTextNode(parts[2]),
  );
  return pre;
}
let toastTimer;
export function toast(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 5000);
}
export function message(text = "", error = false) {
  const n = $("global-message");
  n.textContent = text;
  n.hidden = !text;
  n.classList.toggle("error", error);
}
export function empty(text) {
  return element("div", "empty-state", text);
}
export function metadata(items) {
  const node = element("div", "metadata");
  for (const item of items.filter(
    (v) => v !== null && v !== undefined && v !== "",
  ))
    node.append(element("span", "", item));
  return node;
}
export function safeError(error) {
  if (error?.name === "AbortError" || error?.name === "StaleResponse")
    return "";
  return error?.message || "请求未完成，请刷新后重试。";
}

export class StaleResponse extends Error {
  constructor() {
    super("视图已变化");
    this.name = "StaleResponse";
  }
}
export class WorkbenchClient {
  constructor(fetcher = globalThis.fetch.bind(globalThis)) {
    this.fetcher = fetcher;
    this.epoch = 0;
    this.session = null;
    this.controllers = new Set();
    this.personaId = null;
    this.renewal = null;
    this.pageGeneration = null;
    this.environmentConfigured = false;
    this.environmentError = null;
  }
  configureBootstrap(bootstrap) {
    const reset = bootstrap?.local_reset;
    const generation = reset?.enabled ? reset.generation : null;
    if (this.environmentConfigured && generation !== this.pageGeneration) {
      this.blockEnvironment("PLAYGROUND_RESET_STALE");
      throw this.environmentError;
    }
    if (generation !== null && (typeof generation !== "string" || !generation))
      throw new Error("服务未返回有效的环境版本，请刷新后重试。");
    this.pageGeneration = generation;
    this.environmentConfigured = true;
    if (reset?.enabled && reset.state !== "READY")
      this.blockEnvironment(reset.state === "FAILED"
        ? "PLAYGROUND_RESET_FAILED" : "PLAYGROUND_RESET_RUNNING");
    // A reload establishes a new page generation. A persona change or a
    // repeated bootstrap must never silently acknowledge a reset for old forms.
  }
  blockEnvironment(code) {
    const error = new Error("知识环境已更新或正在恢复，请刷新页面后继续。");
    error.code = code;
    error.status = code === "PLAYGROUND_RESET_STALE" ? 409 : 503;
    this.environmentError = error;
    for (const controller of this.controllers) controller.abort();
  }
  clear() {
    this.epoch++;
    for (const item of this.controllers) item.abort();
    this.controllers.clear();
    this.session = null;
    this.renewal = null;
    return this.epoch;
  }
  async selectPersona(personaId) {
    const epoch = this.clear();
    this.personaId = personaId;
    return this.issueSession(personaId, epoch);
  }
  async issueSession(personaId, epoch) {
    const response = await this.fetcher("/playground/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ persona_id: personaId }),
      cache: "no-store",
    });
    if (epoch !== this.epoch) throw new StaleResponse();
    if (!response.ok) throw new Error("无法建立当前身份的会话。");
    const session = await response.json();
    if (epoch !== this.epoch) throw new StaleResponse();
    if (session.identity?.id !== personaId || !session.access_token)
      throw new Error("身份响应无效。");
    this.session = session;
    return session;
  }
  async request(path, body, options = {}) {
    const epoch = this.epoch;
    if (this.environmentError) throw this.environmentError;
    if (!this.session) throw new Error("请先选择查看身份。");
    if (this.session.expires_at * 1000 < Date.now() + 30000) {
      this.renewal ||= this.issueSession(this.personaId, epoch).finally(() => {
        if (epoch === this.epoch) this.renewal = null;
      });
      await this.renewal;
    }
    if (epoch !== this.epoch) throw new StaleResponse();
    if (this.environmentError) throw this.environmentError;
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (options.signal?.aborted) controller.abort();
    else options.signal?.addEventListener("abort", abort, { once: true });
    this.controllers.add(controller);
    try {
      const method = (options.method || (body === undefined ? "GET" : "POST")).toUpperCase();
      const headers = {};
      for (const [name, value] of new Headers(options.headers || {})) {
        if (!["authorization", "x-playground-generation", "content-type"].includes(name))
          headers[name] = value;
      }
      headers.Authorization = `Bearer ${this.session.access_token}`;
      if (body !== undefined) headers["Content-Type"] = "application/json";
      if (this.pageGeneration && path.startsWith("/v1/") && !["GET", "HEAD"].includes(method))
        headers["X-Playground-Generation"] = this.pageGeneration;
      const response = await this.fetcher(path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        cache: "no-store",
      });
      if (epoch !== this.epoch) throw new StaleResponse();
      let payload;
      try { payload = await response.json(); } catch (error) {
        if (epoch !== this.epoch) throw new StaleResponse();
        if (response.ok) throw error;
        payload = {};
      }
      if (epoch !== this.epoch) throw new StaleResponse();
      if (this.environmentError) throw this.environmentError;
      if (!response.ok) {
        const code =
          payload?.error?.code || payload?.code || `HTTP_${response.status}`;
        const error = new Error(
          code === "GRAPH_VIEW_CHANGED" || code === "graph_view_changed"
            ? "图谱版本或可见范围已变化，请刷新图谱。"
            : response.status === 403
              ? "当前身份没有执行此操作的权限。"
              : response.status === 401
                ? "会话已失效，请刷新身份。"
                : {
                    invalid_request: "请求参数无效，请检查输入与范围。",
                    dependency_unavailable: "服务依赖暂时不可用，请稍后刷新。",
                    dependency_timeout: "处理超时，请稍后重试或查看任务记录。",
                    construction_ingestion_failed:
                      "来源入库或向量化失败，请查看任务记录。",
                    construction_input_limit:
                      "资料超出本地构建的处理范围，请缩短正文、标题或分批上传。",
                  }[code] ||
                  payload?.error?.message ||
                  payload?.message ||
                  `请求未完成（${code}）`,
        );
        error.code = code;
        error.status = response.status;
        error.payload = payload;
        const issue = payload?.publication_issue;
        if (response.status === 409 && issue && typeof issue.message === "string" &&
            issue.message.length <= 256 && Array.isArray(issue.targets) && issue.targets.length <= 50) {
          error.publicationIssue = issue;
          error.message = issue.message;
        }
        if (["PLAYGROUND_RESET_STALE", "PLAYGROUND_RESET_RUNNING", "PLAYGROUND_RESET_FAILED"].includes(code)) {
          this.blockEnvironment(code);
          error.message = this.environmentError.message;
        }
        throw error;
      }
      return payload;
    } finally {
      this.controllers.delete(controller);
      options.signal?.removeEventListener("abort", abort);
    }
  }
  requestOptions(path, options = {}) {
    // The native governance modules use fetch-style JSON options; route those
    // through the selected identity instead of issuing a separate admin token.
    const body = typeof options.body === "string" ? JSON.parse(options.body) : options.body;
    return this.request(path, body, options);
  }
  hasScope(scope) {
    return this.session?.identity?.scopes?.includes(scope) || false;
  }
}
