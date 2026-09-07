import {
  $,
  element,
  button,
  clear,
  tag,
  metadata,
  authorityTag,
  TYPE_LABELS,
  PREDICATE_LABELS,
  ORIGIN_LABELS,
  STATUS_LABELS,
  parseAssetKey,
  dateLabel,
  shortId,
  toast,
  empty,
  safeError,
} from "./core.mjs";

const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;
const MIME = {
  txt: "text/plain",
  md: "text/markdown",
  csv: "text/csv",
  json: "application/json",
};
const GROUPS = {
  public: "公开资料",
  engineering: "工程人员",
  maintenance: "维护人员",
};
async function digest(bytes) {
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}
function base64(bytes) {
  let text = "";
  for (let i = 0; i < bytes.length; i += 16384)
    text += String.fromCharCode(...bytes.subarray(i, i + 16384));
  return btoa(text);
}
function savedOperation(principal, fingerprint) {
  const storageKey = `industrial-upload-operations-v1:${principal}`;
  let rows = [];
  try {
    rows = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
    if (!Array.isArray(rows)) rows = [];
  } catch {}
  let item = rows.find((r) => r.fingerprint === fingerprint);
  if (!item) {
    const id = crypto.randomUUID();
    item = {
      fingerprint,
      operation_key: `industrial-upload-${id}`,
      canonical_uri: `industrial-upload://industrial-schneider-demo/${id}`,
    };
    rows.push(item);
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(rows.slice(-25)));
    } catch {}
  }
  return item;
}
function forgetOperation(principal, fingerprint) {
  const key = `industrial-upload-operations-v1:${principal}`;
  try {
    const rows = JSON.parse(sessionStorage.getItem(key) || "[]");
    sessionStorage.setItem(
      key,
      JSON.stringify(rows.filter((r) => r.fingerprint !== fingerprint)),
    );
  } catch {}
}
function recordTitle(item) {
  if (item.entity)
    return `${TYPE_LABELS[item.entity.entity_type] || item.entity.entity_type} · ${item.entity.canonical_name}`;
  return `${item.subject?.canonical_name || "未知实体"} — ${PREDICATE_LABELS[item.predicate] || item.predicate} → ${item.object_entity?.canonical_name || item.literal_value || ""}`;
}
function constructionSummary(chunks, mode) {
  const stats = {};
  const records = { CANDIDATE: new Set(), QUARANTINED: new Set() };
  for (const chunk of chunks) {
    stats[chunk.status] = (stats[chunk.status] || 0) + 1;
    for (const id of [
      ...(chunk.mention_record_ids || []),
      ...(chunk.assertion_record_ids || []),
    ])
      records[chunk.status]?.add(id);
  }
  const labels = {
    CANDIDATE: "产生待复核候选",
    QUARANTINED: "抽取记录已隔离",
    REJECTED: "抽取校验未通过",
    PROVIDER_ERROR: "抽取服务失败",
    EMPTY: "未提取到可用事实",
    SOURCE_ONLY: "仅保留来源，未执行抽取",
  };
  const details = Object.entries(stats)
    .map(([status, count]) => `${labels[status] || status} ${count} 个片段`)
    .join("；");
  let outcome;
  if (mode === "SOURCE_ONLY") outcome = "本次未执行抽取，0 条候选记录。";
  else if (records.CANDIDATE.size)
    outcome = `已生成 ${records.CANDIDATE.size} 条待复核候选记录，需独立复核，批准后才能发布。`;
  else if (records.QUARANTINED.size)
    outcome = `0 条候选记录；${records.QUARANTINED.size} 条隔离记录需核对来源与校验问题。`;
  else {
    const reasons = ["REJECTED", "PROVIDER_ERROR", "EMPTY"]
      .filter((status) => stats[status])
      .map((status) => labels[status]);
    outcome = `${reasons.join("；") || "未产生可用抽取结果"}，0 条候选记录。`;
  }
  return { details, outcome };
}

export class ConstructionWorkbench {
  constructor(client, { onPublished = () => {} } = {}) {
    this.client = client;
    this.onPublished = onPublished;
    this.tab = "upload";
    this.reviewEpoch = 0;
    this.publishEpoch = 0;
    this.jobsEpoch = 0;
    this.busy = false;
    this.activeOperation = null;
    this.selected = new Set();
    this.candidates = [];
    this.activePublication = null;
    document
      .querySelectorAll("[data-build-tab]")
      .forEach((b) =>
        b.addEventListener("click", () => this.showTab(b.dataset.buildTab)),
      );
    $("upload-form").addEventListener(
      "submit",
      (event) => void this.upload(event),
    );
    $("upload-file").addEventListener("change", () => {
      const f = $("upload-file").files[0];
      if (f) $("upload-title").value = f.name.replace(/\.[^.]+$/, "");
    });
    $("refresh-jobs").addEventListener("click", () => void this.loadJobs());
    $("refresh-reviews").addEventListener(
      "click",
      () => void this.loadReviews(),
    );
    $("refresh-publications").addEventListener(
      "click",
      () => void this.loadPublications(),
    );
    $("publish-submit").addEventListener("click", () => void this.publish());
  }
  reset() {
    this.reviewEpoch++;
    this.publishEpoch++;
    this.jobsEpoch++;
    this.activeOperation = null;
    this.busy = false;
    this.selected.clear();
    this.candidates = [];
    this.activePublication = null;
    for (const id of [
      "review-list",
      "publication-candidates",
      "construction-jobs",
    ])
      clear($(id));
    for (const id of ["upload-status", "publish-status", "publication-state"])
      $(id).textContent = "";
    $("publish-submit").disabled = true;
    $("publication-selection").textContent = "尚未选择候选";
    $("upload-submit").classList.remove("busy");
    $("upload-submit").disabled = true;
  }
  beginOperation() {
    if (this.busy) return null;
    const operation = { identity: this.client.epoch };
    this.activeOperation = operation;
    this.busy = true;
    return operation;
  }
  ownsOperation(operation) {
    return (
      this.activeOperation === operation &&
      operation.identity === this.client.epoch
    );
  }
  finishOperation(operation) {
    if (!this.ownsOperation(operation)) return false;
    this.activeOperation = null;
    this.busy = false;
    return true;
  }
  refreshCapabilities() {
    const allowed = this.client.hasScope("knowledge:construct");
    $("upload-submit").disabled = !allowed || this.busy;
    $("upload-limit-note").textContent = allowed
      ? "自动抽取最多 2 个片段；仅入库最多 4 个片段。"
      : "当前身份仅可浏览，请选择具有资料构建职责的身份。";
    const group = $("upload-group").value;
    clear($("upload-group"));
    const groups = this.client.session?.identity?.groups || [];
    for (const value of groups)
      if (GROUPS[value])
        $("upload-group").append(new Option(GROUPS[value], value));
    $("upload-group").value = groups.includes(group)
      ? group
      : groups.includes("maintenance")
        ? "maintenance"
        : groups.includes("engineering")
          ? "engineering"
          : "public";
    $("refresh-reviews").disabled = !this.client.hasScope("knowledge:review");
    $("refresh-publications").disabled =
      !this.client.hasScope("knowledge:publish");
    if (this.tab === "review") void this.loadReviews();
    if (this.tab === "publish") void this.loadPublications();
  }
  showTab(tab) {
    this.tab = tab;
    for (const key of ["upload", "review", "publish"])
      $(`build-${key}`).hidden = key !== tab;
    document
      .querySelectorAll("[data-build-tab]")
      .forEach((b) =>
        b.classList.toggle("selected", b.dataset.buildTab === tab),
      );
    if (tab === "review") void this.loadReviews();
    if (tab === "publish") void this.loadPublications();
  }
  async upload(event) {
    event.preventDefault();
    const pending = this.beginOperation();
    if (!pending) return;
    const principal = this.client.session?.identity?.id;
    let fingerprint;
    $("upload-submit").disabled = true;
    $("upload-submit").classList.add("busy");
    $("upload-status").textContent = "正在检查资料和设备适用范围…";
    try {
      const file = $("upload-file").files[0];
      if (!file || !file.size || file.size > MAX_UPLOAD_BYTES)
        throw new Error("请选择 1 byte 到 5 MiB 的文件。");
      const mime = MIME[file.name.split(".").pop().toLowerCase()];
      if (!mime)
        throw new Error("当前在线入口支持 TXT、Markdown、CSV 和 JSON。");
      const title = $("upload-title").value.trim();
      if (!title) throw new Error("请填写资料标题。");
      const key = parseAssetKey($("upload-asset").value);
      const metadata = {
        title,
        source_name: "USER_UPLOAD",
        mime_type: mime,
        language: "zh",
        tbox_key: "industrial-electric-v1",
        extraction_mode: $("upload-mode").value,
        access_groups: [$("upload-group").value],
        published_at: $("upload-date").value
          ? new Date(`${$("upload-date").value}T00:00:00+08:00`).toISOString()
          : null,
        industrial_context: {
          family: $("upload-family").value,
          asset_keys: key ? [key] : [],
        },
      };
      const bytes = new Uint8Array(await file.arrayBuffer());
      fingerprint = await digest(
        new TextEncoder().encode(
          JSON.stringify({ metadata, checksum: await digest(bytes) }),
        ),
      );
      if (!this.ownsOperation(pending)) return;
      const operation = savedOperation(principal, fingerprint);
      $("upload-status").textContent =
        metadata.extraction_mode === "LLM"
          ? "正在入库、向量化并抽取候选。处理期间请勿重复提交。"
          : "正在入库与向量化，保留可检索来源片段。";
      const result = await this.client.request("/v1/knowledge:construct", {
        ...metadata,
        operation_key: operation.operation_key,
        canonical_uri: operation.canonical_uri,
        max_attempts: 1,
        content_base64: base64(bytes),
      });
      if (!this.ownsOperation(pending)) return;
      const summary = constructionSummary(
        result.chunks,
        result.extraction_mode,
      );
      $("upload-status").textContent =
        `来源资料已入库，共 ${result.chunks.length} 个片段。\n${summary.outcome}\n${summary.details}\n任务 ${result.job_id} 已留存；相同资料和设置再次提交会复用本次操作，不会新建抽取任务。`;
      toast(`来源已入库；${summary.outcome}`);
      void this.loadJobs();
    } catch (error) {
      if (!this.ownsOperation(pending)) return;
      if (error.code === "construction_ingestion_failed" && fingerprint)
        forgetOperation(principal, fingerprint);
      const text = safeError(error);
      if (text)
        $("upload-status").textContent =
          `${text}\n可先查看任务记录；同一资料再次提交会复用本次操作身份。`;
    } finally {
      if (this.finishOperation(pending)) {
        $("upload-submit").classList.remove("busy");
        this.refreshCapabilities();
      }
    }
  }
  async loadJobs() {
    const identity = this.client.epoch,
      epoch = ++this.jobsEpoch;
    const area = clear($("construction-jobs"));
    if (!this.client.hasScope("knowledge:construct")) {
      area.append(empty("当前身份没有构建任务读取权限。"));
      return;
    }
    area.append(element("p", "muted", "正在读取任务…"));
    try {
      const result = await this.client.request(
        "/v1/knowledge/construction-jobs?limit=25",
      );
      if (identity !== this.client.epoch || epoch !== this.jobsEpoch) return;
      clear(area);
      if (!result.items.length)
        area.append(element("p", "muted", "当前没有可见的构建任务。"));
      for (const job of result.items) {
        const item = element("div", "job-item");
        item.append(
          element(
            "strong",
            "",
            `${STATUS_LABELS[job.status] || { COMPLETED: "处理结束", RETRY_WAIT: "等待恢复" }[job.status] || job.status} · ${job.completed_chunks}/${job.expected_chunks} 片段`,
          ),
          element(
            "p",
            "",
            `${dateLabel(job.created_at)} · ${shortId(job.job_id)}`,
          ),
        );
        if (job.status === "COMPLETED" && job.chunks?.length) {
          const summary = constructionSummary(job.chunks, job.extraction_mode);
          item.append(
            element("p", "", `来源已入库；${summary.outcome}`),
            element("p", "muted", summary.details),
          );
        }
        if (job.last_finding_codes?.length)
          item.append(
            element("p", "danger-text", job.last_finding_codes.join("、")),
          );
        area.append(item);
      }
    } catch (error) {
      if (identity === this.client.epoch && epoch === this.jobsEpoch) {
        const text = safeError(error);
        if (text) clear(area).append(element("p", "danger-text", text));
      }
    }
  }
  async loadReviews() {
    const identity = this.client.epoch,
      epoch = ++this.reviewEpoch;
    const area = clear($("review-list"));
    if (!this.client.hasScope("knowledge:review")) {
      area.append(
        empty("当前身份没有复核职责。请切换工业工程师或工业管理员。"),
      );
      return;
    }
    area.append(empty("正在读取候选事实…"));
    try {
      const result = await this.client.request(
        "/v1/knowledge/review-queue?limit=100",
      );
      if (identity !== this.client.epoch || epoch !== this.reviewEpoch) return;
      clear(area);
      const items = [...result.items].sort(
        (a, b) =>
          (a.record_kind === "ENTITY_MENTION" ? 0 : 1) -
          (b.record_kind === "ENTITY_MENTION" ? 0 : 1),
      );
      if (!items.length)
        area.append(
          empty("当前没有待复核候选。已批准的内容可以在发布步骤中查看。"),
        );
      for (const item of items)
        area.append(this.reviewCard(item, identity, epoch));
    } catch (error) {
      if (identity === this.client.epoch && epoch === this.reviewEpoch) {
        const text = safeError(error);
        if (text) clear(area).append(empty(text));
      }
    }
  }
  reviewCard(item, identity, epoch) {
    const card = element("article", "review-card"),
      tags = element("div", "tag-row");
    tags.append(
      authorityTag(item.trust.authority),
      tag(ORIGIN_LABELS[item.trust.origin] || item.trust.origin),
      tag(STATUS_LABELS[item.trust.status] || item.trust.status, "warning"),
    );
    card.append(
      element("h3", "", recordTitle(item)),
      tags,
      element("pre", "source-quote", item.evidence.quoted_text),
      metadata([
        `来源字符 ${item.evidence.char_start}–${item.evidence.char_end}`,
        `片段 ${shortId(item.evidence.chunk_id)}`,
        `当前修订 ${item.revision}`,
        `记录置信度 ${Math.round(item.confidence * 100)}%`,
      ]),
    );
    const notes = element("textarea");
    notes.rows = 2;
    notes.placeholder = "填写人工复核依据：核对了哪些来源、身份和适用条件？";
    notes.maxLength = 4000;
    notes.setAttribute("aria-label", `复核依据：${recordTitle(item)}`);
    card.append(notes);
    const details = element("div", "review-checks"),
      actions = element("div", "review-actions"),
      status = element("p", "inline-status");
    card.append(details, actions, status);
    const current = () =>
      identity === this.client.epoch && epoch === this.reviewEpoch;
    const requireNotes = () => {
      const value = notes.value.trim();
      if (!value) throw new Error("请先填写本项人工复核依据。");
      return value;
    };
    const run = async (action) => {
      if (!current()) return;
      const pending = this.beginOperation();
      if (!pending) return;
      card.querySelectorAll("button").forEach((b) => (b.disabled = true));
      try {
        await action();
      } catch (error) {
        const text = safeError(error);
        if (text && current()) status.textContent = text;
      } finally {
        if (this.finishOperation(pending) && current()) {
          card.querySelectorAll("button").forEach((b) => (b.disabled = false));
        }
      }
    };
    const decide = (decision) =>
      run(async () => {
        const note = requireNotes();
        await this.client.request("/v1/knowledge/reviews:batch", {
          decisions: [
            {
              record_kind: item.record_kind,
              record_id: item.record_id,
              expected_revision: item.revision,
              decision,
              notes: note,
            },
          ],
        });
        if (current()) {
          toast(`已保存：${STATUS_LABELS[decision]}`);
          await this.loadReviews();
        }
      });
    if (item.record_kind === "ENTITY_MENTION")
      actions.append(
        button("核对已有实体", () =>
          run(async () => {
            const response = await this.client.request(
              `/v1/knowledge/entity-resolution/${encodeURIComponent(item.record_id)}?expected_revision=${item.revision}`,
            );
            if (!current()) return;
            clear(details);
            for (const suggestion of response.suggestions) {
              const row = element("div", "dependency");
              row.append(
                element(
                  "strong",
                  "",
                  suggestion.target
                    ? `候选匹配：${suggestion.target.canonical_name}`
                    : "没有可确认的已有实体",
                ),
                element("p", "", suggestion.reason),
              );
              for (const evidence of suggestion.evidence || [])
                for (const source of evidence.authoritative_evidence || [])
                  row.append(
                    element("pre", "source-quote", source.quoted_text),
                  );
              if (suggestion.target)
                row.append(
                  button("确认链接到此实体", () =>
                    run(async () => {
                      const note = requireNotes();
                      await this.client.request(
                        "/v1/knowledge/entity-resolution:apply",
                        {
                          record_id: item.record_id,
                          expected_revision: item.revision,
                          target_entity_id: suggestion.target.entity_id,
                          notes: note,
                        },
                      );
                      if (current()) {
                        toast("实体链接已保存，依赖关系仍须逐项复核。");
                        await this.loadReviews();
                      }
                    }),
                  ),
                );
              details.append(row);
            }
          }),
        ),
      );
    else
      actions.append(
        button("检查关系与依赖", () =>
          run(async () => {
            const response = await this.client.request(
              `/v1/knowledge/review-assessments/${encodeURIComponent(item.record_id)}?expected_revision=${item.revision}`,
            );
            if (!current()) return;
            clear(details).append(
              element(
                "p",
                "dependency",
                `${response.status} · ${response.summary}`,
              ),
            );
            for (const dep of response.dependencies)
              details.append(
                element(
                  "p",
                  "muted",
                  `${dep.name} · ${STATUS_LABELS[dep.status] || dep.status} · ${dep.ready ? "依赖已就绪" : "需先处理实体"}`,
                ),
              );
            for (const match of response.matches || [])
              details.append(
                element(
                  "pre",
                  "source-quote",
                  match.record.evidence.quoted_text,
                ),
              );
          }),
        ),
      );
    actions.append(
      button(
        item.record_kind === "ENTITY_MENTION" ? "批准为当前实体" : "批准此关系",
        () => decide("APPROVED"),
        "button primary",
      ),
      button("拒绝", () => decide("REJECTED")),
      button("暂存待处理", () => decide("QUARANTINED")),
    );
    return card;
  }
  async loadPublications() {
    const identity = this.client.epoch,
      epoch = ++this.publishEpoch;
    this.selected.clear();
    this.candidates = [];
    this.activePublication = null;
    $("publish-submit").disabled = true;
    $("publication-selection").textContent = "尚未选择候选";
    const area = clear($("publication-candidates"));
    if (!this.client.hasScope("knowledge:publish")) {
      area.append(empty("当前身份没有发布职责。请切换工业管理员。"));
      return;
    }
    area.append(empty("正在读取发布版本和已批准知识…"));
    try {
      const [candidates, history] = await Promise.all([
        this.client.request("/v1/knowledge/publication-candidates?limit=100"),
        this.client.request("/v1/knowledge/publications?limit=100"),
      ]);
      if (identity !== this.client.epoch || epoch !== this.publishEpoch) return;
      this.candidates = candidates.items;
      this.activePublication =
        history.items.find((item) => item.status === "ACTIVE") || null;
      $("publication-state").textContent = this.activePublication
        ? `当前知识版本 ${this.activePublication.generation} · 本次只加入所选批准项，保留其他已发布记录。`
        : "当前尚无知识发布版本。";
      clear(area);
      if (!this.candidates.length)
        area.append(empty("没有待发布的批准项。请先完成候选复核。"));
      for (const candidate of this.candidates) {
        const record = candidate.record;
        const label = element("label", "candidate-item"),
          checkbox = element("input");
        checkbox.type = "checkbox";
        checkbox.setAttribute("aria-label", `选择发布：${recordTitle(record)}`);
        checkbox.addEventListener("change", () => {
          if (identity !== this.client.epoch || epoch !== this.publishEpoch)
            return;
          if (this.busy) {
            checkbox.checked = this.selected.has(record.revision_id);
            return;
          }
          if (checkbox.checked) this.selected.add(record.revision_id);
          else this.selected.delete(record.revision_id);
          $("publication-selection").textContent =
            `已选择 ${this.selected.size} 项`;
          $("publish-submit").disabled = !this.selected.size;
        });
        const body = element("div");
        body.append(
          element("h3", "", recordTitle(record)),
          authorityTag(record.trust.authority),
          metadata([
            `修订 ${record.revision}`,
            candidate.requires_replacement
              ? "替换同一记录的旧修订"
              : "新增至当前发布版本",
          ]),
        );
        label.append(checkbox, body);
        area.append(label);
      }
    } catch (error) {
      if (identity === this.client.epoch && epoch === this.publishEpoch) {
        const text = safeError(error);
        if (text) clear(area).append(empty(text));
      }
    }
  }
  async publish() {
    if (!this.selected.size) return;
    const pending = this.beginOperation();
    if (!pending) return;
    let publishedMessage = null;
    $("publish-submit").disabled = true;
    $("publish-status").textContent = "正在核验并发布所选知识…";
    try {
      const selected = this.candidates.filter((item) =>
        this.selected.has(item.record.revision_id),
      );
      const result = await this.client.request(
        "/v1/knowledge/publications:publish",
        {
          approved_revision_ids: selected.map(
            (item) => item.record.revision_id,
          ),
          expected_active_publication_id:
            this.activePublication?.publication_id || null,
          remove_record_ids: [],
          replace_record_ids: selected
            .filter((item) => item.requires_replacement)
            .map((item) => item.record.record_id),
        },
      );
      if (!this.ownsOperation(pending)) return;
      publishedMessage = `已发布知识版本 ${result.generation}。已批准的次权威事实仍保留原来的来源与权威类别。`;
      $("publish-status").textContent = publishedMessage;
      this.selected.clear();
      this.candidates = [];
      $("publication-selection").textContent = "所选候选已发布";
      $("publication-candidates")
        .querySelectorAll("input")
        .forEach((input) => {
          input.checked = false;
          input.disabled = true;
        });
      toast("知识已发布，可回到图谱查看新关系。");
      const entityIds = [
        ...new Set(
          selected.flatMap(({ record }) =>
            [
              record.entity?.entity_id,
              record.subject?.entity_id,
              record.object_entity?.entity_id,
            ].filter(Boolean),
          ),
        ),
      ];
      let refreshFailed = false;
      try {
        await this.onPublished(entityIds);
      } catch {
        refreshFailed = true;
      }
      if (!this.ownsOperation(pending)) return;
      await this.loadPublications();
      if (refreshFailed && this.ownsOperation(pending))
        $("publish-status").textContent =
          `${publishedMessage}\n图谱刷新未完成，可手动刷新查看已发布知识。`;
    } catch (error) {
      if (this.ownsOperation(pending)) {
        const text = safeError(error);
        if (publishedMessage)
          $("publish-status").textContent =
            `${publishedMessage}\n页面刷新未完成，可手动刷新查看已发布知识。`;
        else if (text) $("publish-status").textContent = text;
      }
    } finally {
      if (this.finishOperation(pending)) {
        $("publish-submit").disabled = !this.selected.size;
      }
    }
  }
}
