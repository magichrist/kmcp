/* Playbook Studio SPA — routed, data-driven pages wired to /api/*.
 * No hardcoded content: every page renders live backend data.
 * Auth: 401 -> token prompt (localStorage 'kmcp_token', Bearer header).
 */
const API = window.API || "/api";
const TOKEN_KEY = "kmcp_token";

function $(sel, root = document) { return root.querySelector(sel); }
function $$(sel, root = document) { return [...root.querySelectorAll(sel)]; }
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* Copy text to clipboard; works in Safari and other browsers. */
function showCopyToast(msg, duration = 900) {
  const toastEl = $("#toast");
  if (toastEl) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    setTimeout(() => toastEl.classList.remove("show"), duration);
  }
}

function copyText(text) {
  const t = String(text ?? "");
  let ok = false;

  /* Method 1: execCommand — synchronous, works in Safari from user gesture */
  try {
    const el = document.createElement("textarea");
    el.value = t;
    el.setAttribute("readonly", "");
    el.style.position = "fixed";
    el.style.left = "-9999px";
    el.style.top = "10px";
    document.body.appendChild(el);
    el.focus();
    el.select();
    ok = document.execCommand("copy");
    document.body.removeChild(el);
  } catch (err) { ok = false; }

  if (ok) { showCopyToast("copied", 1200); return; }

  /* Method 2: Clipboard API fallback */
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(() => showCopyToast("copied", 1200))
      .catch(() => showCopyToast("copy failed", 2500));
    return;
  }

  showCopyToast("copy failed", 2500);
}

/* ── API client ─────────────────────────────────────────────────────── */

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }

function toast(msg, bad = false, duration = bad ? 4500 : 2600) {
  const el = $("#toast");
  if (!el) return;
  el.textContent = msg;
  el.style.borderColor = bad ? "#6b4343" : "";
  el.style.background = bad ? "#2e1d20" : "";
  el.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => {
    el.classList.remove("show");
    el.style.borderColor = "";
    el.style.background = "";
  }, duration);
}

/* Busy state for buttons during async actions: disabled + trailing "…".
 * Remembers the original label on the first busy call so it can be restored. */
function busyBtn(btn, busy) {
  if (!btn) return;
  if (busy) {
    if (!btn.dataset.btnBase) btn.dataset.btnBase = btn.textContent.replace(/\s*…\s*$/, "").trim();
    btn.disabled = true;
    btn.textContent = btn.dataset.btnBase + "…";
  } else {
    btn.disabled = false;
    btn.textContent = btn.dataset.btnBase || btn.textContent.replace(/\s*…\s*$/, "").trim();
  }
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (token()) headers.Authorization = `Bearer ${token()}`;
  const res = await fetch(API + path, { ...opts, headers });
  if (res.status === 401) {
    promptForToken();
    throw new Error("401 Unauthorized — API token required");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body?.error?.message || body?.error || `${res.status} ${res.statusText}`);
  return body?.data ?? body;
}

let pendingRetry = [];
function promptForToken() {
  $("#tokenModal").classList.add("show");
  $("#tokenInput").focus();
  return new Promise((resolve) => pendingRetry.push(resolve));
}

async function saveTokenAndRetry() {
  const value = $("#tokenInput").value.trim();
  if (!value) return;
  localStorage.setItem(TOKEN_KEY, value);
  $("#tokenModal").classList.remove("show");
  $("#tokenInput").value = "";
  refreshAuthBadge();
  toast("Token saved — reloading");
  pendingRetry.forEach((r) => r(true));
  pendingRetry = [];
  route();
}

/* ── shell helpers ─────────────────────────────────────────────────── */

function setPageMeta({ eyebrow, title, subtitle, actions = [], crumb } = {}) {
  const actionsEl = $("#topActions");
  if (actionsEl) actionsEl.innerHTML = actions
    .map((a, i) => `<button class="btn ${a.cls || "ghost"}" data-act="top${i}">${esc(a.label)}</button>`)
    .join("");
  if (actionsEl) {
    $$("#topActions [data-act]").forEach((btn, i) => {
      btn.addEventListener("click", () => { const f = actions[i]?.fn; if (f) f(); });
    });
  }
  const tail = $("#crumbPage");
  if (tail) tail.textContent = crumb || title || "";
  const titleEl = $("#crumbTitle");
  if (titleEl) titleEl.textContent = subtitle || "";
}

async function render() {
  const container = $("#content");
  container.innerHTML = `<section class="pagehead">
    <div class="page-top"><div id="pageHeadInfo"></div><div class="head-right" id="pageHeadActions"></div></div>
  </section><section class="pagebody" id="pageBody"></section>`;
  return container;
}

function headActionsHtml(buttons) {
  return buttons.map((b) => `<button class="btn ${b.cls || "ghost"}" id="${b.id}">${esc(b.label)}</button>`).join("");
}

/* Generic form modal — replaces bare prompt() dialogs.
 * fields: [{ name, label, type: "text"|"select"|"area", value, placeholder,
 *            options, required (default true), hint }]
 * Resolves with {name: value} on submit, or null on cancel / backdrop / Escape. */
function formModal({ title, subtitle, fields, submitLabel = "Save", wide = false }) {
  return new Promise((resolve) => {
    const bd = document.createElement("div");
    bd.className = "modal-backdrop show";
    const control = (f) => {
      const id = `fm_${f.name}`;
      if (f.type === "select") {
        return `<select class="select" id="${id}">${(f.options || []).map((o) => {
          const val = String(o.value ?? o);
          return `<option value="${esc(val)}" ${String(f.value ?? "") === val ? "selected" : ""}>${esc(o.label ?? o)}</option>`;
        }).join("")}</select>`;
      }
      if (f.type === "area") {
        return `<textarea class="select area" id="${id}" placeholder="${esc(f.placeholder || "")}" spellcheck="false" style="min-height:72px;font-family:ui-monospace,Menlo,monospace">${esc(f.value ?? "")}</textarea>`;
      }
      return `<input class="select" id="${id}" value="${esc(f.value ?? "")}" placeholder="${esc(f.placeholder || "")}" autocomplete="off" spellcheck="false" />`;
    };
    bd.innerHTML = `
      <div class="modal ${wide ? "new-pb-modal" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <h3>${esc(title)}</h3>
        ${subtitle ? `<p class="dim">${esc(subtitle)}</p>` : ""}
        ${fields.map((f) => `
          <label style="display:block;margin:10px 0 0">
            <span class="np-label" style="display:block;font-size:12px;margin-bottom:4px">${esc(f.label)}${f.required === false ? ' <em class="dim">optional</em>' : ""}</span>
            ${control(f)}
            ${f.hint ? `<span class="dim" style="display:block;font-size:11px;margin-top:3px">${esc(f.hint)}</span>` : ""}
          </label>`).join("")}
        <div class="modal-actions">
          <button class="btn" id="fmCancel">Cancel</button>
          <button class="btn primary" id="fmSubmit">${esc(submitLabel)}</button>
        </div>
      </div>`;
    document.body.appendChild(bd);
    const close = (val) => { document.removeEventListener("keydown", escKey); bd.remove(); resolve(val); };
    const escKey = (e) => { if (e.key === "Escape") close(null); };
    document.addEventListener("keydown", escKey);
    bd.addEventListener("mousedown", (e) => { if (e.target === bd) close(null); });
    bd.querySelector("#fmCancel").addEventListener("click", () => close(null));
    bd.querySelector("#fmSubmit").addEventListener("click", () => {
      const out = {};
      for (const f of fields) {
        const v = (bd.querySelector(`#fm_${f.name}`)?.value ?? "").trim();
        if (f.required !== false && !v) { toast(`${f.label} is required`, true); return; }
        out[f.name] = v;
      }
      close(out);
    });
    const first = bd.querySelector(".select");
    if (first) first.focus();
  });
}

/* ── shared widgets ────────────────────────────────────────────────── */

function kpis(items) {
  return `<div class="kpi-grid">${items.map((k) => `
    <span class="kpi-badge">
      <span class="kpi-badge-label">${esc(k.label)}</span>
      <span class="kpi-badge-value">${esc(k.value)}</span>
      ${k.sub || ""}
    </span>`).join("")}</div>`;
}

/* render a {key: count} map as compact badge chips for KPI sub-lines */
function kpiBadges(obj) {
  if (!obj || typeof obj !== "object") return "";
  const entries = Object.entries(obj).filter(([, v]) => v != null && v !== 0);
  if (!entries.length) return "";
  const chips = entries.map(([k, v]) => `<span class="kpi-chip">${esc(k)}: ${esc(String(v))}</span>`).join("");
  return `<span class="kpi-sub-badges">${chips}</span>`;
}

function table(headers, rows, opts = {}) {
  /* opts may be a string (legacy emptyText) or { emptyText, emptyLabel, sortable, sortState }.
   * emptyLabel: when the list is empty, render an action button (wired by the caller via wireRetry).
   * sortable: array of column indexes that render as clickable sort headers.
   * sortState: { col, dir } used to show the active sort direction indicator. */
  let emptyText = "Nothing here yet.";
  let emptyLabel = null;
  let sortable = [];
  let sortState = null;
  let selectable = false;
  if (typeof opts === "string") emptyText = opts;
  else {
    emptyText = opts.emptyText ?? "Nothing here yet.";
    emptyLabel = opts.emptyLabel ?? null;
    sortable = opts.sortable || [];
    sortState = opts.sortState;
    selectable = !!opts.selectable;
  }
  const selCols = selectable ? 1 : 0;
  const totalCols = headers.length + selCols;
  if (!rows.length) {
    const act = emptyLabel ? `<div style="margin-top:12px"><button class="btn primary" data-retry>${esc(emptyLabel)}</button></div>` : "";
    return `<div class="panel table-panel"><p class="empty">${esc(emptyText)}</p>${act}</div>`;
  }
  return `<div class="panel table-panel" style="overflow:auto">
    <div class="table-head" style="grid-template-columns:repeat(${totalCols},minmax(0,1fr)) auto">
      ${selectable ? `<span><input type="checkbox" class="sel-all" id="selAll" title="Select all"></span>` : ""}
      ${headers.map((h, i) => {
        const canSort = sortable.includes(i);
        const active = sortState && sortState.col === i;
        const dir = active ? (sortState.dir === "asc" ? "↑" : "↓") : "";
        return canSort
          ? `<button class="sort-header" type="button" data-sort="${i}" title="Sort by ${esc(h)}">${esc(h)} <span class="sort-dir" aria-hidden="true">${dir}</span></button>`
          : `<span>${esc(h)}</span>`;
      }).join("")}<span></span>
    </div>
    ${rows.map((r) => `<div class="table-row" style="grid-template-columns:repeat(${totalCols},minmax(0,1fr)) auto">
      ${selectable ? `<input type="checkbox" class="sel-row" data-fid="${esc(String(r.id ?? ""))}">` : ""}
      ${r.cells.map((c) => `<span>${c}</span>`).join("")}<span>${r.menu || ""}</span>
    </div>`).join("")}
  </div>`;
}

/* Attach click handlers to every [data-retry] button inside an empty-state panel
 * (rendered by table() or emptyStatePanel()). scope defaults to document. */
function wireRetry(scope, fn) {
  const root = scope || document;
  $$("[data-retry]", root).forEach((b) => b.addEventListener("click", fn));
}

/* Helpful empty-state panel with a retry button, shown when a list API call
   fails or returns no rows (instead of leaving "Loading…" on screen forever). */
function emptyStatePanel(title, message, retryLabel = "Retry") {
  return `<div class="panel table-panel">
    <div class="empty-state">
      <p class="empty-title">${esc(title)}</p>
      <p class="empty-msg">${esc(message)}</p>
      <button class="btn primary" data-retry>${esc(retryLabel)}</button>
    </div>
  </div>`;
}

/* Attach prev/page-size/next wiring to a pagination bar rendered by paginationHtml(). */
function wirePagination(pg, { onPrev, onNext, onSize }) {
  pg.querySelector("[data-act=prev]")?.addEventListener("click", onPrev);
  pg.querySelector("[data-act=next]")?.addEventListener("click", onNext);
  pg.querySelector("#pageSize")?.addEventListener("change", (e) => onSize(e.target.value));
}

/* Attach click handlers to sortable table headers. */
function wireSortHeaders(el, onSort) {
  $$("[data-sort]", el).forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      onSort(Number(btn.dataset.sort));
    });
  });
}

/* Render prev/next, page size selector and page indicator. total may be null
   when the backend does not report a count (then "of N" is omitted). */
function paginationHtml({ page, pageSize, total, hasPrev, hasNext, from, to }) {
  const totalPages = total != null ? Math.max(1, Math.ceil(total / pageSize)) : 0;
  return `<div class="pagination" style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin:6px 0 8px;font-size:13px;color:var(--muted)">
    <div style="display:flex;align-items:center;gap:8px">
      <button class="btn" data-act="prev" ${!hasPrev ? "disabled" : ""}>‹ Prev</button>
      <span class="page-indicator">Page <b>${page}</b>${totalPages ? ` of <b>${totalPages}</b>` : ""} · showing <b>${from}</b>–<b>${to}</b>${total != null ? ` of <b>${total}</b>` : ""}</span>
      <select class="select" id="pageSize">${[10, 25, 50, 100].map((s) => `<option value="${s}" ${s === pageSize ? "selected" : ""}>${s}</option>`).join("")}</select>
      <button class="btn" data-act="next" ${!hasNext ? "disabled" : ""}>Next ›</button>
    </div>
  </div>`;
}

function colorJson(v, indent = 0) {
  /* Recursively colorize a JSON value into HTML spans for human readability. */
  const pad = "  ".repeat(indent);
  const pad1 = "  ".repeat(indent + 1);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* Compact ISO timestamps: "2026-11-08T12:02:52.748528+00:00" -> "2026-11-08 12:02:52" */
  const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
  const compactTime = (s) => {
    const m = s.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/);
    return m ? `${m[1]} ${m[2]}` : s;
  };

  if (v === null) return `<span class="json-null">null</span>`;
  if (typeof v === "boolean") return `<span class="json-bool">${v}</span>`;
  if (typeof v === "number") return `<span class="json-num">${v}</span>`;
  if (typeof v === "string") {
    const disp = ISO_RE.test(v) ? compactTime(v) : v;
    return `<span class="json-str">"${esc(disp)}"</span>`;
  }
  if (Array.isArray(v)) {
    if (!v.length) return `<span class="json-punc">[</span><span class="json-punc">]</span>`;
    const items = v.map((x) => `<div>${pad1}${colorJson(x, indent + 1)},</div>`).join("");
    return `<span class="json-punc">[</span>\n${items}\n${pad}<span class="json-punc">]</span>`;
  }
  if (typeof v === "object") {
    const keys = Object.keys(v);
    if (!keys.length) return `<span class="json-punc">{</span><span class="json-punc">}</span>`;
    const items = keys.map((k) => {
      const val = colorJson(v[k], indent + 1);
      return `<div>${pad1}<span class="json-key">"${esc(k)}"</span>: ${val}</div>`;
    }).join("");
    return `<span class="json-punc">{</span>\n${items}\n${pad}<span class="json-punc">}</span>`;
  }
  return `<span class="json-str">"${esc(String(v))}"</span>`;
}

function renderDsl() {
  const el = $("#dslCode");
  if (!el) return;
  try {
    const parsed = JSON.parse(rawDsl || "{}");
    el.innerHTML = colorJson(parsed);
  } catch {
    // keep existing HTML if rawDsl is invalid
  }
}

/* compact human-readable timestamp: "2026-11-10T16:47:07" -> "2026-11-10 16:47" */
function fmtTs(v) {
  if (!v) return "—";
  const m = String(v).match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
  return m ? `${m[1]} ${m[2]}` : String(v).slice(0, 16);
}

/* Format seconds into a compact uptime string. */
function fmtUptime(sec) {
  const s = Math.floor(Number(sec) || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}h ${m}m ${r}s` : m ? `${m}m ${r}s` : `${r}s`;
}

const BADGE = (s) => {
  const map = {
    completed: "run-state", running: "run-state run-run", pending: "run-state",
    failed: "run-state failed", cancelled: "run-state failed",
    draft: "run-state", published: "run-state ok", healthy: "run-state ok",
    unreachable: "run-state failed", critical: "run-state failed",
    high: "run-state failed", medium: "run-state warn", low: "run-state",
    info: "run-state",
  };
  return `<span class="${map[s] || "run-state"}">${esc(s || "—")}</span>`;
};

/* ── route: overview ───────────────────────────────────────────────── */

async function pageOverview() {
  setPageMeta({ crumb: "Overview", title: "Workspace overview", subtitle: "Live backend summary" });
  const o = await api("/overview");
  const execState = o.executions?.by_status || {};
  const fnd = o.findings?.by_severity || {};
  const runState = ["running", "completed", "failed", "cancelled"]
    .filter((s) => execState[s]).map((s) => `${s}: ${execState[s]}`).join(" · ") || "no runs yet";
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">OVERVIEW</div><h1>Overview</h1>
    <div class="subtitle">Everything below is read live from the database.</div>`;
  $("#pageBody").innerHTML = `
    ${kpis([
      { label: "Playbooks", value: o.playbooks?.total ?? 0, sub: kpiBadges(o.playbooks?.by_status) },
      { label: "Executions", value: o.executions?.total ?? 0, sub: kpiBadges(execState) || "no runs yet" },
      { label: "Tools", value: o.tools?.total ?? 0, sub: kpiBadges(o.tools?.by_visibility) },
      { label: "Findings", value: o.findings?.total ?? 0, sub: kpiBadges(fnd) },
      { label: "MCP connections", value: o.connections?.total ?? 0, sub: kpiBadges({ healthy: o.connections?.healthy ?? 0 }) },
      { label: "Agent profiles", value: o.agents?.total ?? 0, sub: "" },
    ])}
    <div class="quick-actions" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:10px 0 14px">
      <button class="btn primary" id="qaNewPlaybook" style="width:100%;padding:14px">＋ New playbook</button>
      <button class="btn" id="qaRunPlaybook" style="width:100%;padding:14px">▶ Run a playbook</button>
      <button class="btn" id="qaAddConnection" style="width:100%;padding:14px">🔌 Add connection</button>
      <button class="btn" id="qaViewFindings" style="width:100%;padding:14px">🔎 View findings</button>
    </div>
    <div class="two-col">
      <div class="panel" style="min-height:220px">
        <div class="panel-head"><span>Recent executions</span></div>
        <div style="padding:8px">${table(["Execution", "Playbook", "Status", "Completed"],
          (o.recent_executions || []).slice(0, 5).map((e) => ({
            cells: [esc(e.execution_id), esc(e.playbook_id || "—"), BADGE(e.status), fmtTs(e.completed_at)],
            menu: `<a href="#/executions/${encodeURIComponent(e.execution_id)}">view</a>`,
          })), "No executions yet.")}
        </div>
      </div>
      <div class="panel" style="min-height:220px">
        <div class="panel-head"><span>Latest findings</span></div>
        <div style="padding:8px">${table(["Target hint", "Tool", "Severity", "Test"],
          (o.recent_findings || []).slice(0, 5).map((f) => ({
            cells: [esc(f.port_state || f.status || "—"), esc(f.tool), BADGE(f.severity), esc(f.test_name || f.checkname || f.validation_status || "—")],
            menu: `<a href="#/findings">findings</a>`,
          })), "No findings yet.")}
        </div>
      </div>
    </div>`;
  $("#qaNewPlaybook")?.addEventListener("click", () => { location.hash = "#/playbooks"; });
  $("#qaRunPlaybook")?.addEventListener("click", () => { location.hash = "#/executions"; });
  $("#qaAddConnection")?.addEventListener("click", () => { location.hash = "#/connections"; });
  $("#qaViewFindings")?.addEventListener("click", () => { location.hash = "#/findings"; });
}

/* ── route: playbook studio (library + editor) ─────────────────────── */

let selectedId = null;
let playbooksCache = [];

async function refreshPlaybooks() {
  const el = $("#pbGrid");
  try {
    const d = await api("/playbooks?limit=200");
    playbooksCache = d.playbooks || [];
    if (el) renderPlaybookGrid();
  } catch (err) {
    if (el) el.innerHTML = emptyStatePanel("Failed to load playbooks", err.message, "Retry");
    el?.querySelector("[data-retry]")?.addEventListener("click", refreshPlaybooks);
  }
}

function pbFiltered() {
  const q = ($("#pbSearch")?.value || "").toLowerCase().trim();
  const st = $("#pbStatusFilter")?.value || "";
  const cat = $("#pbCategoryFilter")?.value || "";
  let rows = playbooksCache.filter((p) =>
    (!q || `${p.name} ${p.playbook_id} ${p.description || ""}`.toLowerCase().includes(q)) &&
    (!st || (p.status || "") === st) &&
    (!cat || (p.category || "custom") === cat));
  const sort = $("#pbSort")?.value || "newest";
  if (sort === "name") rows = rows.sort((a, b) => String(a.name).localeCompare(String(b.name)));
  else if (sort === "oldest") rows = rows.sort((a, b) => (a.id ?? 0) - (b.id ?? 0));
  else rows = rows.sort((a, b) => (b.id ?? 0) - (a.id ?? 0));
  return rows;
}

function renderPlaybookGrid() {
  const el = $("#pbGrid");
  if (!el) return;
  const rows = pbFiltered();
  if (!playbooksCache.length) {
    el.innerHTML = `
      <div class="lib-empty">
        <div class="lib-empty-glyph">⌘</div>
        <h2>Create your first playbook</h2>
        <p>Build a security workflow out of registered tools, shell commands and internal stages — visually on the graph, in the DSL, or both.</p>
        <div class="lib-empty-actions">
          <button class="btn primary" id="pbNewBtn2">＋ Create playbook</button>
          <button class="btn" id="pbImportBtn2">↙ Import playbook</button>
        </div>
      </div>`;
    $("#pbNewBtn2")?.addEventListener("click", newPlaybook);
    $("#pbImportBtn2")?.addEventListener("click", importPlaybook);
    return;
  }
  if (!rows.length) {
    el.innerHTML = `<p class="empty">No playbooks match this filter.</p>
      <div style="margin-top:12px"><button class="btn primary" id="pbClearFilters">Clear filters</button></div>`;
    $("#pbClearFilters")?.addEventListener("click", () => {
      $("#pbSearch").value = "";
      $("#pbStatusFilter").value = "";
      $("#pbCategoryFilter").value = "";
      $("#pbSort").value = "newest";
      renderPlaybookGrid();
    });
    return;
  }
  el.innerHTML = rows.map((p) => {
    const pid = esc(p.playbook_id);
    const isDraft = (p.status || "") === "draft";
    return `
      <a class="pb-card ${p.playbook_id === selectedId ? "active" : ""}" href="#/playbooks/${encodeURIComponent(p.playbook_id)}">
        <div class="pb-card-top">
          <span class="pb-card-name">${esc(p.name)}</span>${BADGE(p.status)}
        </div>
        <p class="pb-card-desc">${esc(p.description || "No description yet.")}</p>
        <div class="pb-card-meta">
          <span class="chip">${esc(p.category || "custom")}</span>
          <span class="chip mono">${pid}</span>
          <span class="dim">v ${esc(p.latest_version || "—")}</span>
          <span class="dim">${esc((p.created_at || "").slice(0, 10))}</span>
        </div>
        <div class="pb-card-foot">
          <button class="btn ghost pb-card-export" title="Export playbook" data-pid="${pid}">Export</button>
          <button class="btn ghost pb-card-versions" title="View versions" data-pid="${pid}">Versions</button>
          ${isDraft ? `<button class="btn pb-card-publish" title="Publish draft" data-pid="${pid}">Publish</button>` : ""}
        </div>
        <button class="pb-card-delete" title="Delete playbook" data-pid="${pid}">✕</button>
      </a>`;
  }).join("");
}

async function pagePlaybooks() {
  selectedId = null;
  setPageMeta({ crumb: "Playbook Studio" });
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">PLAYBOOK STUDIO</div><h1>Playbooks</h1>
    <div class="subtitle">Create, manage and organize security workflows. Open one to edit it on the graph or in the DSL.</div>`;
  $("#pageHeadActions").innerHTML = headActionsHtml([
    { id: "pbImportBtn", label: "↙ Import" },
    { id: "pbNewBtn", label: "＋ New playbook", cls: "primary" },
    { id: "pbSeedBtn", label: "⟳ Seed" },
  ]);
  $("#pageBody").innerHTML = `
    <div class="filterbar lib-filter">
      <input class="select" id="pbSearch" placeholder="Search playbooks…" style="flex:1;min-width:220px">
      <select class="select" id="pbStatusFilter">
        <option value="">All statuses</option><option value="draft">draft</option><option value="published">published</option>
      </select>
      <select class="select" id="pbCategoryFilter"><option value="">All categories</option></select>
      <select class="select" id="pbSort">
        <option value="newest">Newest first</option><option value="oldest">Oldest first</option><option value="name">Name A–Z</option>
      </select>
    </div>
    <div class="lib-grid" id="pbGrid"><p class="empty">Loading…</p></div>`;
  $("#pbNewBtn").addEventListener("click", newPlaybook);
  $("#pbImportBtn").addEventListener("click", importPlaybook);
  $("#pbSeedBtn").addEventListener("click", seedPlaybooks);
  // delegated actions on playbook cards (grid innerHTML is replaced on each render)
  $("#pbGrid").addEventListener("click", async (e) => {
    const btn = e.target.closest(".pb-card-delete, .pb-card-export, .pb-card-versions, .pb-card-publish");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    if (btn.classList.contains("pb-card-delete")) {
      busyBtn(btn, true);
      await deletePlaybookById(btn.dataset.pid);
      busyBtn(btn, false);
    } else if (btn.classList.contains("pb-card-export")) {
      busyBtn(btn, true);
      await exportPlaybookById(btn.dataset.pid);
      busyBtn(btn, false);
    } else if (btn.classList.contains("pb-card-versions")) {
      await viewPlaybookVersions(btn.dataset.pid);
    } else if (btn.classList.contains("pb-card-publish")) {
      busyBtn(btn, true);
      await publishPlaybookById(btn.dataset.pid);
      busyBtn(btn, false);
    }
  });
  ["input", "change"].forEach((ev) => {
    $("#pbSearch").addEventListener(ev, renderPlaybookGrid);
    $("#pbStatusFilter").addEventListener(ev, renderPlaybookGrid);
    $("#pbCategoryFilter").addEventListener(ev, renderPlaybookGrid);
    $("#pbSort").addEventListener(ev, renderPlaybookGrid);
  });
  await refreshPlaybooks();
  const cats = [...new Set(playbooksCache.map((p) => p.category || "custom"))].sort();
  $("#pbCategoryFilter").innerHTML =
    `<option value="">All categories</option>` + cats.map((c) => `<option>${esc(c)}</option>`).join("");
}

async function pagePlaybookEditor(id) {
  // Prevent duplicate document-level click listeners from accumulating
  document.removeEventListener("click", pbCloseMenuOnOutside);
  selectedId = id;
  graphDef = null; graphSel = null; graphPos = {};
  historyStack = []; futureStack = [];
  inspectTab = "general";
  runStatus = {};
  stepResults = {};
  if (runStatusTimer) { clearInterval(runStatusTimer); runStatusTimer = null; }

  /* restore saved canvas zoom/pan for this playbook */
  let canvasRestored = false;
  const saved = localStorage.getItem(`kmcp_canvas_${id}`);
  if (saved) {
    try {
      const cs = JSON.parse(saved);
      graphZoom = cs.zoom ?? 1;
      graphPan = { x: cs.panX ?? 0, y: cs.panY ?? 0 };
      canvasRestored = true;
    } catch { graphZoom = 1; graphPan = { x: 0, y: 0 }; }
  } else {
    graphZoom = 1; graphPan = { x: 0, y: 0 };
  }

  /* restore inspector hidden state */
  const hid = localStorage.getItem(`kmcp_inspector_hidden_${id}`);
  inspectorHidden = hid === "1";
  const insCol = $("#insCol");
  if (insCol) insCol.classList.toggle("inspector-hidden", inspectorHidden);
  const editorGrid = $("#editorGrid");
  if (editorGrid) editorGrid.classList.toggle("inspector-hidden", inspectorHidden);

  const crumbEl = $("#crumbPage");
  if (crumbEl) crumbEl.textContent = id;
  $("#pageHeadInfo").innerHTML = "";
  $("#pageHeadActions").innerHTML = "";
  const ph = $(".pagehead");
  if (ph) ph.classList.add("hide");
  $("#pageBody").className = "pagebody editor-body";

  $("#pageBody").innerHTML = `
  <section class="editor-shell" id="editorShell">
    <header class="pb-head">
      <div class="pb-head-l">
        <a class="btn ghost back-btn" href="#/playbooks" title="Back to playbooks">↩ Playbooks</a>
        <div class="pb-title">
          <h2 id="pbName">…</h2>
          <div class="pb-meta" id="pbMeta"></div>
        </div>
      </div>
      <div class="pb-head-r">
        <button class="btn" id="pbValidate">Validate</button>
        <button class="btn ghost" id="pbReset" title="Reset test results">↻ Reset</button>
        <button class="btn" id="pbSave" title="Save draft">Save draft</button>
        <button class="btn accent" id="pbRun" title="Run published version with test inputs">▶ Run test</button>
        <button class="btn danger" id="pbCancelRun" title="Cancel current run" hidden>⏹ Cancel</button>
        <button class="btn primary" id="pbPublish">Publish</button>
        <div class="menu" id="pbMenuWrap">
          <button class="btn ghost" id="pbMore" title="More actions">⋯</button>
          <div class="menu-list" id="pbMenuList" hidden>
            <button data-mact="versions">Versions &amp; rollback</button>
            <button data-mact="export">Export JSON</button>
            <button data-mact="import">Import over…</button>
            <button data-mact="delete" class="danger">Delete playbook</button>
          </div>
        </div>
      </div>
    </header>
    <div class="pb-bar">
      <div class="segmented" id="builderSwitch">
        <button data-view="graph" class="active">Graph</button>
        <button data-view="dsl">DSL</button>
      </div>
      <span class="pb-hint dim" id="pbHint"></span>
    </div>
    <div class="editor-grid" id="editorGrid">
      <div class="canvas-col">
        <div class="graph-toolbar" id="graphToolbar">
          <span class="tool-sep"></span>
          <button class="btn icon" id="undoBtn" title="Undo">↶</button>
          <button class="btn icon" id="redoBtn" title="Redo">↷</button>
          <span class="tool-sep"></span>
          <button class="btn" id="addToolBtn" title="Add a registered-tool step">＋ Tool</button>
          <button class="btn" id="addShellBtn" title="Add a shell-command node (inputs → command → filter)">＋ Shell</button>
          <span class="tool-sep"></span>
          <button class="btn icon" id="resetLayoutBtn" title="Auto-layout">⇋</button>
          <button class="btn icon" id="zoomOut" title="Zoom out">−</button>
          <button class="btn icon" id="zoomReset" title="Fit to screen">⛶</button>
          <button class="btn icon" id="zoomIn" title="Zoom in">＋</button>
          <button class="btn icon" id="centerSelBtn" title="Center on selected node">⌂</button>
          <span class="tool-sep"></span>
          <button class="btn icon" id="inspectorToggle" title="Hide/show inspector">◀</button>
          <button class="btn icon" id="delSelBtn" title="Delete selected node">✕</button>
          <span class="tool-sep"></span>
          <span class="count" id="graphCount">0 steps</span>
        </div>
        <div class="canvas-area" id="canvasArea">
          <div class="canvas-inner" id="canvasInner">
            <svg class="edges" id="edgeLayer" width="6000" height="4000" xmlns="http://www.w3.org/2000/svg"></svg>
          </div>
          <div class="canvas-legend">
            <span><i class="legend-dot" style="background:#edc57c"></i>tool</span>
            <span><i class="legend-dot" style="background:#f09685"></i>shell</span>
            <span><i class="legend-dot" style="background:#92dbad"></i>scope / report</span>
            <span><i class="legend-dot" style="background:#aabaff"></i>correlation</span>
          </div>
          <div class="canvas-empty" id="canvasEmpty" hidden>
            <div class="ce-glyph">⌘</div>
            <h3>Your workflow starts here</h3>
            <p>Add a step to begin — use ＋ Tool (registered security tool) or ＋ Shell (command node) in the graph toolbar.</p>
            <button class="btn primary" id="firstStepBtn">Add first step</button>
          </div>
        </div>
      </div>
      <div class="ins-resizer" id="insResizer" title="Drag to resize the inspector"></div>
      <div class="ins-col" id="insCol">
        <aside class="inspector" id="inspector"></aside>
      </div>
    </div>
    <div class="dsl-panel" id="dslPanel" hidden>
      <textarea id="dslCode" class="code-area" spellcheck="false" placeholder="Playbook definition (JSON as returned by /definition)…"></textarea>
      <div class="dsl-bar">
        <button class="btn icon" id="dslUndo" title="Undo (Ctrl+Z)">↶</button>
        <button class="btn icon" id="dslRedo" title="Redo (Ctrl+Y)">↷</button>
        <span class="tool-sep"></span>
        <button class="btn" id="dslSyncToDsl">Sync graph → DSL</button>
        <button class="btn" id="dslSyncToGraph">Sync DSL → graph</button>
        <span class="dsl-valid-chip" id="dslValidChip">—</span>
        <span class="dim">Canonical model — the graph above edits this same JSON. Graph ⇄ DSL is always in sync.</span>
      </div>
    </div>
    <div class="result-wrap"><pre class="result" id="editorResult" hidden></pre></div>
  </section>`;

  /* ── load playbook meta + definition ── */
  try {
    const [d, defn] = await Promise.all([
      api(`/playbooks/${encodeURIComponent(id)}`),
      api(`/playbooks/${encodeURIComponent(id)}/definition`).catch(() => ({ definition: null })),
    ]);
    const def = defn.definition;
    $("#pbName").textContent = d.name || id;
    if (crumbEl) crumbEl.textContent = d.name || id;
    $("#pbMeta").innerHTML = `${BADGE(d.status)} <span class="version">v ${esc(d.current_version_id || "no version")}</span><span class="dim mono">${esc(id)}</span>`;
    rawDsl = def ? JSON.stringify(def, null, 2) : "";
    $("#dslCode").value = rawDsl;
    // Restore unsaved DSL draft from localStorage if present
    const saved = localStorage.getItem(`kmcp_dsl_${selectedId}`);
    if (saved && saved !== rawDsl) {
      rawDsl = saved;
      $("#dslCode").value = rawDsl;
    }
    $("#dslCode").addEventListener("input", onDslInput);
    $("#dslCode").addEventListener("change", onDslInput);
    $("#dslCode").addEventListener("blur", onDslInput);
    updateDslValidChip();
    $("#dslUndo").disabled = !dslHistory.length;
    $("#dslRedo").disabled = !dslFuture.length;

    function onDslInput() {
      const text = $("#dslCode").value || "";
      // Track every edit for undo/redo regardless of JSON validity.
      // JSON is validated at save/publish time, not on every keystroke.
      if (text !== rawDsl) {
        dslHistory.push(rawDsl);
        if (dslHistory.length > 40) dslHistory.shift();
        dslFuture = [];
        rawDsl = text;
        localStorage.setItem(`kmcp_dsl_${selectedId}`, rawDsl);
      }
      updateDslValidChip();
      $("#dslUndo").disabled = !dslHistory.length;
      $("#dslRedo").disabled = !dslFuture.length;
    }
  } catch (err) {
    $("#pbName").textContent = id;
    $("#pbMeta").innerHTML = `<span class="error">${esc(err.message)}</span>`;
  }

  $("#editorShell").style.setProperty("--ins-w", "380px");

  $("#firstStepBtn").addEventListener("click", () => addToolStep());
  $("#undoBtn").addEventListener("click", undoGraph);
  $("#redoBtn").addEventListener("click", redoGraph);
  $("#addToolBtn").addEventListener("click", () => addToolStep());
  $("#addShellBtn").addEventListener("click", () => addShellStep());
  $("#resetLayoutBtn").addEventListener("click", resetGraphLayout);
  $("#zoomIn").addEventListener("click", () => nudgeZoom(+0.2));
  $("#centerSelBtn").addEventListener("click", centerOnSelection);
  $("#zoomOut").addEventListener("click", () => nudgeZoom(-0.2));
  $("#zoomReset").addEventListener("click", fitToScreen);

  /* hide/show inspector toggle */
  const insToggle = $("#inspectorToggle");
  if (insToggle) {
    insToggle.addEventListener("click", () => {
      inspectorHidden = !inspectorHidden;
      const insCol = $("#insCol");
      if (insCol) insCol.classList.toggle("inspector-hidden", inspectorHidden);
      const editorGrid = $("#editorGrid");
      if (editorGrid) editorGrid.classList.toggle("inspector-hidden", inspectorHidden);
      if (selectedId) {
        localStorage.setItem(`kmcp_inspector_hidden_${selectedId}`, inspectorHidden ? "1" : "0");
      }
      insToggle.textContent = inspectorHidden ? "▶" : "◀";
      insToggle.title = inspectorHidden ? "Show inspector" : "Hide inspector";
    });
  }
  $("#delSelBtn").addEventListener("click", () => { if (graphSel) deleteGraphStep(graphSel); else toast("Select a node on the canvas first"); });

  $$("#builderSwitch button").forEach((btn) =>
    btn.addEventListener("click", () => switchBuilderView(btn.dataset.view)));

  /* DSL undo / redo */
  $("#dslUndo")?.addEventListener("click", () => {
    if (!dslHistory.length) return toast("Nothing to undo");
    dslFuture.push(rawDsl);
    rawDsl = dslHistory.pop();
    $("#dslCode").value = rawDsl;
    localStorage.setItem(`kmcp_dsl_${selectedId}`, rawDsl);
    $("#dslUndo").disabled = !dslHistory.length;
    $("#dslRedo").disabled = !dslFuture.length;
    toast("Undo");
  });
  $("#dslRedo")?.addEventListener("click", () => {
    if (!dslFuture.length) return toast("Nothing to redo");
    dslHistory.push(rawDsl);
    rawDsl = dslFuture.pop();
    $("#dslCode").value = rawDsl;
    localStorage.setItem(`kmcp_dsl_${selectedId}`, rawDsl);
    $("#dslUndo").disabled = !dslHistory.length;
    $("#dslRedo").disabled = !dslFuture.length;
    toast("Redo");
  });

  /* DSL <-> graph sync buttons */
  $("#dslSyncToDsl")?.addEventListener("click", syncGraphToDsl);
  $("#dslSyncToGraph")?.addEventListener("click", syncDslToGraph);



  $("#pbValidate").addEventListener("click", validateDefinition);
  $("#pbReset").addEventListener("click", () => {
    localStorage.setItem('kmcp_reset_' + selectedId, '1');
    runStatus = {};
    stepResults = {};
    renderBuilder();
    toast("Test results cleared");
  });
  $("#pbSave").addEventListener("click", saveDraft);
  $("#pbPublish").addEventListener("click", publish);
  $("#pbRun").addEventListener("click", () => runModal(selectedId));
  $("#pbCancelRun").addEventListener("click", cancelCurrentRun);
  $("#pbMore").addEventListener("click", () => {
    const l = $("#pbMenuList");
    if (l) l.hidden = !l.hidden;
  });
  $$("#pbMenuList button").forEach((btn) =>
    btn.addEventListener("click", async () => {
      $("#pbMenuList").hidden = true;
      const act = btn.dataset.mact;
      if (act === "versions") showVersions();
      else if (act === "export") exportPlaybook();
      else if (act === "import") importPlaybook();
      else if (act === "delete") { busyBtn(btn, true); await deletePlaybook(); busyBtn(btn, false); }
    }));
  document.addEventListener("click", pbCloseMenuOnOutside);

  initInspectorResizer();
  initCanvasGestures();
  refreshTools().catch(() => {});
  // Skip re-loading run results if the user reset them (persists across refreshes
  // until the next run clears the marker).
  if (!localStorage.getItem('kmcp_reset_' + selectedId)) {
    loadRunStatus(selectedId).catch(() => {});
  }
  switchBuilderView("graph");
  // keep large graphs fully visible on entry — unless the user has a saved
  // canvas position/zoom for this playbook, in which case restore that instead.
  if (getGraphSteps().length >= 3 && !canvasRestored) {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => fitToScreen());
    else fitToScreen();
  }
}

function pbCloseMenuOnOutside(e) {
  const wrap = $("#pbMenuWrap");
  if (!wrap) { document.removeEventListener("click", pbCloseMenuOnOutside); return; }
  if (!wrap.contains(e.target)) { const l = $("#pbMenuList"); if (l) l.hidden = true; }
}

/* ── canvas gestures: trackpad pinch-zoom, scroll pan, drag pan ───── */
/* macOS trackpad: two-finger pinch → wheel + ctrlKey (Chrome/Edge) or
   gesturechange (Safari). Two-finger scroll pans. Click-drag on empty
   canvas pans too, like the run-detail graphs you already know. */

function initCanvasGestures() {
  const area = $("#canvasArea");
  if (!area || area.dataset.gestures) return;
  area.dataset.gestures = "1";

  /* two-finger scroll / shift-wheel → pan (natural-scroll friendly) */
  area.addEventListener("wheel", (e) => {
    if (e.ctrlKey) {
      // trackpad pinch — zoom anchored at the pointer
      e.preventDefault();
      // cap per-event step so momentum pinch doesn't overshoot
      const factor = Math.min(1.6, Math.max(0.6, Math.exp(-e.deltaY * 0.01)));
      canvasZoomAt(e.clientX, e.clientY, factor);
      return;
    }
    // plain two-finger scroll: pan the workflow instead of scrolling the page
    e.preventDefault();
    const lines = e.deltaMode === 1 ? 16 : 1; // normalize firefox "lines" mode
    graphPan = { x: graphPan.x - e.deltaX * lines, y: graphPan.y - e.deltaY * lines };
    applyZoom();
  }, { passive: false });

  /* Safari pinch (aspect=0 for full zoom pinches) */
  let gestureScale = null;
  area.addEventListener("gesturestart", (e) => { gestureScale = e.scale || 1; e.preventDefault(); });
  area.addEventListener("gesturechange", (e) => {
    if (!gestureScale) return;
    e.preventDefault();
    const factor = Math.min(1.6, Math.max(0.6, (e.scale || 1) / gestureScale));
    canvasZoomAt(e.clientX, e.clientY, factor);
    gestureScale = e.scale || 1;
  });
  area.addEventListener("gestureend", () => { gestureScale = null; });

  /* click-drag on empty canvas → pan */
  area.addEventListener("mousedown", (e) => {
    if (e.button !== 0 && e.button !== 1) return;
    if (e.target instanceof Element &&
        e.target.closest(".node, .port, .canvas-empty button")) return;
    const sx = e.clientX, sy = e.clientY, ox = graphPan.x, oy = graphPan.y;
    let moved = false;
    canvasJustPanned = false;
    document.body.classList.add("canvas-panning");
    const move = (ev) => {
      const dx = ev.clientX - sx, dy = ev.clientY - sy;
      if (!moved && Math.hypot(dx, dy) < 4) return;
      moved = true;
      graphPan = { x: ox + dx, y: oy + dy };
      applyZoom();
    };
    const up = () => {
      document.body.classList.remove("canvas-panning");
      canvasJustPanned = moved;
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
    e.preventDefault();
  });

  /* Clicking empty canvas deselects the selected node (a pan drag doesn't count). */
  area.addEventListener("click", (e) => {
    if (canvasJustPanned) { canvasJustPanned = false; return; }
    if (e.target.closest(".node, .port, .canvas-empty button")) return;
    if (graphSel === null) return;
    graphSel = null;
    const inner = $("#canvasInner");
    if (inner) $$(".node", inner).forEach((n) => n.classList.remove("selected"));
    renderInspector();
    syncToolbarButtons();
  });
}

function initInspectorResizer() {
  const handle = $("#insResizer");
  const shell = $("#editorShell");
  if (!handle || !shell) return;
  let startX = 0, startW = 0, dragging = false;
  const move = (ev) => {
    if (!dragging) return;
    const w = Math.min(640, Math.max(300, Math.round(startW + (startX - ev.clientX))));
    shell.style.setProperty("--ins-w", `${w}px`);
  };
  const up = () => {
    dragging = false;
    document.body.classList.remove("resizing");
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
  };
  handle.addEventListener("mousedown", (e) => {
    dragging = true;
    document.body.classList.add("resizing");
    startX = e.clientX;
    startW = parseInt(getComputedStyle($("#insCol")).width, 10) || 380;
    e.preventDefault();
  });
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
}

/* ── canonical model: graphDef ⇄ DSL — a single source of truth ────── */

const NODE_W = 280, NODE_H = 140, X_GAP = 360, Y_GAP = 200;
let builderView = "dsl";
let graphDef = null;          // canonical definition object while editing
let rawDsl = "";              // raw JSON text (source of truth for saves)
let graphSel = null;          // selected step id
let graphPos = {};            // {stepId:{x,y}} drag overrides
let graphZoom = 1;
let graphPan = { x: 0, y: 0 };
let inspectorHidden = false;
let historyStack = [];
let futureStack = [];
let dslHistory = [];   // undo stack for DSL edits
let dslFuture = [];    // redo stack for DSL edits
let inspectTab = "general";
let inspTabByNode = new Map();   // node id -> last active inspector tab id
let runStatus = {};           // {stepId: status} from the latest execution
let stepResults = {};         // {stepId: {outputs, error, tool_id}} from the latest execution
let runStatusTimer = null;
let currentExecId = null;   // execution id of the run started from this editor

const TYPE_ICONS = {
  tool: { cls: "scan", glyph: "◎" },
  shell: { cls: "cmd", glyph: "❯" },
  scope_check: { cls: "dns", glyph: "✓" },
  finding_correlation: { cls: "web", glyph: "⇄" },
  assessment_report: { cls: "out", glyph: "▤" },
  custom: { cls: "mod", glyph: "▣" },
};
const iconFor = (s) => TYPE_ICONS[s?.type] || { cls: "scan", glyph: "◈" };

function getGraphSteps() { return Array.isArray(graphDef?.steps) ? graphDef.steps : []; }

function stepById(id) { return getGraphSteps().find((s) => s.id === id); }

function emptyDefinition(name) {
  return { name: name || "draft", version: "1.0.0", inputs: {}, steps: [], execution: { timeout: 900, max_parallel: 4 } };
}

function loadGraphFromDSL() {
  const raw = rawDsl?.trim();
  if (raw) {
    try {
      graphDef = JSON.parse(raw);
    } catch (err) {
      toast("DSL is not valid JSON — fix the DSL before opening the graph: " + err.message, true);
      return false;
    }
  } else {
    graphDef = emptyDefinition(selectedId);
  }
  if (!Array.isArray(graphDef.steps)) graphDef.steps = [];
  graphSel = null;
  graphPos = {};
  return true;
}

function captureGraphToDSL() {
  if (builderView !== "graph" || !graphDef) return;
  const t = $("#dslCode");
  if (t) { rawDsl = JSON.stringify(graphDef, null, 2); t.value = rawDsl; }
}

/* Reveal whether the DSL textarea currently holds parseable JSON. */
function updateDslValidChip() {
  const chip = $("#dslValidChip");
  if (!chip) return;
  const text = ($("#dslCode")?.value || "").trim();
  let valid = true;
  try { JSON.parse(text || "{}"); } catch { valid = false; }
  chip.textContent = valid ? "valid JSON" : "invalid JSON";
  chip.className = "dsl-valid-chip" + (valid ? " valid" : " invalid");
}

/* Write the in-memory graph model to the DSL textarea (force reconciliation). */
function syncGraphToDsl() {
  if (builderView === "dsl") { if (!loadGraphFromDSL()) return; }
  applyTabFields(stepById(graphSel));
  const t = $("#dslCode");
  rawDsl = graphDef ? JSON.stringify(graphDef, null, 2) : "";
  if (t) t.value = rawDsl;
  localStorage.setItem(`kmcp_dsl_${selectedId}`, rawDsl);
  updateDslValidChip();
  toast("Synced graph → DSL");
}

/* Parse the DSL textarea and switch to the graph view (force reconciliation). */
function syncDslToGraph() {
  const t = $("#dslCode");
  if (!t) return;
  const text = t.value.trim();
  try { JSON.parse(text || "{}"); } catch (err) {
    toast("DSL is not valid JSON — fix it before syncing to graph: " + err.message, true);
    return;
  }
  rawDsl = text;
  localStorage.setItem(`kmcp_dsl_${selectedId}`, rawDsl);
  switchBuilderView("graph");
  toast("Synced DSL → graph");
}

function switchBuilderView(view) {
  // Always sync rawDsl from the DSL textarea first, so unsaved edits are
  // never lost when switching between graph and DSL views.
  const t = $("#dslCode");
  if (t) rawDsl = t.value || rawDsl;

  if (view === builderView && view === "graph" && graphDef) { renderBuilder(); return; }
  if (builderView === "graph") { applyTabFields(stepById(graphSel)); captureGraphToDSL(); }
  if (view === "graph") {
    if (!loadGraphFromDSL()) return;   // invalid JSON: stay in DSL with a toast
  }
  builderView = view;
  const dv = $("#dslPanel"), gr = $("#editorGrid");
  if (dv) dv.hidden = view !== "dsl";
  if (gr) gr.hidden = view !== "graph";
  $$("#builderSwitch button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (view === "graph") renderBuilder();
  else if (view === "dsl") {
    const tt = $("#dslCode");
    if (tt) tt.value = rawDsl;
  }
}

/* ── undo / redo (JSON snapshots of the canonical model) ───────────── */

function pushHistory() {
  if (builderView !== "graph" || !graphDef) return;
  historyStack.push(JSON.stringify(graphDef));
  if (historyStack.length > 40) historyStack.shift();
  futureStack = [];
}

function undoGraph() {
  if (!historyStack.length) return toast("Nothing to undo");
  if (builderView !== "graph") return;
  futureStack.push(JSON.stringify(graphDef));
  graphDef = JSON.parse(historyStack.pop());
  graphSel = null;
  captureGraphToDSL();
  renderBuilder();
}

function redoGraph() {
  if (!futureStack.length) return toast("Nothing to redo");
  if (builderView !== "graph") return;
  historyStack.push(JSON.stringify(graphDef));
  graphDef = JSON.parse(futureStack.pop());
  graphSel = null;
  captureGraphToDSL();
  renderBuilder();
}

/* ── layout: layering + positions ─────────────────────────────────── */

/* cycle-safe longest-path layering (iterative relaxation) */
function graphLayout(depths) {
  const steps = getGraphSteps();
  const ids = new Set(steps.map((s) => s.id));
  steps.forEach((s) => { depths[s.id] = 0; });
  for (let iter = 0; iter < steps.length + 1; iter++) {
    let changed = false;
    for (const s of steps) {
      const deps = (s.depends_on || []).filter((d) => ids.has(d) && d !== s.id);
      const d = deps.length ? Math.max(...deps.map((x) => (depths[x] ?? 0) + 1)) : 0;
      if (d > depths[s.id]) { depths[s.id] = d; changed = true; }
    }
    if (!changed) break;
  }
}

function nodePos(id, depths) {
  if (graphPos[id]) return graphPos[id];
  const depth = depths[id] ?? 0;
  const sameLane = getGraphSteps().filter((s) => (depths[s.id] ?? 0) === depth);
  const yIdx = sameLane.findIndex((s) => s.id === id);
  return { x: 40 + depth * X_GAP, y: 40 + yIdx * Y_GAP };
}

function applyZoom() {
  const inner = $("#canvasInner");
  if (inner) inner.style.transform = `translate(${graphPan.x}px,${graphPan.y}px) scale(${graphZoom})`;
  /* persist canvas state */
  if (selectedId) {
    localStorage.setItem(`kmcp_canvas_${selectedId}`, JSON.stringify({
      zoom: graphZoom, panX: graphPan.x, panY: graphPan.y
    }));
  }
}

/* zoom keeping the canvas point under clientX/clientY anchored in place */
function canvasZoomAt(clientX, clientY, factor) {
  const area = $("#canvasArea");
  if (!area) return;
  const rect = area.getBoundingClientRect();
  const ox = clientX - rect.left, oy = clientY - rect.top;
  const nz = Math.round(Math.min(2.5, Math.max(0.2, graphZoom * factor)) * 1000) / 1000;
  if (nz === graphZoom) return;
  const k = nz / graphZoom;
  graphZoom = nz;
  graphPan = { x: ox - (ox - graphPan.x) * k, y: oy - (oy - graphPan.y) * k };
  applyZoom();
}

function nudgeZoom(delta) {
  const area = $("#canvasArea");
  if (!area) return;
  const rect = area.getBoundingClientRect();
  canvasZoomAt(rect.left + area.clientWidth / 2, rect.top + area.clientHeight / 2, 1 + delta);
}

function fitToScreen() {
  const steps = getGraphSteps();
  if (!steps.length || builderView !== "graph") { graphZoom = 1; graphPan = { x: 0, y: 0 }; applyZoom(); return; }
  const area = $("#canvasArea");
  if (!area) return;
  const depths = {};
  graphLayout(depths);
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const s of steps) {
    const p = nodePos(s.id, depths);
    minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x + NODE_W); maxY = Math.max(maxY, p.y + NODE_H);
  }
  const pad = 48;
  const w = Math.max(1, area.clientWidth - 2 * pad), h = Math.max(1, area.clientHeight - 2 * pad);
  const z = Math.max(0.25, Math.min(1.25, w / (maxX - minX), h / (maxY - minY)));
  graphZoom = Math.round(z * 100) / 100;
  graphPan = {
    x: Math.round((w - (maxX - minX) * graphZoom) / 2 - minX * graphZoom + pad),
    y: Math.round((h - (maxY - minY) * graphZoom) / 2 - minY * graphZoom + pad),
  };
  applyZoom();
}

/* Pan (and lightly zoom) the canvas so the selected node sits center screen. */
function centerOnSelection() {
  if (!graphSel) { toast("Select a node first"); return; }
  const depths = {};
  graphLayout(depths);
  const p = nodePos(graphSel, depths);
  const area = $("#canvasArea");
  if (!area) return;
  const rect = area.getBoundingClientRect();
  const cx = rect.width / 2, cy = rect.height / 2;
  const nx = p.x + NODE_W / 2, ny = p.y + NODE_H / 2;
  /* nudge zoom toward 1 so a distant node becomes comfortably viewable */
  const targetZoom = Math.min(graphZoom, 1);
  graphZoom = Math.round(Math.min(1.25, Math.max(0.4, targetZoom)) * 100) / 100;
  graphPan = {
    x: Math.round(cx - nx * graphZoom),
    y: Math.round(cy - ny * graphZoom),
  };
  applyZoom();
  toast(`Centered on ${esc(graphSel)}`);
}

function resetGraphLayout() { pushHistory(); graphPos = {}; graphZoom = 1; graphPan = { x: 0, y: 0 }; renderBuilder(); toast("Layout reset"); }

/* ── node rendering ────────────────────────────────────────────────── */

function runStatusClass(id) {
  const st = runStatus[id];
  if (!st) return "";
  if (st === "completed" || st === "ok") return "ok";
  if (st === "failed" || st === "cancelled") return "failed";
  if (st === "running" || st === "pending") return "run";
  return "";
}

function renderBuilder() {
  try {
    const inner = $("#canvasInner");
  if (!inner) return;
  if (!graphDef) loadGraphFromDSL();
  const steps = getGraphSteps();
  const depths = {};
  graphLayout(depths);

  $$(".node", inner).forEach((n) => n.remove());
  applyZoom();
  const empty = $("#canvasEmpty");
  if (empty) empty.hidden = steps.length > 0;
  const legend = $(".canvas-legend");
  if (legend) legend.style.display = steps.length ? "" : "none";

  for (const s of steps) {
    const p = nodePos(s.id, depths);
    const ic = iconFor(s);
    let bodyHtml = "";
    if (s.type === "shell") {
      const inRows = Object.entries(s.inputs || {})
        .filter(([k]) => k)
        .map(([k, v]) => `<div class="shell-line in"><i>in</i><b>${esc(k)}</b><span>${esc(String(v))}</span></div>`)
        .join("") || `<div class="shell-line in"><i>in</i><span class="dim">no inputs</span></div>`;
      const cmd = s.command ? esc(s.command) : '<span class="dim">no command</span>';
      const f = s.filter && s.filter.type !== "none" ? s.filter : null;
      const fLine = f
        ? `<div class="shell-line flt"><i>⌕</i><b>${esc(f.type || "grep")}</b><span>${esc(String(f.pattern || "").slice(0, 46))}</span></div>`
        : `<div class="shell-line flt"><i>⌕</i><span class="dim">no filter</span></div>`;
      bodyHtml = `${inRows}<div class="shell-line cmd"><i>❯</i><span class="cmd-txt">${cmd}</span></div>${fLine}`;
    } else {
      const isTool = s.type === "tool";
      const argSummary = isTool
        ? `tool: ${s.tool || "?"} · ${Object.keys(s.args || {}).length} arg(s)`
        : Object.keys(s.args || {}).length ? `${Object.keys(s.args || {}).length} arg(s)` : "internal stage";
      bodyHtml = esc(argSummary) + (s.when ? `<br/>when: ${esc(s.when)}` : "");
    }
    const stCls = runStatusClass(s.id);
    const stTxt = runStatus[s.id] ? `run status: ${runStatus[s.id]}` : "";
    const res = stepResults[s.id];
    const resHtml = res
      ? `<div class="node-result ${res.error ? 'err' : ''}">${renderNodeOutput(res)}</div>`
      : "";

function renderNodeOutput(res) {
  const out = res && res.outputs ? res.outputs : null;
  const err = res.error;
  const hasErr = !!err;

  let html = "";

  /* Component-style key: value lines for common keys (click label to copy value) */
  const commonKeys = ["tool", "command", "stdout", "stderr", "exit_code", "success", "timed_out", "duration", "start_time", "end_time"];
  const valColor = {
    tool: "val-tool", command: "val-cmd", stdout: "val-stdout", stderr: "val-stderr",
    exit_code: "val-exit", success: "val-bool", timed_out: "val-bool",
    duration: "val-dur", start_time: "val-time", end_time: "val-time"
  };
  if (out && typeof out === "object" && !Array.isArray(out)) {
    for (const k of commonKeys) {
      if (k in out) {
        let v = String(out[k]);
        /* Compact ISO timestamps: "2026-11-08T12:02:52.779699+00:00" -> "2026-11-08 12:02:52" */
        if ((k === "start_time" || k === "end_time")) {
          const m = v.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/);
          if (m) v = `${m[1]} ${m[2]}`;
        }
        const vc = valColor[k] || "";
        html += `<div class="result-line"><span class="result-key copyable" data-copy-value="${esc(v)}" title="click to copy value">${esc(k)}:</span> <span class="result-val ${vc}">${esc(v)}</span></div>`;
      }
    }
  }

  if (hasErr) {
    html += `<div class="result-line err-line"><span class="result-key">error:</span> <span class="result-val err">${esc(String(err))}</span></div>`;
  }

  /* Show only extra outputs (common keys removed) as colorized JSON */
  const extra = {};
  if (out && typeof out === "object" && !Array.isArray(out)) {
    for (const [k, v] of Object.entries(out)) {
      if (!commonKeys.includes(k)) extra[k] = v;
    }
  }
  const extraKeys = Object.keys(extra);
  if (extraKeys.length) {
    html += `<span class="result-label">extra outputs</span>`;
    html += `<div class="result-val jsonic">${colorJson(extra)}</div>`;
  } else if (!html) {
    html += `<span class="result-val dim">no output</span>`;
  }

  return html;
}
    const el = document.createElement("div");
    const running = runStatus[s.id] === "running" || runStatus[s.id] === "pending";
    el.className = `node ${s.type === "shell" ? "shell " : ""}${running ? "running " : ""}${graphSel === s.id ? "selected" : ""}`;
    el.style.left = `${p.x}px`; el.style.top = `${p.y}px`;
    el.dataset.step = s.id;
    el.innerHTML = `
      <div class="node-top">
        <div class="node-icon ${ic.cls}">${ic.glyph}</div>
        <div class="node-names"><div class="node-title">${esc(s.id)}</div>
          <div class="node-kind">${esc(s.type || "step")}${s.tool ? ` · ${esc(s.tool)}` : ""}</div></div>
        <span class="node-status ${stCls}" title="${esc(stTxt)}"></span>
      </div>
      <div class="node-body">${bodyHtml}</div>
      ${resHtml}
      <div class="node-foot">
        <span class="chip">${(s.depends_on || []).length} in</span>
        ${Object.keys(s.inputs || {}).length ? `<span class="chip">${Object.keys(s.inputs || {}).length} inputs</span>` : ""}
        ${s.filter && s.filter.type !== "none" ? `<span class="chip accent">⌕ out</span>` : ""}
        ${s.checkpoint ? '<span class="chip ck">checkpoint</span>' : ""}
      </div>
      <span class="port left" title="input port"></span><span class="port right" title="drag or click to connect"></span>`;
    el.addEventListener("mousedown", (e) => startNodeDrag(e, s.id, p));
    const rightPort = el.querySelector(".port.right");
    if (rightPort) rightPort.addEventListener("mousedown", (e) => startPortConnect(e, s.id));
    inner.appendChild(el);
  }

  // edges
  const svg = $("#edgeLayer");
  let edges = "";
  for (const s of steps) {
    for (const dep of s.depends_on || []) {
      if (!depths.hasOwnProperty(dep) || dep === s.id) continue;
      const a = nodePos(dep, depths), b = nodePos(s.id, depths);
      const y1 = a.y + NODE_H / 2, y2 = b.y + NODE_H / 2;
      const hot = graphSel === dep || graphSel === s.id ? " hot" : "";
      edges += `<path class="edge${hot}" d="M${a.x + NODE_W},${y1} C${(a.x + NODE_W + b.x) / 2},${y1} ${(a.x + NODE_W + b.x) / 2},${y2} ${b.x},${y2}" data-dep="${esc(dep)}" data-step="${esc(s.id)}" stroke-width="4" vector-effect="non-scaling-stroke"/>`;
    }
  }
  svg.innerHTML = edges;

  // toolbar state
  const cnt = $("#graphCount");
  if (cnt) cnt.textContent = `${steps.length} step${steps.length === 1 ? "" : "s"}`;
  syncToolbarButtons();

  renderInspector();
  } catch (err) {
    const inner = $("#canvasInner");
    if (inner) inner.innerHTML = `<p class="error" style="padding:20px">Render error: ${esc(err.message)}</p>`;
    console.error("renderBuilder failed:", err);
  }
}

/* Keep the graph undo/redo/delete buttons in sync with the stack/selection. */
function syncToolbarButtons() {
  const ub = $("#undoBtn"), rb = $("#redoBtn"), db = $("#delSelBtn");
  if (ub) ub.disabled = !historyStack.length;
  if (rb) rb.disabled = !futureStack.length;
  if (db) db.disabled = !graphSel;
}

/* ── inspector (tabbed, type-aware) ────────────────────────────────── */

const INS_TABS = ["general", "inputs", "outputs", "advanced"];
const INS_TAB_LABELS = { general: "General", inputs: "Inputs", outputs: "Outputs", advanced: "Advanced" };

function parseRef(v) {
  const t = String(v ?? "").trim();
  const m = t.match(/^\{\{(\w+)\}\}$/);
  if (m) return { mode: "run", name: m[1] };
  const u = t.match(/^\{\{(\w+)\.(\w+)\}\}$/);
  if (u) return { mode: "upstream", step: u[1], field: u[2] };
  return { mode: "literal", val: t };
}

function defInputs(def) {
  const raw = def?.inputs;
  if (Array.isArray(raw)) return raw.filter((x) => typeof x === "object" && x?.name).map((x) => ({ name: x.name, spec: x }));
  if (raw && typeof raw === "object") return Object.entries(raw).map(([name, spec]) => ({ name, spec: typeof spec === "object" && spec ? spec : {} }));
  return [];
}

/* collect run-input names referenced as {{name}} across step inputs, args and
   shell commands — these are the inputs a playbook actually takes at launch */
function collectRunInputs(def) {
  const names = new Set();
  const stepInputDefaults = {};
  const add = (v) => {
    if (typeof v !== "string") return;
    for (const m of v.matchAll(/\{\{\s*([a-zA-Z_][\w]*)\s*\}\}/g)) names.add(m[1]);
  };
  for (const s of def?.steps || []) {
    if (s.inputs) {
      for (const [k, v] of Object.entries(s.inputs)) {
        stepInputDefaults[k] = v; // literal default for this input key
        add(v);
      }
    }
    if (s.command) add(s.command);
    if (s.args) for (const v of Object.values(s.args)) add(v);
  }
  const result = [...names].map((name) => ({ name, spec: { type: "string", required: true } }));
  // step input keys that aren't run-input references become optional literal inputs
  for (const [k, v] of Object.entries(stepInputDefaults)) {
    if (!names.has(k) && typeof v === "string" && !v.match(/^\{\{[a-zA-Z_]\w*\}\}$/)) {
      result.push({ name: k, spec: { type: "string", required: false, default: v } });
    }
  }
  return result;
}

function namedGroups(pattern) {
  const out = [];
  const re = /\(\?P<([A-Za-z_]\w*)>/g;
  let m;
  while ((m = re.exec(String(pattern || "")))) out.push(m[1]);
  return out;
}

function upstreamOutFields(stepId) {
  const s = stepById(stepId);
  if (!s) return [];
  return namedGroups(s.filter?.pattern);
}

/* ── inspector help (hover tooltip "icons") ─────────────────────────── */

function helpTip(text) {
  const t = String(text || "").replace(/<br\s*\/?>/gi, "\n").replace(/<[^>]+>/g, "").trim();
  if (!t) return "";
  return `<span class="help-tip-wrap"><button type="button" class="help-tip" data-tip="${esc(t)}" aria-label="Help: ${esc(t)}" tabindex="0">?</button></span>`;
}

function insLabel(text, control, tip) {
  return `<label class="ins-field">
    <span class="ins-flabel"><span class="ins-flabel-txt">${esc(text)}</span>${helpTip(tip)}</span>${control}</label>`;
}

/* same affordance for section headings */
function subLabel(text, tip, extra = "") {
  return `<div class="ins-subhead">${esc(text)}${extra}${helpTip(tip)}</div>`;
}

function insDependsField(s) {
  return insLabel("Depends on", `<input class="select" id="insDeps" value="${esc((s.depends_on || []).join(", "))}" placeholder="comma separated upstream step ids">`,
    "Steps that must finish before this one runs. Enter their <b>Step ids</b>, comma separated — e.g. <code>recon_1, parse_2</code>. Upstream steps are always implicit prerequisites, this wires extra ones.");
}

function insGeneralTab(s) {
  const typeFixed = `
    <div class="ins-field">
      <span class="ins-flabel">Step type</span>
      <div class="type-static"><span class="node-icon cmd">❯</span> shell command <span class="dim">(inputs → command → output filter)</span></div>
    </div>`;
  const typeSel = insLabel("Step type", `
    <select class="select" id="insType">
      ${["tool", "shell", "scope_check", "finding_correlation", "assessment_report", "custom"]
        .map((v) => `<option value="${v}" ${v === (s.type || "tool") ? "selected" : ""}>${v}</option>`).join("")}
    </select>`,
    "<b>tool</b> runs a registry tool · <b>shell</b> runs your own command · <b>custom</b> = internal module stage · the rest are built-in reporting/scope stages. Changing type resets tool-specific settings.");
  const toolSel = s.type === "tool" ? insLabel("Tool", `
    <input class="select" id="insTool" value="${esc(s.tool || "")}" placeholder="e.g. nuclei" list="dlTools">` +
    `<datalist id="dlTools">${(toolsCache || []).map((t) => `<option value="${esc(t.name)}"></option>`).join("")}</datalist>`,
    "Pick a registered tool — its argument form on the Inputs tab follows this choice. Names come from the Tool Registry (see the Tools page).") : "";
  const cmdField = s.type === "shell" ? insLabel("Shell command", `
    <textarea class="select area mono" id="insCommand" style="min-height:64px" spellcheck="false" placeholder="nmap {ip} -sn — {name} placeholders fill from the Inputs tab">${esc(s.command || "")}</textarea>`,
    "Any shell command. Put <code>{name}</code> placeholders where mapped inputs land — e.g. <code>nmap -sn {ip}</code> with an input named <b>ip</b> mapped to the run input or an upstream field.") : "";
  return `
    ${insLabel("Step id", `<input class="select mono" id="insId" value="${esc(s.id)}">`,
      "Unique name used on the canvas and to reference this step's outputs as <code>{{thisid.field}}</code> downstream. Renaming updates all references automatically.")}
    ${s.type === "shell" ? typeFixed : typeSel}
    ${toolSel}
    ${cmdField}
    ${insDependsField(s)}
    <div class="checkline">${insLabel("Checkpoint", `<input type="checkbox" id="insCheckpoint" ${s.checkpoint ? "checked" : ""}>`, "Persists the run state after this step so a failed run can be resumed from here instead of starting over.")}</div>`;
}

/* ── schema-driven tool arguments ─────────────────────────────────── */

function toolInputSchema(toolName) {
  if (!toolName) return null;
  const t = (toolsCache || []).find(
    (x) => String(x.name || "").toLowerCase() === String(toolName).toLowerCase());
  const sc = t?.input_schema;
  if (!sc || sc.type !== "object" || !sc.properties || !Object.keys(sc.properties).length) return null;
  return sc;
}

function argControlHtml(name, prop, val) {
  const type = String(prop?.type || "string");
  const common = `data-arg="${esc(name)}" data-argtype="${type}"`;
  if (type === "boolean") {
    const v = val === undefined ? (prop?.default ?? null) : val;
    return `<select class="select" ${common}>
      <option value="" ${v === null || v === undefined ? "selected" : ""}>— not set</option>
      <option value="true" ${v === true ? "selected" : ""}>true</option>
      <option value="false" ${v === false ? "selected" : ""}>false</option>
    </select>`;
  }
  if (type === "integer" || type === "number") {
    const v = val !== undefined ? val : prop?.default;
    return `<input class="select" type="number" ${common}
      value="${v === undefined || v === null ? "" : esc(String(v))}" placeholder="${type === "number" ? "e.g. 1.5" : "e.g. 30"}">`;
  }
  if (type === "array" || type === "object") {
    const v = val !== undefined ? val : prop?.default;
    return `<textarea class="select area mono" ${common} style="min-height:46px" spellcheck="false"
      placeholder="${type === "array" ? '[ "item1", "item2" ]' : '{ "key": "value" }'}">${esc(v === undefined || v === null ? "" : JSON.stringify(v, null, 1))}</textarea>`;
  }
  const v = val !== undefined ? val : prop?.default;
  return `<input class="select" ${common} value="${v === undefined || v === null ? "" : esc(String(v))}">`;
}

function argRowHtml(name, prop, val, required) {
  const req = Array.isArray(required) && required.includes(name);
  return `<div class="arg-row${req ? " arg-required" : ""}">
    <div class="arg-head">
      <code class="arg-key">${esc(name)}</code>
      <span class="chip">${esc(String(prop?.type || "string"))}</span>
      ${req ? `<span class="chip accent">required</span>` : ""}
      ${helpTip(prop?.description || `Argument “${name}” of type ${prop?.type || "string"}.`)}
    </div>
    ${argControlHtml(name, prop, val)}
  </div>`;
}

function argExtraRowHtml(name, val) {
  return `<div class="arg-row arg-extra">
    <div class="arg-head">
      <input class="select inp mono arg-extra-name" data-argname value="${esc(name)}" placeholder="extra argument name">
      ${helpTip("Not in this tool's schema — usually a runtime/execution option. Extra keys are still passed to the runner.")}
    </div>
    <textarea class="select area mono arg-extra-val" style="min-height:40px" spellcheck="false" placeholder="JSON value">${esc(JSON.stringify(val, null, 1))}</textarea>
  </div>`;
}

/* best-effort read of builder fields → args object; also used to seed raw JSON */
function collectBuilderArgs() {
  const args = {};
  let missing = [];
  $$("#argBuilder [data-arg]").forEach((el) => {
    const name = el.dataset.arg;
    const type = el.dataset.argtype || "string";
    const raw = el.value;
    if (type === "boolean") {
      if (raw === "") return;
      args[name] = raw === "true";
      return;
    }
    if (type === "integer") {
      if (String(raw).trim() === "") return;
      args[name] = parseInt(raw, 10);
      return;
    }
    if (type === "number") {
      if (String(raw).trim() === "") return;
      args[name] = parseFloat(raw);
      return;
    }
    if (type === "array" || type === "object") {
      const txt = String(raw).trim();
      if (!txt) return;
      try { args[name] = JSON.parse(txt); }
      catch { missing.push(`${name} must be valid ${type} JSON (e.g. ${type === "array" ? '["a","b"]' : '{"k":"v"}'})`); }
      return;
    }
    const txt = String(raw).trim();
    if (txt) args[name] = txt;
  });
  // extras (unknown keys)
  $$("#argBuilder .arg-extra").forEach((row) => {
    const name = (row.querySelector(".arg-extra-name")?.value || "").trim();
    const txt = (row.querySelector(".arg-extra-val")?.value || "").trim();
    if (!name && !txt) return;
    if (!name || !txt) { missing.push(`extra argument: fill both name and JSON value (or clear them)`); return; }
    try { args[name] = JSON.parse(txt); }
    catch { missing.push(`extra argument "${name}" must be a JSON value (true, 90, "text", …)`); }
  });
  return { args, missing };
}

function insInputsTab(s) {
  if (s.type === "shell") {
    const rows = Object.entries(s.inputs || {}).map(([k, v], i) => mapRowHtml(i, k, v, s)).join("");
    return `
      <div class="ins-note dim"><code>{{name}}</code> = run input · <code>{{step.field}}</code> = previous step output · bare text = literal. Use the source selector so you don't have to type references.</div>
      ${subLabel("Input parameters", "Values fed to this step. <b>Run input</b> asks at launch (wraps as <code>{{name}}</code>); <b>Previous step</b> pipes an upstream output field (<code>{{step.field}}</code>); <b>Literal value</b> hardcodes text, a number or a value like an IP or URL.", `<button class="btn icon" id="insAddInput" title="Add input parameter">＋</button>`)}
      <div id="insInputRows">${rows || `<p class="dim map-none">No inputs mapped yet — add one below.</p>`}</div>`;
  }
  const isTool = s.type === "tool";
  const schema = isTool ? toolInputSchema(s.tool) : null;
  if (isTool && schema) {
    const req = schema.required || [];
    const props = schema.properties;
    const argd = s.args || {};
    const rows = Object.entries(props).map(([k, p]) => argRowHtml(k, p, argd[k], req)).join("");
    const extras = Object.keys(argd).filter((k) => !props[k]).map((k) => argExtraRowHtml(k, argd[k])).join("");
    return `
      ${subLabel("Tool arguments", `Arguments for <b>${esc(s.tool || "tool")}</b>, straight from the registry's input schema — names and types are fixed, hover each <b>?</b> for what the argument does. Filled values become the JSON shown on the <i>Edit raw JSON</i> view.`)}
      <div id="argBuilder">${rows || `<p class="dim map-none">This tool takes no arguments.</p>`}${extras}</div>
      <button class="btn ghost sm" id="argAddExtra" type="button">＋ Extra argument</button>
      <button class="btn ghost sm" id="argRawToggle" type="button">Edit raw JSON</button>
      <textarea class="select area" id="insArgs" hidden style="min-height:150px;font-family:ui-monospace,Menlo,monospace" spellcheck="false">${esc(JSON.stringify(s.args || {}, null, 2))}</textarea>
      <div class="ins-note dim">Prefer a JSON blob? <i>Edit raw JSON</i> switches this same data to a raw view.</div>`;
  }
  return `
    ${subLabel(isTool ? "Tool arguments (JSON object)" : "Module arguments (JSON object)", isTool
      ? `No schema registered for <code>${esc(s.tool || "this tool")}</code> — write the arguments object by hand, e.g. <code>{"target":"10.0.0.5","verbose":true}</code>. Keys should match the tool's input schema in the Tool Registry.`
      : `Internal module stage arguments. The required keys depend on the module — the server reports a precise error if one is missing. Run inputs are referenced as <code>{{name}}</code>.`)}
    <textarea class="select area" id="insArgs" style="min-height:150px;font-family:ui-monospace,Menlo,monospace" spellcheck="false">${esc(JSON.stringify(s.args || {}, null, 2))}</textarea>`;
}

function insOutputsTab(s) {
  const res = stepResults[s.id];
  const hasRes = res && (res.outputs || res.error || res.stdout || res.stderr);
  if (s.type === "shell") {
    const f = s.filter && s.filter.type === "none" ? { type: "none" } : (s.filter || { type: "grep", pattern: "" });
    const fields = namedGroups(f.pattern);
    return `
      ${subLabel("Output extraction", "Cut structured values out of the command output. <b>grep</b> returns the first/last matching line, <b>regex</b> applies a pattern with named capture groups <code>(?P&lt;field&gt;…)</code> — each named group becomes an output field the next step can map as <code>{{thisstepid.field}}</code>.")}
      <div class="ins-filter-row">
        <select class="select" id="insFilterType">
          ${["grep", "regex", "none"].map((v) => `<option value="${v}" ${v === (s.filter?.type || "grep") ? "selected" : ""}>${v}</option>`).join("")}
        </select>
        <label class="checkline sm">first match only <input type="checkbox" id="insFilterFirst" ${s.filter?.first_only ? "checked" : ""}></label>
      </div>
      ${insLabel("Pattern", `<input class="select mono" id="insFilterPattern" value="${esc(f.pattern || "")}" placeholder='(?P<ip>[0-9]+\\.[0-9.]+)'>`,
        "Applies to the raw command output. Named groups become mapped fields — e.g. <code>(?P<host>[a-z0-9.-]+)</code> becomes <code>{{&lt;this step's id&gt;.host}}</code> for the next step.")}
      ${subLabel("Extracted fields", "Preview of what downstream steps can map — comes from the named <code>(?P&lt;group&gt;…)</code> groups above.")}
      ${fields.length ? `<div class="chip-row">${fields.map((x) => `<span class="chip accent mono">${esc(s.id)}.${esc(x)}</span>`).join("")}</div>` : `<p class="dim">No named fields — downstream steps will only see raw text unless you add (?P&lt;name&gt;…) groups.</p>`}
      ${hasRes ? `
        <div class="ins-subhead">Last run output</div>
        <pre class="result" style="max-height:200px;overflow:auto;margin:8px 0">${colorJson(res)}</pre>
      ` : ''}`;
  }
  if (hasRes) {
    return `
      <div class="ins-subhead">Last run output</div>
      <pre class="result" style="max-height:200px;overflow:auto;margin:8px 0">${colorJson(res)}</pre>
      <div class="ins-note">This step type produces its outputs internally — no manual output extraction to configure.</div>`;
  }
  return `<div class="ins-note">This step type produces its outputs internally (findings, correlations, scope checks or the assessment report) — no manual output extraction to configure.</div>`;
}

function insAdvancedTab(s) {
  return `
    ${subLabel("Execution arguments", "Extra JSON settings for the runner. Shell nodes can use <code>{name}</code> placeholders filled from the Inputs tab. Common keys: <code>timeout</code>, <code>verbose</code>.")}
    <textarea class="select area" id="insArgs" style="min-height:130px;font-family:ui-monospace,Menlo,monospace" spellcheck="false">${esc(JSON.stringify(s.args || {}, null, 2))}</textarea>
    ${insLabel("Condition (when)", `<input class="select mono" id="insWhen" value="${esc(s.when || "")}" placeholder="e.g. outputs? no match => skip">`,
      "Skips this step when the expression is falsy (0, false, empty). Leave blank to always run.")}`;
}

function insTabsHtml(s) {
  return `
    <div class="ins-head">
      <div class="ins-idline"><span class="mono">${esc(s.id)}</span><span class="chip">${esc(s.type || "step")}</span>
        <button class="btn ghost icon ins-close" id="insClose" title="Close inspector">×</button></div>
      <div class="ins-tabs" role="tablist">
        ${INS_TABS.map((t) => `<button class="${t === inspectTab ? "active" : ""}" data-tab="${t}" role="tab">${INS_TAB_LABELS[t]}</button>`).join("")}
      </div>
    </div>
    <div class="ins-body" id="insBody">${
      inspectTab === "general" ? insGeneralTab(s)
      : inspectTab === "inputs" ? insInputsTab(s)
      : inspectTab === "outputs" ? insOutputsTab(s)
      : insAdvancedTab(s)}</div>
    <div class="ins-foot">
      <span class="dim" id="insNote"></span>
      <div class="ins-foot-btns">
        <button class="btn danger" id="insDel">Delete step</button>
        <button class="btn" id="insDup">Duplicate step</button>
        <button class="btn primary" id="insApply">Apply</button>
      </div>
    </div>`;
}

function renderInspector() {
  const inj = $("#inspector");
  if (!inj) return;
  const s = graphSel ? stepById(graphSel) : null;
  if (!s) {
    inj.innerHTML = `
      <div class="ins-empty">
        <div class="eyebrow">INSPECTOR</div>
        <p class="empty">Select a step on the canvas to edit its configuration. Graph and DSL always edit the same canonical model.</p>
      </div>`;
    return;
  }
  /* Restore this node's last active inspector tab (per-node persistence). */
  const savedTab = inspTabByNode.get(graphSel);
  if (savedTab && INS_TABS.includes(savedTab)) inspectTab = savedTab;
  inj.innerHTML = insTabsHtml(s);

  $$("#inspector .ins-tabs button").forEach((btn) =>
    btn.addEventListener("click", () => {
      const s = stepById(graphSel);
      if (s) {
        applyTabFields(s);   // persist this tab's edits before switching
        inspTabByNode.set(graphSel, btn.dataset.tab);  // remember per node
      }
      inspectTab = btn.dataset.tab;
      renderInspector();
    }));

  $("#insApply")?.addEventListener("click", () => applyNodeInspector());
  $("#insDel")?.addEventListener("click", () => {
    if (!graphSel) return;
    if (!confirm(`Delete step “${graphSel}” and its edges?`)) return;
    deleteGraphStep(graphSel);
  });
  $("#insDup")?.addEventListener("click", () => { if (graphSel) duplicateStep(graphSel); });
  $("#insClose")?.addEventListener("click", () => $("#insCol")?.classList.remove("ins-open"));
  $("#insAddInput")?.addEventListener("click", () => addMapRow());

  // tool-argument schema builder wiring
  const bld = $("#argBuilder");
  if (bld) {
    $("#argRawToggle")?.addEventListener("click", () => {
      const ta = $("#insArgs");
      if (!ta) return;
      if (ta.hidden) {
        // seed raw view from the current builder fields
        const { args, missing } = collectBuilderArgs();
        if (!missing.length && Object.keys(args).length) {
          ta.value = JSON.stringify(args, null, 2);
        }
      }
      ta.hidden = !ta.hidden;
      bld.style.display = ta.hidden ? "" : "none";
      $("#argAddExtra") && ($("#argAddExtra").style.display = ta.hidden ? "" : "none");
      $("#argRawToggle").textContent = ta.hidden ? "Edit raw JSON" : "Use form fields";
      $("#argRawToggle").focus();
    });
    $("#argAddExtra")?.addEventListener("click", () => {
      const wrap = document.createElement("div");
      wrap.innerHTML = argExtraRowHtml("", "");
      const row = wrap.firstElementChild;
      bld.appendChild(row);
      row.querySelector(".arg-extra-name").focus();
    });
    // switching the tool re-opens the inspector on Inputs so the schema follows
    $("#insTool")?.addEventListener("change", () => {
      const s2 = stepById(graphSel);
      if (!s2) return;
      const t = $("#insTool").value.trim();
      if (t) s2.tool = t; else delete s2.tool;
      inspectTab = "inputs";
      renderInspector();
    });
  }

  // flip tooltips near the right edge / top of the scroll area so bubbles aren't clipped
  const bodyEl = $("#insBody");
  const bodyRect = bodyEl && typeof bodyEl.getBoundingClientRect === "function"
    ? bodyEl.getBoundingClientRect() : null;
  $$("#insBody .help-tip").forEach((b) => {
    const r = b.getBoundingClientRect();
    if (r.right + 270 > document.documentElement.clientWidth - 12) b.classList.add("tip-left");
    if (bodyRect && r.top - 130 < bodyRect.top) b.classList.add("tip-down");
  });

  // mapper wiring
  $$("#insBody .map-row").forEach((row) => {
    row.querySelectorAll(".map-mode").forEach((sel) => sel.addEventListener("change", () => {
      row.dataset.mode = sel.value;
    }));
    const stepSel = row.querySelector(".map-step");
    const fieldInp = row.querySelector(".map-field");
    if (stepSel && fieldInp) stepSel.addEventListener("change", () => {
      const dl = row.querySelector("datalist");
      if (dl) dl.innerHTML = upstreamOutFields(stepSel.value).map((x) => `<option value="${esc(x)}"></option>`).join("");
      fieldInp.value = "";
    });
    const whole = row.querySelector(".map-field-whole");
    if (whole && stepSel) {
      whole.addEventListener("change", () => {
        const m = whole.value.match(/^(\w+)\.(\w+)$/);
        if (m) { stepSel.value = m[1]; fieldInp.value = m[2]; }
      });
    }
    row.querySelector(".map-del")?.addEventListener("click", () => row.remove());
  });
}

function mapRowHtml(i, key, rawVal, s) {
  const ref = parseRef(rawVal);
  const upstreamSteps = getGraphSteps().filter((x) => x.id !== s.id);
  const preferred = ref.mode === "upstream" ? ref.step
    : ((s.depends_on || []).find((d) => stepById(d)) || upstreamSteps[0]?.id || "");
  const fields = upstreamOutFields(preferred);
  return `
    <div class="map-row" data-mode="${ref.mode}">
      <div class="map-k"><input class="select inp map-key" value="${esc(key)}" placeholder="parameter name"></div>
      <div class="map-src">
        <select class="select map-mode" title="Where is the value coming from?">
          <option value="run" ${ref.mode === "run" ? "selected" : ""}>Run input</option>
          <option value="upstream" ${ref.mode === "upstream" ? "selected" : ""}>Previous step</option>
          <option value="literal" ${ref.mode === "literal" ? "selected" : ""}>Literal value</option>
        </select>
      </div>
      <div class="map-arg">
        <div class="mf mf-run"><input class="select map-run" list="dlRunInputs" value="${ref.mode === "run" ? esc(ref.name) : ""}" placeholder="run input name"></div>
        <div class="mf mf-up">
          <select class="select map-step">
            <option value="">— step —</option>
            ${upstreamSteps.map((x) => `<option value="${esc(x.id)}" ${x.id === preferred ? "selected" : ""}>${esc(x.id)}</option>`).join("")}
          </select>
          <input class="select map-field" list="dlFields_${i}" value="${ref.mode === "upstream" ? esc(ref.field) : ""}" placeholder="output field">
          <datalist id="dlFields_${i}">${fields.map((x) => `<option value="${esc(x)}"></option>`).join("")}</datalist>
        </div>
        <div class="mf mf-lit"><input class="select map-lit" value="${ref.mode === "literal" ? esc(ref.val) : ""}" placeholder="literal value"></div>
      </div>
      <button class="btn danger icon map-del" title="Remove input">×</button>
    </div>`;
}

function addMapRow() {
  const rows = $("#insInputRows");
  if (!rows || !graphSel) return;
  const s = stepById(graphSel);
  const none = rows.querySelector(".map-none");
  if (none) none.remove();
  const wrap = document.createElement("div");
  wrap.innerHTML = mapRowHtml(999, "", "", s).replace(/999/g, `n${Date.now().toString(36)}`);
  const row = wrap.firstElementChild;
  rows.appendChild(row);
  row.querySelector(".map-mode").addEventListener("change", () => { row.dataset.mode = row.querySelector(".map-mode").value; });
  const stepSel = row.querySelector(".map-step");
  const fieldInp = row.querySelector(".map-field");
  if (stepSel) stepSel.addEventListener("change", () => {
    const dl = row.querySelector("datalist");
    if (dl) dl.innerHTML = upstreamOutFields(stepSel.value).map((x) => `<option value="${esc(x)}"></option>`).join("");
    fieldInp.value = "";
  });
  row.querySelector(".map-del").addEventListener("click", () => row.remove());
  row.querySelector(".map-key").focus();
}

/* apply: merges whichever fields are present in the current tab */
/* pull the currently-shown inspector fields into step object s from the DOM.
   Best-effort, no toasts, no canvas re-render — used to persist a tab's edits
   before switching tabs/pages so nothing the user typed is thrown away. */
function applyTabFields(s) {
  if (!s) return;
  const oldId = s.id;

  const idEl = $("#insId");
  if (idEl) {
    const newId = (idEl.value || "").trim();
    if (newId && newId !== oldId && !getGraphSteps().some((x) => x.id === newId)) {
      getGraphSteps().forEach((x) => {
        if (Array.isArray(x.depends_on)) x.depends_on = x.depends_on.map((d) => (d === oldId ? newId : d));
      });
      delete graphPos[oldId];
      s.id = newId;
    }
  }

  const depsEl = $("#insDeps");
  if (depsEl) {
    s.depends_on = depsEl.value.split(",").map((d) => d.trim()).filter(Boolean) || [];
  }

  const typeEl = $("#insType");
  if (typeEl && typeEl.value !== s.type) {
    s.type = typeEl.value;
    if (s.type !== "tool") delete s.tool;
  } else if (s.type === "shell" && !typeEl) {
    s.type = "shell";
  }

  const toolEl = $("#insTool");
  if (toolEl && s.type === "tool") {
    const t = toolEl.value.trim();
    if (t) s.tool = t; else delete s.tool;
  }

  const rowsEl = $("#insInputRows");
  if (rowsEl) {
    const inputs = {};
    $$("#insInputRows .map-row").forEach((row) => {
      const k = (row.querySelector(".map-key")?.value || "").trim();
      if (!k) return;
      const mode = row.querySelector(".map-mode")?.value || "literal";
      let v = "";
      if (mode === "run") v = `{{${(row.querySelector(".map-run")?.value || "").trim()}}}`;
      else if (mode === "upstream") {
        const st = (row.querySelector(".map-step")?.value || "").trim();
        const fl = (row.querySelector(".map-field")?.value || "").trim();
        if (!st || !fl) return; // keep previous value for this row
        v = `{{${st}.${fl}}}`;
      } else v = (row.querySelector(".map-lit")?.value || "").trim();
      if (v) inputs[k] = v;
    });
    if (Object.keys(inputs).length) s.inputs = inputs; else delete s.inputs;
  }

  const cmdEl = $("#insCommand");
  if (cmdEl) {
    const cmd = (cmdEl.value || "").trim();
    if (cmd) s.command = cmd; else delete s.command;
    s.type = "shell";
  }

  const ftypeEl = $("#insFilterType");
  if (ftypeEl) {
    const ftype = ftypeEl.value || "none";
    const fpat = ($("#insFilterPattern")?.value || "").trim();
    if (ftype === "none") {
      s.filter = { type: "none" };
    } else if (ftype && ftype !== "none" && fpat) {
      s.filter = { type: ftype, pattern: fpat };
      if ($("#insFilterFirst")?.checked) s.filter.first_only = true;
    } else delete s.filter;
  }

  const argsEl = $("#insArgs");
  if (argsEl) {
    const builder = $("#argBuilder");
    if (builder && argsEl.hidden) {
      const { args, missing } = collectBuilderArgs();
      if (!missing.length) {
        const sc = toolInputSchema(s.tool);
        const lack = sc ? (sc.required || []).filter((k) => args[k] === undefined || args[k] === "" || args[k] === null) : [];
        if (!lack.length) {
          if (Object.keys(args).length) s.args = args; else delete s.args;
        }
      }
    } else {
      try {
        const parsed = JSON.parse(argsEl.value || "{}");
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          if (Object.keys(parsed).length) s.args = parsed; else delete s.args;
        }
      } catch { /* keep previous args */ }
    }
  }

  const whenEl = $("#insWhen");
  if (whenEl) {
    const when = whenEl.value.trim();
    if (when) s.when = when; else delete s.when;
  }

  const ckEl = $("#insCheckpoint");
  if (ckEl && ckEl.checked) s.checkpoint = true; else if (ckEl) delete s.checkpoint;
}

function applyNodeInspector() {
  const s = stepById(graphSel);
  if (!s) return;
  const oldId = s.id;
  const ids = new Set(getGraphSteps().map((x) => x.id).filter((x) => x !== oldId));

  const idEl = $("#insId");
  if (idEl) {
    const newId = (idEl.value || "").trim();
    if (!newId) return toast("Step id required", true);
    if (newId !== oldId && ids.has(newId)) return toast(`Step id "${newId}" already used`, true);
  }

  applyTabFields(s);

  // required-argument / invalid-arg guards (only on explicit Save)
  const rowsEl = $("#insInputRows");
  if (rowsEl) {
    $$("#insInputRows .map-row").forEach((row) => {
      const k = (row.querySelector(".map-key")?.value || "").trim();
      if (!k) return;
      const mode = row.querySelector(".map-mode")?.value || "literal";
      if (mode === "upstream") {
        const st = (row.querySelector(".map-step")?.value || "").trim();
        const fl = (row.querySelector(".map-field")?.value || "").trim();
        if (!st || !fl) { toast(`${k}: pick a step and an output field`, true); }
      }
    });
  }
  const argsEl = $("#insArgs");
  if (argsEl) {
    const builder = $("#argBuilder");
    if (builder && argsEl.hidden) {
      const sc = toolInputSchema(s.tool);
      if (sc) {
        const argd = s.args || {};
        const lack = (sc.required || []).filter((k) => argd[k] === undefined || argd[k] === "" || argd[k] === null);
        if (lack.length) return toast(`Required argument${lack.length > 1 ? "s" : ""} ${lack.map((k) => `"${k}"`).join(", ")} missing for ${s.tool}`, true);
      }
    }
  }

  graphSel = s.id;
  captureGraphToDSL();
  renderBuilder();
  toast(`Step ${s.id} updated`);
}

/* compat wrappers (graph engine kept API-compatible for tooling) */
function applyShellInspector() { applyNodeInspector(); }
function applyInspector() { applyNodeInspector(); }

/* ── node mutations ────────────────────────────────────────────────── */

function deleteGraphStep(id) {
  pushHistory();
  graphDef.steps = getGraphSteps().filter((s) => s.id !== id);
  getGraphSteps().forEach((s) => {
    if (Array.isArray(s.depends_on)) s.depends_on = s.depends_on.filter((d) => d !== id);
  });
  graphSel = null;
  inspTabByNode.delete(id);
  captureGraphToDSL();
  renderBuilder();
  toast(`Step ${esc(id)} deleted`);
}

function duplicateStep(id) {
  const s = stepById(id);
  if (!s) return;
  let finalId = `${id}_copy`;
  let n = 1;
  while (stepById(finalId)) finalId = `${id}_copy_${n++}`;
  pushHistory();
  const copy = { ...s, id: finalId };
  getGraphSteps().push(copy);
  graphSel = finalId;
  captureGraphToDSL();
  renderBuilder();
  toast(`Duplicated ${esc(id)} → ${esc(finalId)}`);
}

function nextId(prefix) {
  let n = 1, id;
  do { id = `${prefix}_${n++}`; } while (stepById(id));
  return id;
}

function addGraphStep() {
  if (builderView !== "graph") { if (!loadGraphFromDSL()) return; }
  pushHistory();
  getGraphSteps().push({ id: nextId("step"), type: "tool", depends_on: [] });
  graphSel = getGraphSteps().slice(-1)[0].id;
  captureGraphToDSL();
  renderBuilder();
}

function addToolStep(toolName, forcedType) {
  if (builderView !== "graph") { if (!loadGraphFromDSL()) return; }
  pushHistory();
  const base = (toolName || "tool").replace(/[^a-zA-Z0-9_]/g, "_").slice(0, 24) || "tool";
  const s = { id: nextId(base), type: forcedType || "tool", depends_on: [] };
  if (toolName && (forcedType || "tool") === "tool") s.tool = toolName;
  getGraphSteps().push(s);
  graphSel = s.id;
  captureGraphToDSL();
  renderBuilder();
  inspectTab = "inputs";
  renderInspector();
  toast(`Added ${s.id}`);
}

function addShellStep() {
  if (builderView !== "graph") { if (!loadGraphFromDSL()) return; }
  pushHistory();
  const id = nextId("shell");
  getGraphSteps().push({
    id, type: "shell", command: "", inputs: {},
    filter: { type: "grep", pattern: "" }, depends_on: [],
  });
  graphSel = id;
  captureGraphToDSL();
  renderBuilder();
  inspectTab = "inputs";
  renderInspector();
  toast(`Added shell node ${id} — configure inputs, command and filter in the inspector`);
}

/* ── node dragging ────────────────────────────────────────────────── */

let dragCtx = null;
let canvasJustPanned = false;   // set true when an empty-canvas pan drag occurred
function startNodeDrag(e, id, origin) {
  if (e.button !== 0) return;
  /* Prevent text selection immediately on mousedown */
  e.preventDefault();
  const startX = e.clientX, startY = e.clientY;
  const ox = origin.x, oy = origin.y;
  let moved = false;
  const move = (ev) => {
    const dx = ev.clientX - startX, dy = ev.clientY - startY;
    if (!moved && Math.hypot(dx, dy) < 4) return;
    moved = true;
    const x = Math.round(ox + dx / graphZoom), y = Math.round(oy + dy / graphZoom);
    graphPos[id] = { x, y };
    const el = $(`.node[data-step="${CSS.escape(id)}"]`);
    if (el) { el.style.left = `${x}px`; el.style.top = `${y}px`; }
    redrawEdges();
  };
  const up = (ev) => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    document.body.style.userSelect = "";
    if (!moved) {
      const isPort = ev.target instanceof Element && ev.target.classList.contains("port");
      if (pendingConnect && isPort && pendingConnect !== id) { disarmConnect(); completeConnect(pendingConnect, id); return; }
      const sel = getGraphSteps().find((x) => x.id === id);
      graphSel = id;
      if (sel) inspectTab = "general";
      /* Update selection visually without full re-render (keeps DOM stable for copy clicks) */
      const canvasInner = $("#canvasInner");
      if (canvasInner) {
        $$(".node", canvasInner).forEach((n) => n.classList.remove("selected"));
        const nodeEl = $(`.node[data-step="${CSS.escape(id)}"]`);
        if (nodeEl) nodeEl.classList.add("selected");
      }
      renderInspector();
      syncToolbarButtons();
      if (window.matchMedia("(max-width: 1023px)").matches) $("#insCol")?.classList.add("ins-open");
    }
  };
  document.body.style.userSelect = "none";
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
}

/* ── edge creation ────────────────────────────────────────────────── */

let connectCtx = null;
function wouldCreateCycle(srcId, tgtId) {
  if (srcId === tgtId) return true;
  const seen = new Set();
  const stack = [srcId];
  while (stack.length) {
    const cur = stack.pop();
    if (cur === tgtId) return true;
    if (seen.has(cur)) continue;
    seen.add(cur);
    const s = stepById(cur);
    (s?.depends_on || []).forEach((d) => stack.push(d));
  }
  return false;
}

let pendingConnect = null;

function armConnect(srcId) {
  pendingConnect = srcId;
  $$(".port").forEach((p) => p.classList.remove("armed"));
  const el = $(`.node[data-step="${CSS.escape(srcId)}"] .port.right`);
  if (el) el.classList.add("armed");
  toast(`Connect: click the target node's ○ left port (Esc to cancel)`);
}

function disarmConnect() {
  pendingConnect = null;
  $$(".port").forEach((p) => p.classList.remove("armed", "compatible", "incompatible"));
}

function completeConnect(srcId, tgtId) {
  if (tgtId === srcId) { toast("A step cannot depend on itself", true); disarmConnect(); return; }
  if (wouldCreateCycle(srcId, tgtId)) { toast(`Wire ${srcId} → ${tgtId} would create a cycle`, true); disarmConnect(); return; }
  const s = stepById(tgtId);
  if (!s) { disarmConnect(); return; }
  s.depends_on = Array.isArray(s.depends_on) ? s.depends_on : [];
  if (s.depends_on.includes(srcId)) { toast(`${tgtId} already depends on ${srcId}`); disarmConnect(); return; }
  pushHistory();
  s.depends_on.push(srcId);
  disarmConnect();
  captureGraphToDSL();
  renderBuilder();
  toast(`Connected ${srcId} → ${tgtId}`);
}

function clearConnectPreview() {
  const p = $("#connectPreview");
  if (p) p.remove();
}

function startPortConnect(e, srcId) {
  e.preventDefault();
  e.stopPropagation();
  const startX = e.clientX, startY = e.clientY;
  const depths = {};
  graphLayout(depths);
  const a = nodePos(srcId, depths);
  const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
  connectCtx = { srcId, depths };
  /* Dim target ports that would create a cycle or belong to the source node;
   * glow the compatible input ports as "drop here" targets. */
  const compatible = new Set(), incompatible = new Set();
  for (const s of getGraphSteps()) {
    if (s.id === srcId || wouldCreateCycle(srcId, s.id)) incompatible.add(s.id);
    else compatible.add(s.id);
  }
  $$(".port").forEach((p) => {
    const node = p.closest(".node");
    if (!node || !node.dataset.step || !p.classList.contains("left")) return;
    if (compatible.has(node.dataset.step)) p.classList.add("compatible");
    else if (incompatible.has(node.dataset.step)) p.classList.add("incompatible");
  });
  const inner = $("#canvasInner");
  const toCanvas = (ev) => {
    const r = inner.getBoundingClientRect();
    return { x: (ev.clientX - r.left) / graphZoom, y: (ev.clientY - r.top) / graphZoom };
  };
  const move = (ev) => {
    const dx = ev.clientX - startX, dy = ev.clientY - startY;
    if (Math.hypot(dx, dy) < 4) return;
    disarmConnect();
    const m = toCanvas(ev);
    const mx = (x1 + m.x) / 2;
    clearConnectPreview();
    const svg = $("#edgeLayer");
    if (!svg) return;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.id = "connectPreview";
    path.setAttribute("class", "edge");
    path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${m.y} ${m.x},${m.y}`);
    svg.appendChild(path);
  };
  const up = (ev) => {
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
    clearConnectPreview();
    connectCtx = null;
    if (Math.hypot(ev.clientX - startX, ev.clientY - startY) < 4) { disarmConnect(); armConnect(srcId); return; }
    disarmConnect();
    const hit = document.elementFromPoint(ev.clientX, ev.clientY)?.closest(".node");
    if (!hit) return;
    const tgtId = hit.dataset.step;
    if (!tgtId) return;
    completeConnect(srcId, tgtId);
  };
  document.addEventListener("mousemove", move);
  document.addEventListener("mouseup", up);
}

/* Esc cancels an armed click-connect */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (pendingConnect) {
      disarmConnect();
      const col = $("#insCol");
      if (col) col.classList.remove("ins-open");
    }
    if (connectCtx) {
      clearConnectPreview();
      connectCtx = null;
      disarmConnect();
      toast("Connection cancelled");
    }
  }
  /* Delete / Backspace removes the selected node (when not typing in a field). */
  if ((e.key === "Delete" || e.key === "Backspace") && graphSel) {
    const active = document.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA"
      || active.tagName === "SELECT" || active.isContentEditable)) return;
    e.preventDefault();
    deleteGraphStep(graphSel);
  }
});

/* Clicking the ? help icon toggles its bubble; hover still works too. */
document.addEventListener("click", (e) => {
  const tip = e.target.closest(".help-tip");
  if (!tip) return;
  e.stopPropagation();
  tip.classList.toggle("open");
});

/* Double-click an edge to remove the dependency (delegated, survives innerHTML swaps). */
document.addEventListener("dblclick", (e) => {
  const path = e.target.closest(".edge");
  if (!path) return;
  e.stopPropagation();
  const dep = path.dataset.dep, step = path.dataset.step;
  if (!dep || !step) return;
  pushHistory();
  const s = stepById(step);
  if (s && Array.isArray(s.depends_on)) {
    s.depends_on = s.depends_on.filter((d) => d !== dep);
    captureGraphToDSL();
    renderBuilder();
    toast(`Removed ${dep} → ${step}`);
  }
});

/* Click a copyable key label to copy its value to clipboard. */
document.addEventListener("click", (e) => {
  const span = e.target.closest(".copyable");
  if (!span) return;
  e.preventDefault();
  e.stopPropagation();
  copyText(span.dataset.copyValue || "");
});

function redrawEdges() {
  const depths = {};
  graphLayout(depths);
  const svg = $("#edgeLayer");
  if (!svg) return;
  let edges = "";
  for (const s of getGraphSteps()) {
    for (const dep of s.depends_on || []) {
      if (!depths.hasOwnProperty(dep) || dep === s.id) continue;
      const a = nodePos(dep, depths), b = nodePos(s.id, depths);
      const y1 = a.y + NODE_H / 2, y2 = b.y + NODE_H / 2;
      const hot = graphSel === dep || graphSel === s.id ? " hot" : "";
      edges += `<path class="edge${hot}" d="M${a.x + NODE_W},${y1} C${(a.x + NODE_W + b.x) / 2},${y1} ${(a.x + NODE_W + b.x) / 2},${y2} ${b.x},${y2}" data-dep="${esc(dep)}" data-step="${esc(s.id)}" stroke-width="4" vector-effect="non-scaling-stroke"/>`;
    }
  }
  svg.innerHTML = edges;
}

function addStage(type) {
  if (builderView !== "graph") { if (!loadGraphFromDSL()) return; }
  pushHistory();
  const id = nextId(type === "scope_check" ? "scope" : type === "finding_correlation" ? "corr" : "report");
  getGraphSteps().push({ id, type, depends_on: [] });
  graphSel = id;
  captureGraphToDSL();
  renderBuilder();
  toast(`Added ${id} (${type})`);
}

/* ── run: input modal built from the playbook's input specs ────────── */

/* Read the last-run record for a playbook from localStorage and render a
 * small status chip (status + timestamp) for the run modal header. */
function runLastChipHtml(id) {
  const raw = localStorage.getItem(`kmcp_run_${id}`);
  if (!raw) return "";
  let r;
  try { r = JSON.parse(raw); } catch { return ""; }
  const st = String(r.status || "—");
  const cls = st === "completed" ? "ok" : (st === "failed" || st === "cancelled" ? "failed" : "run-run");
  const at = r.completedAt || r.startedAt;
  return `<span class="chip ${cls}">Last run: ${esc(st)}${at ? ` · ${esc(fmtTs(at))}` : ""}</span>`;
}

async function runModal(id) {
  let def = null;
  try { def = (await api(`/playbooks/${encodeURIComponent(id)}/definition`)).definition; } catch { /* not saved yet */ }
  // Always prefer the live editor graph (graphDef) — it holds the user's
  // unsaved step inputs / {{name}} references. Only fall back to the server
  // definition when the editor graph hasn't been loaded yet.
  if (graphDef) {
    applyTabFields(stepById(graphSel));
    def = graphDef;
  } else if (!def) {
    // server returned null (unsaved draft, server not yet restarted); nothing to show
    def = null;
  }
  const declared = defInputs(def);
  const stepDerived = collectRunInputs(def);
  const byName = new Map();
  for (const sp of [...stepDerived, ...declared]) byName.set(sp.name, sp); // declared overrides step-derived
  const specs = [...byName.values()];
  const defaults = new Map(specs.map((sp) => [sp.name, sp.spec?.default]));
  const rows = specs.map((sp) => {
    const type = String(sp.spec?.type || "string");
    const required = sp.spec?.required ? "required" : "optional";
    let field;
    if (type === "boolean") {
      field = `<select class="select run-in" data-name="${esc(sp.name)}"><option value="true">true</option><option value="false">false</option></select>`;
    } else if (type === "integer" || type === "number") {
      field = `<input class="select run-in" data-name="${esc(sp.name)}" type="number" value="${esc(String(sp.spec?.default ?? ""))}">`;
    } else if (type === "array" || type === "object") {
      field = `<textarea class="select area run-in" data-name="${esc(sp.name)}" spellcheck="false" style="min-height:56px;font-family:ui-monospace,Menlo,monospace" placeholder="JSON ${type}">${esc(JSON.stringify(sp.spec?.default ?? (type === "array" ? [] : {})))}</textarea>`;
    } else {
      field = `<input class="select run-in" data-name="${esc(sp.name)}" value="${esc(String(sp.spec?.default ?? ""))}">`;
    }
    return `
      <div class="run-row">
        <div class="run-row-head"><b class="mono">${esc(sp.name)}</b>
          <span class="chip ${sp.spec?.required ? "ck" : ""}">${esc(type)} · ${esc(required)}</span></div>
        ${field}
      </div>`;
  }).join("");

  /* placeholder reference — every {{...}} referenced anywhere in the playbook */
  const allRefs = collectRunInputs(def);
  const refList = allRefs.map((sp) => {
    const deflt = sp.spec?.default;
    const hint = deflt === undefined ? "" : ` (default: ${esc(JSON.stringify(deflt))})`;
    return `<span class="chip mono">${esc(sp.name)}${hint}</span>`;
  }).join(" ");
  const refHtml = allRefs.length
    ? `<div class="run-row">
       <div class="run-row-head"><b>Placeholders used in this playbook</b><span class="chip">{{name}} · optional unless marked required</span></div>
       <p class="dim ins-note" style="margin:4px 0 0">These {{name}} references appear in step inputs, args, or commands. Provide them above or as extra JSON.</p>
       <div style="display:flex;flex-wrap:wrap;gap:6px">${refList}</div>
     </div>`
    : "";

  const bd = document.createElement("div");
  bd.className = "modal-backdrop show";
  bd.innerHTML = `
    <div class="modal run-modal">
      <h3>Run “${esc(id)}”</h3>
      ${runLastChipHtml(id)}
      <p class="dim">Runs the published version. This playbook takes ${specs.length} input${specs.length === 1 ? "" : "s"} — anything omitted falls back to its default.</p>
      ${rows || `<p class="dim">No declared inputs — start a run directly, or add extra ones as JSON below.</p>`}
      ${refHtml}
      <div class="run-row">
        <div class="run-row-head"><b>Extra inputs</b><span class="chip">JSON · optional</span></div>
        <textarea class="select area" id="runExtra" spellcheck="false" style="min-height:64px;font-family:ui-monospace,Menlo,monospace">{}</textarea>
      </div>
      <div class="run-row">
        <label class="checkline" style="margin:0">
          <input type="checkbox" id="runDebug" checked />
          Debug run (marks this execution as a manual debug run)
        </label>
      </div>
      <div class="modal-actions">
        <button class="btn" id="runCancel">Cancel</button>
        <button class="btn ghost" id="runReset">Reset to defaults</button>
        <button class="btn primary" id="runStart">▶ Start run</button>
      </div>
    </div>`;
  document.body.appendChild(bd);
  bd.querySelector("#runCancel").addEventListener("click", () => bd.remove());
  bd.querySelector("#runReset").addEventListener("click", () => {
    bd.querySelectorAll(".run-in").forEach((f) => {
      const name = f.dataset.name;
      const deflt = defaults.get(name);
      if (f.tagName === "TEXTAREA") {
        f.value = deflt === undefined || deflt === null ? ""
          : (typeof deflt === "string" ? deflt : JSON.stringify(deflt));
      } else {
        f.value = deflt === undefined || deflt === null ? "" : String(deflt);
      }
    });
    bd.querySelector("#runExtra").value = "{}";
    toast("Inputs reset to defaults");
  });
  bd.addEventListener("mousedown", (e) => { if (e.target === bd) bd.remove(); });
  bd.querySelector("#runStart").addEventListener("click", async () => {
    const inputs = {};
    let ok = true;
    bd.querySelectorAll(".run-in").forEach((f) => {
      const k = f.dataset.name;
      if (f.tagName === "TEXTAREA") {
        const raw = (f.value || "").trim();
        if (!raw) return;
        try { inputs[k] = JSON.parse(raw); }
        catch { toast(`Input ${k} must be valid JSON`, true); ok = false; }
        return;
      }
      if (f.value === "") return;
      inputs[k] = f.getAttribute("type") === "number" ? Number(f.value) : f.value;
    });
    let extra = {};
    const rawExtra = (bd.querySelector("#runExtra").value || "").trim();
    if (rawExtra) {
      try { extra = JSON.parse(rawExtra); }
      catch { toast("Extra inputs must be valid JSON", true); ok = false; }
    }
    if (!ok) return;
    Object.assign(inputs, extra || {});
    const debug = bd.querySelector("#runDebug")?.checked === true;
    const startBtn = bd.querySelector("#runStart");
    busyBtn(startBtn, true);
    bd.remove();
    try {
      await startRun(id, inputs, { debug, trigger: "studio" });
    } finally {
      busyBtn(startBtn, false);
    }
  });
}

async function startRun(id, inputs, opts = {}) {
  // Stop any previous live-poll timer
  if (runStatusTimer) { clearInterval(runStatusTimer); runStatusTimer = null; }
  // A new run clears the reset marker so fresh results persist on refresh
  localStorage.removeItem('kmcp_reset_' + id);
  runStatus = {};
  toast(`Starting ${id}${opts.debug ? " (debug)" : ""}…`);
  try {
    const r = await api("/executions", { method: "POST", body: JSON.stringify({ playbook_id: id, inputs, debug: !!opts.debug, trigger: opts.trigger || "studio" }) });
    const execId = r?.execution?.execution_id || r?.execution_id || "";
    if (!execId) { toast("Run started but no execution id"); return; }
    toast(`Run started: ${execId}`);
    localStorage.setItem(`kmcp_run_${id}`, JSON.stringify({ status: "running", startedAt: new Date().toISOString(), playbook_id: id }));
    currentExecId = execId;
    $("#pbCancelRun")?.hidden ? $("#pbCancelRun").hidden = false : null;

    // Poll execution status live so nodes highlight yellow while running
    runStatusTimer = setInterval(async () => {
      try {
        const det = await api(`/executions/${encodeURIComponent(execId)}`);
        const exec = det.execution;
        if (!exec) return;

        const map = {};
        const results = {};
        for (const s of exec.steps || []) {
          map[s.step_id] = s.status;
          results[s.step_id] = {
            outputs: s.outputs,
            error: s.error,
            tool_id: s.tool_id,
          };
        }
        runStatus = map;
        stepResults = results;

        if (builderView === "graph" && $("#canvasInner")) renderBuilder();

        const st = exec.status;
        if (st !== "running" && st !== "pending") {
          clearInterval(runStatusTimer);
          runStatusTimer = null;
          currentExecId = null;
          localStorage.setItem(`kmcp_run_${id}`, JSON.stringify({ status: st, completedAt: new Date().toISOString(), playbook_id: id }));
          $("#pbCancelRun")?.hidden ? null : ($("#pbCancelRun").hidden = true);
          renderBuilder();
          toast(st === "completed" ? "Run completed" : `Run ${st}`);
        }
      } catch { /* keep polling */ }
    }, 1000);

    // Initial status load to catch already-completed steps
    await loadRunStatus(id);
    if (builderView === "graph" && $("#canvasInner")) renderBuilder();
  } catch (err) { toast(err.message, true); }
}

async function cancelCurrentRun() {
  if (!currentExecId) return toast("No run in progress", true);
  if (!confirm(`Cancel run ${currentExecId}?`)) return;
  const b = $("#pbCancelRun");
  busyBtn(b, true);
  try {
    await api(`/executions/${encodeURIComponent(currentExecId)}/cancel`, { method: "POST" });
    toast("Cancelling run…");
    currentExecId = null;
    $("#pbCancelRun")?.hidden ? null : ($("#pbCancelRun").hidden = true);
  } catch (err) {
    toast(err.message, true);
  }
  busyBtn(b, false);
}

/* surface the latest execution's per-step status on the graph */
async function loadRunStatus(id) {
  try {
    const d = await api("/executions?limit=25");
    const mine = (d.executions || []).filter((e) => e.playbook_id === id);
    if (!mine.length) return;
    const det = await api(`/executions/${encodeURIComponent(mine[0].execution_id)}`);
    const exec = det.execution;
    const map = {};
    const results = {};
    for (const s of exec.steps || []) {
      map[s.step_id] = s.status;
      results[s.step_id] = {
        outputs: s.outputs,
        error: s.error,
        tool_id: s.tool_id,
      };
    }
    runStatus = map;
    stepResults = results;
    if (builderView === "graph" && $("#canvasInner")) renderBuilder();
  } catch { /* best-effort only */ }
}

/* ── playbook-level actions ────────────────────────────────────────── */

async function validateDefinition() {
  const out = $("#editorResult");
  if (!selectedId) return toast("Select a playbook first", true);
  if (builderView === "graph") { applyTabFields(stepById(graphSel)); captureGraphToDSL(); }
  let def;
  try {
    def = JSON.parse(rawDsl);
  } catch (err) {
    out.hidden = false;
    out.textContent = `Invalid JSON: ${err.message}`;
    toast("Validation failed — invalid JSON", true);
    return;
  }
  try {
    const res = await fetch(`${API}/playbooks/${encodeURIComponent(selectedId)}/validate-definition`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(token() ? { Authorization: `Bearer ${token()}` } : {}) },
      body: JSON.stringify({ definition: def }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      out.hidden = false;
      const details = body?.details ?? [];
      const lines = details.map((f) => {
        const loc = [
          f.step_id ? `step "${f.step_id}"` : "",
          f.field ? `field "${f.field}"` : "",
        ].filter(Boolean).join(" ");
        return `${f.severity?.toUpperCase() ?? "ERROR"}: ${loc} ${f.message ?? ""}`.trim();
      });
      out.textContent = `Validation failed (${details.length} finding${details.length !== 1 ? "s" : ""}):\n\n${lines.join("\n") || JSON.stringify(body, null, 2)}`;
      toast("Validation failed — see details below", true);
      return;
    }
    out.hidden = false;
    out.textContent = JSON.stringify(body, null, 2);
    toast(body.valid ? "Definition valid" : "Validation failed", !body.valid);
  } catch (err) {
    out.hidden = false;
    out.textContent = `Validation error: ${err.message}`;
    toast("Validation failed — see details below", true);
  }
}

async function saveDraft() {
  if (!selectedId) return toast("Select a playbook first", true);
  if (builderView === "graph") { applyTabFields(stepById(graphSel)); captureGraphToDSL(); }
  try {
    const body = { definition: JSON.parse(rawDsl) };
    await api(`/playbooks/${encodeURIComponent(selectedId)}/definition`, {
      method: "PUT", body: JSON.stringify(body),
    });
    toast("Draft saved");
    localStorage.removeItem(`kmcp_dsl_${selectedId}`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function publish() {
  if (!selectedId) return toast("Select a playbook first", true);
  if (builderView === "graph") { applyTabFields(stepById(graphSel)); captureGraphToDSL(); }
  try {
    const def = JSON.parse(rawDsl);
    const version = def.version || "1.0.0";
    await api(`/playbooks/${encodeURIComponent(selectedId)}/publish`, {
      method: "POST", body: JSON.stringify({ definition: def, version }),
    });
    toast(`Published ${version}`);
    localStorage.removeItem(`kmcp_dsl_${selectedId}`);
    await refreshPlaybooks();
  } catch (err) {
    if (/approval/i.test(err.message || "")) {
      /* An approval request is only a request — an operator must approve it
         through /api/approvals/{id}/approve before publish is allowed. */
      if (confirm(`Publishing requires operator approval for ${selectedId}.\nSend an approval request?`)) {
        try {
          await api(`/playbooks/${encodeURIComponent(selectedId)}/approval-request`, { method: "POST" });
          toast("Approval request sent — an operator must approve it before publishing");
        } catch (e2) { toast(e2.message, true); }
      }
      return;
    }
    toast(err.message, true);
  }
}

/* ── create: structured modal (name → auto id → category → description) */

async function newPlaybook() {
  const existing = new Set(playbooksCache.map((p) => p.playbook_id));
  const bd = document.createElement("div");
  bd.className = "modal-backdrop show";
  bd.innerHTML = `
    <div class="modal new-pb-modal" role="dialog" aria-modal="true" aria-labelledby="npTitle">
      <div class="np-head">
        <div class="np-glyph">✦</div>
        <div>
          <h3 id="npTitle">New playbook</h3>
          <p class="dim">Name it here — the workflow canvas opens right after, so you can start wiring steps immediately.</p>
        </div>
      </div>
      <label class="np-field">
        <span class="np-label">Name <em>shown in the library and editor header</em></span>
        <input class="select" id="npName" placeholder="e.g. External recon sweep" autocomplete="off" spellcheck="false">
      </label>
      <label class="np-field">
        <span class="np-label">Playbook ID <em>stable identifier used in runs and URLs — auto-filled from the name</em></span>
        <input class="select mono" id="npId" placeholder="external_recon_sweep" autocomplete="off" spellcheck="false">
        <span class="np-hint" id="npHint"></span>
      </label>
      <div class="np-row2">
        <label class="np-field">
          <span class="np-label">Category</span>
          <select class="select" id="npCat">
            <option value="custom">custom</option>
            <option value="recon">recon</option>
            <option value="assessment">assessment</option>
            <option value="reporting">reporting</option>
            <option value="response">response</option>
          </select>
        </label>
        <label class="np-field np-col">
          <span class="np-label">Description <em>optional</em></span>
          <input class="select" id="npDesc" placeholder="What does this workflow do?">
        </label>
      </div>
      <div class="modal-actions">
        <button class="btn" id="npCancel">Cancel</button>
        <button class="btn primary" id="npCreate" disabled>Create playbook</button>
      </div>
    </div>`;
  document.body.appendChild(bd);

  const nameEl = bd.querySelector("#npName"), idEl = bd.querySelector("#npId"),
    hintEl = bd.querySelector("#npHint"), catEl = bd.querySelector("#npCat"),
    descEl = bd.querySelector("#npDesc"), createBtn = bd.querySelector("#npCreate");
  let idDirty = false;

  function slugify(v) {
    const s = String(v).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48);
    return s || (v ? `pb_${Date.now().toString(36)}` : "");
  }
  function validate() {
    let err = "";
    const id = idEl.value;
    if (id && existing.has(id)) err = `A playbook with ID “${id}” already exists`;
    else if (/[^\w-]/.test(id)) err = "Letters, numbers, dashes and underscores only";
    hintEl.textContent = err;
    hintEl.classList.toggle("warn", !!err);
    if (err) { createBtn.disabled = true; return false; }
    createBtn.disabled = !(nameEl.value.trim() && id);
    return true;
  }

  idEl.addEventListener("input", () => { idDirty = true; validate(); });
  nameEl.addEventListener("input", () => { if (!idDirty) idEl.value = slugify(nameEl.value); validate(); });
  descEl.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  [nameEl, idEl].forEach((el) => el.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); }));
  document.addEventListener("keydown", escClose);

  function escClose(e) { if (e.key === "Escape") close(); }
  function close() {
    document.removeEventListener("keydown", escClose);
    bd.remove();
  }
  async function submit() {
    if (!validate() || createBtn.disabled) return;
    const name = nameEl.value.trim().slice(0, 120);
    const pid = idEl.value.trim();
    if (existing.has(pid)) { nameEl.reportValidity?.(); return; }
    const payload = { playbook_id: pid, name, category: catEl.value };
    if (descEl.value.trim()) payload.description = descEl.value.trim();
    createBtn.disabled = true;
    createBtn.textContent = "Creating…";
    try {
      await api("/playbooks/create", { method: "POST", body: JSON.stringify(payload) });
    } catch (err) {
      createBtn.disabled = false;
      createBtn.textContent = "Create playbook";
      return toast(err.message, true);
    }
    close();
    toast(`“${name}” created — opening the editor`);
    location.hash = `#/playbooks/${encodeURIComponent(pid)}`;
  }

  bd.querySelector("#npCancel").addEventListener("click", close);
  createBtn.addEventListener("click", submit);
  bd.addEventListener("mousedown", (e) => { if (e.target === bd) close(); });
  nameEl.focus();
}

async function deletePlaybookById(id) {
  if (!confirm(`Delete playbook ${id}? This cannot be undone.`)) return;
  try {
    await api(`/playbooks/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("Playbook deleted");
    await refreshPlaybooks();
  } catch (err) {
    toast(err.message, true);
  }
}

async function deletePlaybook() {
  if (!selectedId) return toast("Select a playbook first", true);
  if (!confirm(`Delete playbook ${selectedId}? This cannot be undone.`)) return;
  try {
    await api(`/playbooks/${encodeURIComponent(selectedId)}`, { method: "DELETE" });
    toast("Playbook deleted");
    location.hash = "#/playbooks";
  } catch (err) { toast(err.message, true); }
}

async function showVersions() {
  if (!selectedId) return toast("Select a playbook first", true);
  try {
    const d = await api(`/playbooks/${encodeURIComponent(selectedId)}/versions`);
    const list = d.versions || [];
    if (!list.length) return toast("No versions published yet");
    const v = await formModal({
      title: `Versions of “${selectedId}”`,
      subtitle: "Pick a version to move the published pointer back to.",
      fields: [{
        name: "version_id",
        label: "Roll back to version",
        type: "select",
        options: list.map((x) => ({ value: String(x.version_id), label: `#${x.version_id} · v${x.version}` })),
      }],
      submitLabel: "Roll back",
    });
    if (!v) return;
    await api(`/playbooks/${encodeURIComponent(selectedId)}/rollback`, {
      method: "POST", body: JSON.stringify({ version_id: v.version_id }),
    });
    toast("Rolled back");
    await pagePlaybookEditor(selectedId);
  } catch (err) { toast(err.message, true); }
}

async function exportPlaybook() {
  if (!selectedId) return toast("Select a playbook first", true);
  await exportPlaybookById(selectedId);
}

async function exportPlaybookById(pid) {
  try {
    const pkg = await api(`/playbooks/${encodeURIComponent(pid)}/export`);
    const blob = new Blob([JSON.stringify(pkg, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${pid}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("Export downloaded");
  } catch (err) { toast(err.message, true); }
}

/* Publish a draft playbook from the library. Publish is gated on operator
   approval: on a 403, prompt to create an approval request, then retry. */
async function publishPlaybookById(pid) {
  let def;
  try {
    const d = await api(`/playbooks/${encodeURIComponent(pid)}/definition`);
    def = d.definition;
  } catch (err) {
    return toast(err.message, true);
  }
  if (!def) return toast("No draft definition to publish", true);
  const version = (def && def.version) || "1.0.0";
  try {
    await api(`/playbooks/${encodeURIComponent(pid)}/publish`, {
      method: "POST", body: JSON.stringify({ definition: def, version }),
    });
    toast("Published");
    await refreshPlaybooks();
  } catch (err) {
    if (/approval/i.test(err.message || "")) {
      /* Never self-approve: request only, then wait for an operator decision. */
      if (confirm("Publishing requires operator approval. Send an approval request?")) {
        try {
          await api(`/playbooks/${encodeURIComponent(pid)}/approval-request`, { method: "POST" });
          toast("Approval request sent — an operator must approve it before publishing");
        } catch (e2) { toast(e2.message, true); }
      }
      return;
    }
    toast(err.message, true);
  }
}

/* Open a modal listing a playbook's published versions, each with a rollback
   action (POST /api/playbooks/{id}/rollback). */
async function viewPlaybookVersions(pid) {
  try {
    const d = await api(`/playbooks/${encodeURIComponent(pid)}/versions`);
    const list = d.versions || [];
    if (!list.length) return toast("No versions published yet");
    const bd = document.createElement("div");
    bd.className = "modal-backdrop show";
    bd.innerHTML = `
      <div class="modal new-pb-modal" role="dialog" aria-modal="true">
        <h3>Versions of “${esc(pid)}”</h3>
        <div style="max-height:340px;overflow:auto;margin:10px 0;border:1px solid var(--line2);border-radius:9px;background:var(--panel2)">
          <table style="width:100%;border-collapse:collapse;font-size:12.5px">
            <thead><tr>
              <th style="text-align:left;padding:7px 10px">Version</th>
              <th style="text-align:left;padding:7px 10px">Published</th>
              <th style="text-align:right;padding:7px 10px">Action</th>
            </tr></thead>
            <tbody>
              ${list.map((v) => `
                <tr>
                  <td style="padding:7px 10px;border-bottom:1px solid var(--line2)">
                    <span class="mono">v ${esc(v.version)}</span><br/>
                    <span class="dim" style="font-size:11px">${esc(String(v.version_id || ""))}</span>
                  </td>
                  <td style="padding:7px 10px;border-bottom:1px solid var(--line2)">${esc(fmtTs(v.published_at))}</td>
                  <td style="padding:7px 10px;border-bottom:1px solid var(--line2);text-align:right">
                    <button class="btn ghost" data-rid="${esc(String(v.version_id))}" data-rver="${esc(v.version)}">Roll back</button>
                  </td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>
        <div class="modal-actions">
          <button class="btn" id="verClose">Close</button>
        </div>
      </div>`;
    document.body.appendChild(bd);
    const close = () => bd.remove();
    bd.querySelector("#verClose").addEventListener("click", close);
    bd.addEventListener("mousedown", (e) => { if (e.target === bd) close(); });
    bd.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-rid]");
      if (!b) return;
      const rid = b.dataset.rid;
      if (!confirm(`Roll back “${pid}” to version ${b.dataset.rver}?`)) return;
      try {
        await api(`/playbooks/${encodeURIComponent(pid)}/rollback`, {
          method: "POST", body: JSON.stringify({ version_id: rid }),
        });
        toast("Rolled back");
        close();
        await refreshPlaybooks();
      } catch (err) { toast(err.message, true); }
    });
  } catch (err) { toast(err.message, true); }
}

async function importPlaybook() {
  const bd = document.createElement("div");
  bd.className = "modal-backdrop show";
  bd.innerHTML = `
    <div class="modal new-pb-modal" role="dialog" aria-modal="true">
      <h3>Import playbook</h3>
      <p class="dim">Paste a playbook package JSON, or upload a .json file exported from Playbook Studio.</p>
      <label style="display:block;margin:10px 0 0">
        <span class="np-label" style="display:block;font-size:12px;margin-bottom:4px">Paste JSON</span>
        <textarea class="select area" id="impJson" placeholder='{"format":"kmcp-playbook-package","playbook":{...},"versions":[...]}' spellcheck="false" style="min-height:140px;font-family:ui-monospace,Menlo,monospace"></textarea>
      </label>
      <label style="display:block;margin:10px 0 0">
        <span class="np-label" style="display:block;font-size:12px;margin-bottom:4px">Or upload file</span>
        <input type="file" id="impFile" accept=".json" />
      </label>
      <div class="modal-actions">
        <button class="btn" id="impCancel">Cancel</button>
        <button class="btn primary" id="impSubmit">Import</button>
      </div>
    </div>`;
  document.body.appendChild(bd);
  let pkg = null;
  bd.querySelector("#impFile").addEventListener("change", async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      pkg = JSON.parse(await file.text());
      bd.querySelector("#impJson").value = JSON.stringify(pkg, null, 2);
    } catch { toast("Invalid JSON file", true); }
  });
  bd.querySelector("#impCancel").addEventListener("click", () => bd.remove());
  bd.addEventListener("mousedown", (e) => { if (e.target === bd) bd.remove(); });
  bd.querySelector("#impSubmit").addEventListener("click", async () => {
    if (!pkg) {
      const raw = (bd.querySelector("#impJson").value || "").trim();
      if (!raw) return toast("Paste JSON or upload a file first", true);
      try { pkg = JSON.parse(raw); } catch { return toast("Invalid JSON", true); }
    }
    const btn = bd.querySelector("#impSubmit");
    busyBtn(btn, true);
    try {
      await api("/playbooks", { method: "POST", body: JSON.stringify({ package: pkg }) });
      toast("Imported");
      bd.remove();
      await refreshPlaybooks();
    } catch (err) { toast(err.message, true); busyBtn(btn, false); }
  });
}

async function seedPlaybooks() {
  try {
    const r = await api("/playbooks/seed", { method: "POST" });
    const count = r?.seeded ?? 0;
    toast(`Seeded ${count} playbook${count !== 1 ? "s" : ""}`);
    await refreshPlaybooks();
  } catch (err) {
    toast(err.message, true);
  }
}

/* ── route: executions ─────────────────────────────────────────────── */

let execTimer = null;
let execPage = { limit: 25, offset: 0 };

async function refreshExecutions(status) {
  const el = $("#execTable");
  const pg = $("#execPagination");
  if (!el) return;
  const limit = execPage.limit;
  const offset = execPage.offset;
  const q = status && status !== "all" ? `&status=${encodeURIComponent(status)}` : "";
  el.innerHTML = `<div class="panel table-panel"><p class="empty">Loading executions…</p></div>`;
  if (pg) pg.innerHTML = "";
  try {
    /* Request limit+1 so we can tell whether a next page exists without a
       count field from the backend. */
    const d = await api(`/executions?limit=${limit + 1}&offset=${offset}${q}`);
    const list = d.executions || [];
    const hasNext = list.length > limit;
    const rows = list.slice(0, limit);
    const totalShown = rows.length;
    const from = offset + 1;
    const to = offset + totalShown;
    if (!rows.length) {
      el.innerHTML = emptyStatePanel("No executions", "There are no workflow runs yet. Run a playbook to create one.", "Retry");
      el.querySelector("[data-retry]")?.addEventListener("click", () => refreshExecutions($("#execFilter")?.value || "all"));
      if (pg) pg.innerHTML = "";
      return;
    }
    el.innerHTML = table(["Execution", "Playbook", "Trigger", "Status", "Completed"],
      rows.map((e) => ({
        cells: [
          `<a href="#/executions/${encodeURIComponent(e.execution_id)}">${esc(e.execution_id)}</a>`,
          esc(e.playbook_id || "—"),
          `${esc(e.trigger || "—")}${Number(e.debug) ? " <span class=\"chip ck\">debug</span>" : ""}`,
          BADGE(e.status),
          fmtTs(e.completed_at),
        ],
        menu: `<a href="#/executions/${encodeURIComponent(e.execution_id)}">detail</a>`,
      })));
    if (pg) {
      pg.innerHTML = paginationHtml({ page: Math.floor(offset / limit) + 1, pageSize: limit, total: null, hasPrev: offset > 0, hasNext, from, to });
      wirePagination(pg, {
        onPrev: () => { execPage.offset = Math.max(0, execPage.offset - execPage.limit); refreshExecutions($("#execFilter")?.value || "all"); },
        onNext: () => { execPage.offset += execPage.limit; refreshExecutions($("#execFilter")?.value || "all"); },
        onSize: (size) => { execPage.limit = +size; execPage.offset = 0; refreshExecutions($("#execFilter")?.value || "all"); },
      });
    }
  } catch (err) {
    el.innerHTML = emptyStatePanel("Failed to load executions", err.message, "Retry");
    el.querySelector("[data-retry]")?.addEventListener("click", () => refreshExecutions($("#execFilter")?.value || "all"));
    if (pg) pg.innerHTML = "";
  }
}

async function runPlaybook(pid) {
  let id = pid || selectedId;
  let inputs = {};
  if (!id) {
    // no playbook chosen yet — offer a picker modal instead of a bare prompt
    let opts = (playbooksCache || []).map((p) => p.playbook_id).filter(Boolean);
    if (!opts.length) {
      try {
        const d = await api("/playbooks");
        opts = (d.playbooks || []).map((p) => p.playbook_id).filter(Boolean);
      } catch { /* none */ }
    }
    if (!opts.length) return toast("No playbooks available to run", true);
    const v = await formModal({
      title: "Run playbook",
      subtitle: "Runs the published version of the selected playbook.",
      fields: [
        { name: "playbook_id", label: "Playbook", type: "select", options: opts },
        { name: "inputs", label: "Inputs JSON", type: "area", value: "{}", required: false, hint: 'e.g. {"target":"127.0.0.1"}' },
      ],
      submitLabel: "▶ Run",
    });
    if (!v) return;
    id = v.playbook_id;
    if (v.inputs && v.inputs.trim()) {
      try { inputs = JSON.parse(v.inputs); } catch { return toast("Inputs must be valid JSON", true); }
    }
  } else {
    const v = await formModal({
      title: `Run “${id}”`,
      subtitle: "Runs the published version.",
      fields: [{ name: "inputs", label: "Inputs JSON", type: "area", value: "{}", required: false, hint: 'e.g. {"target":"127.0.0.1"}' }],
      submitLabel: "▶ Run",
    });
    if (!v) return;
    if (v.inputs && v.inputs.trim()) {
      try { inputs = JSON.parse(v.inputs); } catch { return toast("Inputs must be valid JSON", true); }
    }
  }
  toast(`Starting ${id}…`);
  try {
    const r = await api("/executions", { method: "POST", body: JSON.stringify({ playbook_id: id, inputs }) });
    const execId = r?.execution?.execution_id || r?.execution_id || "";
    toast(`Run started${execId ? `: ${execId}` : ""}`);
    if (execId) location.hash = `#/executions/${encodeURIComponent(execId)}`;
    else { location.hash = "#/executions"; }
  } catch (err) { toast(err.message, true); }
}

async function pageExecutions() {
  setPageMeta({
    crumb: "Executions", title: "Executions", subtitle: "Live workflow runs",
    actions: [{ label: "▶ Run…", fn: () => runPlaybook() }, { label: "Refresh", fn: () => refreshExecutions($("#execFilter")?.value || "all") }],
  });
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">EXECUTIONS</div><h1>Executions</h1>
    <div class="subtitle">Workflow runs from the DAG engine, newest first. Click an execution for step detail.</div>`;
  $("#pageBody").innerHTML = `
    <div class="filterbar">
      <select id="execFilter" class="select">
        <option value="all">All statuses</option>
        <option value="running">running</option>
        <option value="pending">pending</option>
        <option value="completed">completed</option>
        <option value="failed">failed</option>
        <option value="cancelled">cancelled</option>
      </select>
      <label class="checkline"><input type="checkbox" id="execAuto" checked /> auto-refresh every 5s</label>
    </div>
    <div id="execPagination"></div>
    <div id="execTable">Loading…</div>`;
  $("#execFilter").addEventListener("change", () => { execPage.offset = 0; refreshExecutions($("#execFilter").value); });
  $("#execAuto").addEventListener("change", () => {
    if ($("#execAuto").checked) execTimer = setInterval(() => refreshExecutions($("#execFilter").value), 5000);
    else if (execTimer) { clearInterval(execTimer); execTimer = null; }
  });
  // default on: start the live timer immediately (route() clears it on navigation)
  if (!execTimer) execTimer = setInterval(() => refreshExecutions($("#execFilter")?.value || "all"), 5000);
  await refreshExecutions("all");
}

/* Compact error/output preview for a single step row. */
function stepPreview(s) {
  const err = s.error;
  if (err) {
    const t = String(err).trim();
    return t
      ? `<span class="dim" style="color:var(--red)">${esc(t.slice(0, 140))}${t.length > 140 ? "…" : ""}</span>`
      : `<span class="dim" style="color:var(--red)">failed</span>`;
  }
  const out = s.outputs;
  if (out !== null && out !== undefined) {
    if (typeof out === "object") {
      const keys = Object.keys(out);
      if (!keys.length) return `<span class="dim">no output</span>`;
      const k = keys[0];
      return `<span class="dim">${esc(k)}: ${esc(String(out[k]).slice(0, 140))}</span>`;
    }
    const t = String(out).trim();
    return t ? `<span class="dim">${esc(t.slice(0, 140))}</span>` : `<span class="dim">no output</span>`;
  }
  return `<span class="dim">—</span>`;
}

function renderExecutionDetail(id, e) {
  const running = e.status === "running" || e.status === "pending";
  const resumable = e.status === "failed" || e.status === "cancelled";
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">EXECUTIONS</div><h1>${esc(id)}</h1>
    <div class="subtitle">${esc(e.playbook_id || "")} <span style="margin-left:8px">${BADGE(e.status)}</span>
      ${e.trigger ? ` <span style="margin-left:8px">trigger: <b class="mono">${esc(e.trigger)}</b></span>` : ""}
      ${Number(e.debug) ? ` <span class="chip ck" style="margin-left:8px">debug</span>` : ""}
    </div>`;
  $("#pageBody").innerHTML = `
    <div class="detail-actions" id="execActions">
      <button class="btn danger" id="cancelExecBtn" ${running ? "" : "hidden"}>Cancel</button>
      <button class="btn" id="resumeExecBtn" ${resumable ? "" : "hidden"}>Resume from checkpoint</button>
      <button class="btn primary" id="retryExecBtn" ${!running ? "" : "hidden"}>Retry</button>
      <a class="btn ghost" href="#/executions">← back</a>
    </div>
    <div class="panel" style="margin-top:10px;padding:10px"><pre class="result">${colorJson(e)}</pre></div>
    <div class="two-col" style="margin-top:10px">
      <div class="panel" style="min-height:auto">
        <div class="panel-head"><span>Steps</span></div>
        <div id="execSteps" style="padding:6px">${table(["Step", "Tool", "Status", "Output / error"], (e.steps || []).map((s) => ({
          cells: [esc(s.step_id), esc(s.tool_id || "—"), BADGE(s.status), stepPreview(s)],
          menu: s.status === "failed"
            ? `<button class="btn ghost" data-rerun-step="${esc(s.step_id)}">Re-run step</button>`
            : "",
        })), "No steps recorded.")}</div>
      </div>
      <div class="panel" style="min-height:auto">
        <div class="panel-head"><span>Artifacts</span></div>
        <div style="padding:6px">${table(["Artifact", "Category"], (e.artifacts || []).map((a) => ({
          cells: [esc(a.name || a.artifact_id || a.evidence_id || "—"), esc(a.category || "—")],
        })), "No artifacts recorded.")}</div>
      </div>
    </div>
  `;
  // move execution KPIs to page-top right as minimal badges
  const headRight = $(".head-right");
  if (headRight) {
    ["steps","artifacts","completed"].forEach((k) => { const old = headRight.querySelector(".exec-kpi-"+k); if (old) old.remove(); });
    const stepsCount = (e.steps || []).length;
    const artifactsCount = (e.artifacts || []).length;
    const completedAt = fmtTs(e.completed_at);
    [["exec-kpi-steps", stepsCount + " steps"], ["exec-kpi-artifacts", artifactsCount + " artifacts"], ["exec-kpi-completed", completedAt]].forEach(([cls, label]) => {
      const badge = document.createElement("span");
      badge.className = "exec-kpi-badge " + cls;
      badge.textContent = label;
      badge.style.cssText = "padding:4px 10px; background:var(--panel2); border:1px solid var(--line); border-radius:9px; font-size:12.5px; color:var(--muted); margin-left:8px; white-space:nowrap;";
      headRight.appendChild(badge);
    });
  }
  $("#cancelExecBtn").addEventListener("click", async () => {
    if (!confirm(`Cancel ${id}?`)) return;
    const b = $("#cancelExecBtn");
    busyBtn(b, true);
    try {
      await api(`/executions/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      toast("Cancelled");
    } catch (err) { toast(err.message, true); }
    busyBtn(b, false);
    pageExecutionDetail(id);
  });
  $("#resumeExecBtn").addEventListener("click", async () => {
    const b = $("#resumeExecBtn");
    busyBtn(b, true);
    try {
      await api(`/executions/${encodeURIComponent(id)}/resume`, { method: "POST" });
      toast("Resume requested");
    } catch (err) { toast(err.message, true); }
    busyBtn(b, false);
    pageExecutionDetail(id);
  });
  $("#retryExecBtn").addEventListener("click", async () => {
    /* Re-run the same playbook with the same stored inputs as a new execution. */
    let inputs = e.inputs;
    if (typeof inputs === "string") {
      try { inputs = JSON.parse(inputs); } catch { inputs = {}; }
    }
    if (!inputs || typeof inputs !== "object" || Array.isArray(inputs)) inputs = {};
    try {
      const r = await api("/executions", {
        method: "POST",
        body: JSON.stringify({ playbook_id: e.playbook_id, inputs, trigger: "studio" }),
      });
      const execId = r?.execution?.execution_id || r?.execution_id || "";
      if (execId) {
        toast("Retry started");
        location.hash = `#/executions/${encodeURIComponent(execId)}`;
      } else {
        toast("Retry started — open the executions list to track it");
      }
    } catch (err) { toast(err.message, true); }
  });
  /* Delegated “Re-run step” on failed steps. No per-step retry endpoint exists,
   * so explain the limitation instead of hitting a missing route. */
  $("#execSteps").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-rerun-step]");
    if (!btn) return;
    ev.preventDefault();
    toast("Per-step re-run isn't available via the API yet — use “Resume from checkpoint” to continue this execution, or “Retry” to start a fresh run with the same inputs.");
  });
}

let execDetailTimer = null;

async function pageExecutionDetail(id) {
  setPageMeta({ crumb: "Executions", title: id, subtitle: "Execution detail" });
  let d;
  try {
    d = await api(`/executions/${encodeURIComponent(id)}`);
  } catch (err) {
    $("#pageBody").innerHTML = `<div class="panel" style="margin-top:10px;padding:16px"><p class="empty">Execution “${esc(id)}” could not be loaded: ${esc(err.message)}</p></div>`;
    return;
  }
  const e = d?.execution;
  if (!e) {
    $("#pageBody").innerHTML = `<div class="panel" style="margin-top:10px;padding:16px"><p class="empty">No execution with id “${esc(id)}”. <a href="#/executions">← back to executions</a></p></div>`;
    return;
  }
  renderExecutionDetail(id, e);
  // live-poll while the run is still in flight (route() clears the timer)
  if (execDetailTimer) { clearInterval(execDetailTimer); execDetailTimer = null; }
  if (e.status === "running" || e.status === "pending") {
    execDetailTimer = setInterval(async () => {
      try {
        const det = await api(`/executions/${encodeURIComponent(id)}`);
        const ex = det?.execution;
        if (!ex) return;
        renderExecutionDetail(id, ex);
        if (ex.status !== "running" && ex.status !== "pending") {
          clearInterval(execDetailTimer); execDetailTimer = null;
          toast(ex.status === "completed" ? "Run completed" : `Run ${ex.status}`);
        }
      } catch { /* keep polling */ }
    }, 2000);
  }
}

/* ── route: tool registry ──────────────────────────────────────────── */

let toolsCache = [];
let toolsPage = { limit: 25, offset: 0 };

async function refreshTools() {
  const el = $("#toolTable");
  const hint = $("#toolHint");
  if (el) el.innerHTML = `<div class="panel table-panel"><p class="empty">Loading tools…</p></div>`;
  try {
    const d = await api("/tools?limit=500&offset=0");
    toolsCache = d.tools || [];
    toolsPage.offset = 0;
    renderTools();
    if (hint) {
      if (toolsCache.length) {
        hint.textContent = "";
      } else {
        const srv = await toolRegistryHint().catch(() => "");
        hint.innerHTML = `<div class="panel" style="padding:12px"><p class="empty">No tools registered — import MCP connections to populate the registry.</p>
          <p class="dim" style="font-size:11px">This list is read straight from the running server's database. ${esc(srv)}<br/>
          If you expected tools here: hard-refresh this page (Cmd/Ctrl+Shift+R) to drop a cached script, and check that you are connected to the intended server/port.</p></div>`;
      }
    }
  } catch (err) {
    if (el) el.innerHTML = emptyStatePanel("Failed to load tools", err.message, "Retry");
    el.querySelector("[data-retry]")?.addEventListener("click", refreshTools);
    if (hint) hint.textContent = "";
  }
}

async function toolRegistryHint() {
  try {
    const s = await api("/server");
    const parts = [`db: ${s.db_path || "unknown"} (${s.db_exists ? "file exists" : "file missing"})`];
    return parts.join(" · ");
  } catch { return ""; }
}

function renderTools() {
  const el = $("#toolTable");
  const pg = $("#toolPagination");
  if (!el) return;
  const q = ($("#toolSearch").value || "").toLowerCase();
  const all = toolsCache.filter((t) =>
    !q || String(t.tool_id).toLowerCase().includes(q) || String(t.name || "").toLowerCase().includes(q));
  const { limit, offset } = toolsPage;
  const total = all.length;
  const pageRows = total ? all.slice(offset, offset + limit) : [];
  const from = total ? offset + 1 : 0;
  const to = Math.min(offset + limit, total);
  const hasPrev = offset > 0;
  const hasNext = offset + limit < total;
  if (!pageRows.length) {
    el.innerHTML = emptyStatePanel("No tools match this search", "Try a different keyword or clear the search field.", "Retry");
    el.querySelector("[data-retry]")?.addEventListener("click", refreshTools);
    if (pg) pg.innerHTML = "";
    return;
  }
  const bar = $("#toolBulkBar");
  if (bar) bar.style.display = total ? "" : "none";
  el.innerHTML = table(["Tool", "Provider", "Visibility", "Enabled", "Timeout"],
    pageRows.map((t) => {
      const isBuiltin = t.provider === "native" || t.provider === "builtin";
      const opts = ["agent_callable", "playbook_selectable", "internal", "hidden"]
        .map((v) => `<option value="${v}" ${v === t.visibility ? "selected" : ""}>${v}</option>`).join("");
      const aliasFor = t.alias_for;
      const aliasChip = aliasFor
        ? `<span class="chip alias" title="Alias resolves to ${esc(aliasFor)}">→ ${esc(aliasFor)}</span>`
        : "";
      return {
        cells: [
          `<label class="checkline" style="margin:0;display:flex;align-items:center;gap:8px"><input type="checkbox" class="row-sel" value="${esc(t.tool_id)}">${esc(t.tool_id)}</label>`,
          esc(t.provider || "—"),
          `<select class="select sel-vis" data-tool="${esc(t.tool_id)}">${opts}</select>${aliasChip}`,
          t.enabled ? "yes" : "no", esc(t.default_timeout ?? "—")],
        menu: `<button class="btn mini" data-toolact="test" data-tool="${esc(t.tool_id)}">Test</button>${isBuiltin ? ' <span class="dim">builtin</span>' : ""}`,
      };
    }));
  $$(".sel-vis", el).forEach((sel) => sel.addEventListener("change", async () => {
    try {
      const id = sel.dataset.tool;
      await api(`/tools/${encodeURIComponent(id)}/visibility`, { method: "PUT", body: JSON.stringify({ visibility: sel.value }) });
      toast(`${id} → ${sel.value}`);
    } catch (err) { toast(err.message, true); }
  }));
  $$("input.row-sel", el).forEach((cb) => cb.addEventListener("change", () => {
    const all = $$("input.row-sel", el);
    const n = all.filter((c) => c.checked).length;
    $("#bulkCount").textContent = n ? `(${n} selected)` : "";
  }));
  $$("[data-toolact=test]", el).forEach((btn) => btn.addEventListener("click", async () => {
    const id = btn.dataset.tool;
    const t = toolsCache.find((x) => x.tool_id === id);
    if (t) await testTool(t);
  }));
  if (pg) {
    pg.innerHTML = paginationHtml({ page: Math.floor(offset / limit) + 1, pageSize: limit, total, hasPrev, hasNext, from, to });
    wirePagination(pg, {
      onPrev: () => { toolsPage.offset = Math.max(0, toolsPage.offset - toolsPage.limit); renderTools(); },
      onNext: () => { toolsPage.offset += toolsPage.limit; renderTools(); },
      onSize: (size) => { toolsPage.limit = +size; toolsPage.offset = 0; renderTools(); },
    });
  }
}

async function pageTools() {
  setPageMeta({
    crumb: "Tool Registry", title: "Tool Registry", subtitle: "Live catalog from the database",
    actions: [{ label: "Refresh", fn: refreshTools }],
  });
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">TOOL REGISTRY</div><h1>Tool Registry</h1>
    <div class="subtitle">Every tool known to the backend. Changing visibility writes straight to the DB (promoting to agent_callable may require an approval).</div>`;
  $("#pageBody").innerHTML = `
    <div id="toolHint"></div>
    <div class="filterbar"><input class="select" id="toolSearch" placeholder="Search tool id or name…" /></div>
    <div id="toolBulkBar" style="display:none;gap:10px;align-items:center;margin:8px 0 6px">
      <label class="checkline"><input type="checkbox" id="bulkSelectAll"> Select all</label>
      <span class="dim" style="margin:0 6px">· set visibility</span>
      <select class="select" id="bulkVisibility">
        <option value="agent_callable">agent_callable</option>
        <option value="playbook_selectable">playbook_selectable</option>
        <option value="internal">internal</option>
        <option value="hidden">hidden</option>
      </select>
      <button class="btn primary" id="bulkApply">Apply to selected</button>
      <span id="bulkCount" class="dim"></span>
    </div>
    <div id="toolPagination"></div>
    <div id="toolTable">Loading…</div>`;
  $("#toolSearch").addEventListener("input", () => { toolsPage.offset = 0; renderTools(); });
  $("#bulkSelectAll")?.addEventListener("change", (e) => {
    const all = $$("input.row-sel", $("#toolTable"));
    all.forEach((cb) => { cb.checked = e.target.checked; });
    cbChange();
  });
  const cbChange = () => {
    const all = $$("input.row-sel", $("#toolTable"));
    const n = all.filter((c) => c.checked).length;
    $("#bulkSelectAll").checked = n > 0 && n === all.length;
    $("#bulkCount").textContent = n ? `(${n} selected)` : "";
  };
  $("#toolTable").addEventListener("change", (e) => { if (e.target.matches("input.row-sel")) cbChange(); });
  $("#bulkApply")?.addEventListener("click", async () => {
    const boxes = $$("input.row-sel:checked", $("#toolTable"));
    if (!boxes.length) return toast("Select at least one tool", true);
    const visibility = $("#bulkVisibility").value;
    const failed = [];
    for (const cb of boxes) {
      try {
        await api(`/tools/${encodeURIComponent(cb.value)}/visibility`, {
          method: "PUT", body: JSON.stringify({ visibility }),
        });
      } catch { failed.push(cb.value); }
    }
    const ok = boxes.length - failed.length;
    toast(failed.length
      ? `Visibility → ${visibility} for ${ok}/${boxes.length} (${failed.length} failed)`
      : `Visibility → ${visibility} for ${ok} tool${ok === 1 ? "" : "s"}`, failed.length > 0);
    await refreshTools();
  });
  await refreshTools();
}

/* Test a single tool: render inputs dynamically from its input_schema, run
   via POST /api/tools/{id}/run, and show the result in the same modal. */
async function testTool(t) {
  const rawSchema = t.input_schema;
  let schema = null;
  if (rawSchema && typeof rawSchema === "object" && !Array.isArray(rawSchema)) schema = rawSchema;
  else if (typeof rawSchema === "string") {
    try { schema = JSON.parse(rawSchema); } catch { schema = null; }
  }
  const props = schema && schema.type !== "object" ? null : (schema?.properties || {});
  const required = schema?.required || [];
  const fields = Object.entries(props || {}).map(([name, prop]) => {
    const type = String(prop?.type || "string");
    const req = required.includes(name);
    let input;
    if (type === "boolean") {
      input = `<select class="select" data-arg="${esc(name)}" data-argtype="boolean">
        <option value="">— not set</option><option value="true">true</option><option value="false">false</option></select>`;
    } else if (type === "number" || type === "integer") {
      input = `<input class="select" type="number" data-arg="${esc(name)}" data-argtype="${type}"
        placeholder="${esc(prop?.placeholder ?? (type === "number" ? "e.g. 1.5" : "e.g. 30"))}">`;
    } else if (type === "array" || type === "object") {
      const deflt = prop?.default === undefined ? "" : JSON.stringify(prop.default);
      input = `<textarea class="select area mono" data-arg="${esc(name)}" data-argtype="${type}"
        style="min-height:56px;font-family:ui-monospace,Menlo,monospace" spellcheck="false"
        placeholder="JSON ${type}">${esc(deflt)}</textarea>`;
    } else {
      input = `<input class="select" data-arg="${esc(name)}" data-argtype="string"
        placeholder="${esc(prop?.description || "")}">`;
    }
    return `<div class="run-row" style="margin:8px 0">
      <div class="run-row-head"><b class="mono">${esc(name)}</b>
        <span class="chip ${req ? "ck" : ""}">${esc(type)}${req ? " · required" : ""}</span>
        ${helpTip(prop?.description || "")}
      </div>${input}</div>`;
  }).join("") || `<p class="dim">This tool takes no declared inputs.</p>`;

  const bd = document.createElement("div");
  bd.className = "modal-backdrop show";
  bd.innerHTML = `
    <div class="modal run-modal" style="max-width:660px">
      <h3>Test tool “${esc(t.tool_id)}”</h3>
      <p class="dim">${esc(t.name || t.tool_id)} · provider ${esc(t.provider || "—")} · visibility ${esc(t.visibility || "—")}</p>
      <div id="testToolFields">${fields}</div>
      <div id="testToolResult" hidden style="margin-top:12px;border:1px solid var(--line);border-radius:9px;padding:10px">
        <div style="font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin-bottom:6px">Result</div>
        <pre class="result" id="testToolResultBody" style="max-height:300px;overflow:auto"></pre>
      </div>
      <div class="modal-actions">
        <button class="btn" id="testToolCancel">Cancel</button>
        <button class="btn primary" id="testToolRun">▶ Run tool</button>
      </div>
    </div>`;
  document.body.appendChild(bd);
  const runBtn = bd.querySelector("#testToolRun");
  bd.querySelector("#testToolCancel").addEventListener("click", () => bd.remove());
  bd.addEventListener("mousedown", (e) => { if (e.target === bd) bd.remove(); });
  runBtn.addEventListener("click", async () => {
    const args = {};
    let ok = true;
    bd.querySelectorAll("[data-arg]").forEach((el) => {
      const name = el.dataset.arg;
      const type = el.dataset.argtype || "string";
      const raw = el.value;
      if (type === "boolean") { if (raw) args[name] = raw === "true"; return; }
      if (type === "integer") { if (String(raw).trim() === "") return; args[name] = parseInt(raw, 10); return; }
      if (type === "number") { if (String(raw).trim() === "") return; args[name] = parseFloat(raw); return; }
      if (type === "array" || type === "object") {
        const txt = String(raw).trim();
        if (!txt) return;
        try { args[name] = JSON.parse(txt); }
        catch { toast(`Argument “${name}” must be valid JSON`, true); ok = false; }
        return;
      }
      const txt = String(raw).trim();
      if (txt) args[name] = txt;
    });
    if (!ok) return;
    runBtn.disabled = true;
    const resPanel = bd.querySelector("#testToolResult");
    const resBody = bd.querySelector("#testToolResultBody");
    resPanel.hidden = true;
    resBody.textContent = "";
    try {
      const res = await api(`/tools/${encodeURIComponent(t.tool_id)}/run`, {
        method: "POST", body: JSON.stringify({ args }),
      });
      resPanel.hidden = false;
      resBody.innerHTML = colorJson(res && typeof res.result !== "undefined" ? res.result : res);
      toast("Tool ran");
    } catch (err) {
      toast(err.message, true);
    } finally {
      runBtn.disabled = false;
    }
  });
}

/* ── route: connections ────────────────────────────────────────────── */

let connectionsCache = [];

async function refreshConnections() {
  const el = $("#connTable");
  if (!el) return;
  el.innerHTML = `<div class="panel table-panel"><p class="empty">Loading connections…</p></div>`;
  try {
    const d = await api("/connections");
    connectionsCache = d.connections || [];
    renderConnections();
  } catch (err) {
    el.innerHTML = emptyStatePanel("Failed to load connections", err.message, "Retry");
    el.querySelector("[data-retry]")?.addEventListener("click", refreshConnections);
  }
}

function renderConnections() {
  const el = $("#connTable");
  if (!el) return;
  el.innerHTML = table(["Connection", "Transport", "Endpoint", "Health", "Exported tools"],
    connectionsCache.map((c) => ({
      cells: [
        `<a href="#/connections/${encodeURIComponent(c.connection_id)}">${esc(c.connection_id)}</a><br/><small class="dim">${esc(c.name || "")}</small>`,
        esc(c.transport || "—"),
        esc(c.endpoint_url || c.command || "—"),
        c.enabled ? BADGE(c.health_status || "unknown") : "disabled",
        esc(c.imported_tool_count ?? 0),
      ],
      menu: `<button class="btn mini" data-conn="${esc(c.connection_id)}" data-connact="edit">Edit</button>
             <button class="btn mini" data-conn="${esc(c.connection_id)}" data-connact="test">Test</button>
             <button class="btn mini" data-conn="${esc(c.connection_id)}" data-connact="discover">Discover</button>
             <button class="btn mini danger" data-conn="${esc(c.connection_id)}" data-connact="delete">Delete</button>`,
    }), { emptyText: "No connections yet. Add one to import external MCP tools.", emptyLabel: "Add connection" }));
  el.querySelector("[data-retry]")?.addEventListener("click", connectionModal);
  $$("button[data-conn]", el).forEach((btn) => btn.addEventListener("click", async () => {
    const id = btn.dataset.conn;
    const act = btn.dataset.connact;
    if (act === "edit") { await editConnection(id); return; }
    if (act === "delete" && !confirm(`Delete connection ${id}?`)) return;
    const map = {
      test: { path: `/connections/${encodeURIComponent(id)}/test`, method: "POST" },
      discover: { path: `/connections/${encodeURIComponent(id)}/discover`, method: "POST", body: "{}" },
      delete: { path: `/connections/${encodeURIComponent(id)}`, method: "DELETE" },
    }[act];
    busyBtn(btn, true);
    try { await api(map.path, { method: map.method, body: map.body }); toast(act + " ok"); refreshConnections(); }
    catch (err) { toast(err.message, true); }
    busyBtn(btn, false);
  }));
}

async function connectionModal() {
  const v = await formModal({
    title: "Add MCP connection",
    subtitle: "Registers an external MCP server. Test connectivity and import tools from its detail page afterwards.",
    fields: [
      { name: "connection_id", label: "connection_id (unique)", placeholder: "e.g. fs-local" },
      { name: "name", label: "Friendly name", placeholder: "e.g. Local filesystem server" },
      { name: "transport", label: "Transport", type: "select", value: "sse", options: ["sse", "streamable-http", "stdio"] },
      { name: "endpoint_url", label: "endpoint_url", required: false, placeholder: "http://127.0.0.1:9000/sse", hint: "Required for sse / streamable-http transports." },
      { name: "command", label: "command", required: false, placeholder: "e.g. npx mcp-server-fs", hint: "Required for the stdio transport." },
    ],
    submitLabel: "Add connection",
  });
  if (!v) return;
  const payload = { connection_id: v.connection_id, name: v.name, transport: v.transport };
  if (v.endpoint_url) payload.endpoint_url = v.endpoint_url;
  if (v.command) payload.command = v.command;
  try {
    await api("/connections", { method: "POST", body: JSON.stringify(payload) });
    toast("Connection added");
    await refreshConnections();
  } catch (err) { toast(err.message, true); }
}

async function editConnection(id) {
  const d = await api(`/connections/${encodeURIComponent(id)}`);
  const c = d.connection;
  const v = await formModal({
    title: "Edit connection",
    subtitle: `Editing ${esc(c.connection_id)}`,
    fields: [
      { name: "name", label: "Friendly name", value: c.name || "" },
      { name: "transport", label: "Transport", type: "select", value: c.transport || "sse", options: ["sse", "streamable-http", "stdio"] },
      { name: "endpoint_url", label: "endpoint_url", required: false, value: c.endpoint_url || "", placeholder: "http://127.0.0.1:9000/sse", hint: "Required for sse / streamable-http transports." },
      { name: "command", label: "command", required: false, value: c.command || "", placeholder: "e.g. npx mcp-server-fs", hint: "Required for the stdio transport." },
      { name: "description", label: "Description", required: false, value: c.description || "", placeholder: "Optional description" },
    ],
    submitLabel: "Save changes",
  });
  if (!v) return;
  const payload = { name: v.name, transport: v.transport };
  if (v.endpoint_url !== undefined) payload.endpoint_url = v.endpoint_url;
  if (v.command !== undefined) payload.command = v.command;
  if (v.description !== undefined) payload.description = v.description;
  try {
    await api(`/connections/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(payload) });
    toast("Connection updated");
    await refreshConnections();
    if (location.hash === `#/connections/${encodeURIComponent(id)}`) await pageConnectionDetail(id);
  } catch (err) { toast(err.message, true); }
}

async function pageConnections() {
  setPageMeta({
    crumb: "MCP Connections", title: "MCP Connections", subtitle: "External servers & imported tool counts",
    actions: [{ label: "Refresh", fn: refreshConnections }, { label: "＋ Add connection", fn: connectionModal }],
  });
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">MCP CONNECTIONS</div><h1>MCP Connections</h1>
    <div class="subtitle">Registered external MCP servers. Test probes connectivity; Discover imports tools (imported as internal visibility by default).</div>`;
  $("#pageBody").innerHTML = `<div id="connTable">Loading…</div>`;
  await refreshConnections();
}

async function pageConnectionDetail(id) {
  setPageMeta({ crumb: "MCP Connections", title: `${esc(id)} connection`, subtitle: "Connection detail" });
  const d = await api(`/connections/${encodeURIComponent(id)}`);
  const c = d.connection;
  const health = c.health_status || "unknown";
  const endpoint = c.endpoint_url || c.command || "—";
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">MCP CONNECTIONS</div><h1>${esc(c.name || id)}</h1>
    <div class="subtitle">${esc(c.connection_id)} · ${esc(c.transport)} ${BADGE(health)}</div>`;
  $("#pageHeadActions").innerHTML = headActionsHtml([
    { id: "connEditBtn", label: "Edit" },
    { id: "connDelBtn", label: "Delete connection", cls: "danger" },
  ]);
  $("#pageBody").innerHTML = `
    <div class="detail-actions"><a class="btn ghost" href="#/connections">← back</a></div>
    <div class="two-col">
      <div class="panel">
        <div class="panel-head"><span>Identity</span></div>
        <div style="padding:8px">${table(["Field", "Value"], [
          { cells: ["Connection id", `<span class="mono">${esc(c.connection_id)}</span>`] },
          { cells: ["Name", esc(c.name || "—")] },
          { cells: ["Transport", esc(c.transport)] },
        ])}</div>
      </div>
      <div class="panel">
        <div class="panel-head"><span>Endpoint &amp; health</span></div>
        <div style="padding:8px">${table(["Field", "Value"], [
          { cells: ["endpoint_url / command", `<span class="mono">${esc(endpoint)}</span>`] },
          { cells: ["Health", BADGE(health)] },
          { cells: ["Enabled", c.enabled ? "Yes" : "No"] },
        ])}</div>
      </div>
    </div>
    <div class="panel" style="margin-top:16px">
      <div class="panel-head"><span>Imported tools</span>
        <button class="btn mini" id="discoverBtn">Discover &amp; import</button></div>
      <div style="padding:8px">${table(["Tool", "Visibility"], (d.imported_tools || []).map((t) => ({
        cells: [esc(t.tool_id), esc(t.visibility || "—")],
      })), "Nothing imported from this connection yet. Use Discover &amp; import to pull tools from the server.")}</div>
    </div>`;
  $("#discoverBtn").addEventListener("click", async () => {
    const b = $("#discoverBtn");
    busyBtn(b, true);
    try {
      const r = await api(`/connections/${encodeURIComponent(id)}/discover`, { method: "POST", body: "{}" });
      toast(`Imported ${(r.imported_tools || []).length} tools`);
      pageConnectionDetail(id);
    } catch (err) { toast(err.message, true); }
    busyBtn(b, false);
  });
  $("#connEditBtn").addEventListener("click", () => editConnection(id));
  $("#connDelBtn").addEventListener("click", async () => {
    if (!confirm(`Delete connection "${esc(c.connection_id)}"?`)) return;
    const b = $("#connDelBtn");
    busyBtn(b, true);
    try {
      await api(`/connections/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("Connection deleted");
      location.hash = "#/connections";
    } catch (err) { toast(err.message, true); }
    busyBtn(b, false);
  });
}

/* ── route: agents ─────────────────────────────────────────────────── */

async function refreshAgents() {
  const el = $("#agentTable");
  if (!el) return;
  el.innerHTML = `<div class="panel table-panel"><p class="empty">Loading agent profiles…</p></div>`;
  try {
    const d = await api("/agents");
    const agents = d.agents || [];
    el.innerHTML = table(["Profile", "Description", "Default visibility", "Parallel / Timeout", "Grants"],
      agents.map((a) => ({
        cells: [
          `<a href="#/agents/${encodeURIComponent(a.profile_id)}">${esc(a.profile_id)}</a><br/><small class="dim">${esc(a.name || "")}</small>`,
          esc(a.description || "—"),
          esc(a.default_visibility || "internal"),
          `${a.max_parallel_operations ?? "—"} / ${a.max_execution_seconds ?? "—"}s`,
          `tools: ${a.tool_grants ?? 0} · playbooks: ${a.playbook_grants ?? 0}`,
        ],
      })), { emptyText: "No agent profiles. One is created for you on first tool grant.", emptyLabel: "New profile" });
    el.querySelector("[data-retry]")?.addEventListener("click", agentModal);
  } catch (err) {
    el.innerHTML = emptyStatePanel("Failed to load agents", err.message, "Retry");
    el.querySelector("[data-retry]")?.addEventListener("click", refreshAgents);
  }
}

async function agentModal() {
  const v = await formModal({
    title: "New agent profile",
    subtitle: "Deny-by-default: grant tools and playbooks explicitly from the profile page.",
    fields: [
      { name: "profile_id", label: "profile_id (unique)", placeholder: "e.g. redteam-agent" },
      { name: "name", label: "Friendly name", placeholder: "e.g. Red team agent" },
      { name: "description", label: "Description", type: "area", required: false, placeholder: "What this agent is allowed to do…" },
    ],
    submitLabel: "Create profile",
  });
  if (!v) return;
  const payload = { profile_id: v.profile_id, name: v.name };
  if (v.description) payload.description = v.description;
  try {
    await api("/agents", { method: "POST", body: JSON.stringify(payload) });
    toast("Agent profile created");
    refreshAgents();
  } catch (err) { toast(err.message, true); }
}

async function pageAgents() {
  setPageMeta({
    crumb: "Agents", title: "Agents", subtitle: "Profiles, tool grants & playbook assignments",
    actions: [{ label: "Refresh", fn: refreshAgents }, { label: "＋ New profile", fn: agentModal }],
  });
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">AGENTS</div><h1>Agents</h1>
    <div class="subtitle">Deny-by-default catalogs: an agent can only call tools and run playbooks explicitly granted to its profile.</div>`;
  $("#pageBody").innerHTML = `<div id="agentTable">Loading…</div>`;
  await refreshAgents();
}

async function editAgentProfile(id) {
  const d = await api(`/agents/${encodeURIComponent(id)}`);
  const a = d.agent;
  const v = await formModal({
    title: "Edit profile",
    subtitle: `Rename “${esc(a.name || id)}” and update its description.`,
    fields: [
      { name: "name", label: "Name", value: a.name || "", placeholder: "e.g. Red team agent" },
      { name: "description", label: "Description", type: "area", required: false,
        value: a.description || "", placeholder: "What this agent is allowed to do…" },
    ],
    submitLabel: "Save changes",
  });
  if (!v) return;
  try {
    await api(`/agents/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ name: v.name, description: v.description }),
    });
    toast("Profile updated");
    pageAgentDetail(id);
  } catch (err) { toast(err.message, true); }
}

async function pageAgentDetail(id) {
  setPageMeta({ crumb: "Agents", title: id, subtitle: "Agent profile" });
  const d = await api(`/agents/${encodeURIComponent(id)}`);
  const a = d.agent;
  const catalogs = d.effective_catalog || {};
  const perms = (d.permissions || []).filter((p) => p.profile_id === id);
  const grantedTools = perms.filter((p) => p.granted);
  const asgs = (d.assignments || []).filter((x) => x.profile_id === id);
  const assignedPlaybooks = asgs.filter((x) => x.granted);
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">AGENTS</div><h1>${esc(id)}</h1>
    <div class="subtitle">${esc(a.name || "")} · parallel ${a.max_parallel_operations ?? "—"} · timeout ${a.max_execution_seconds ?? "—"}s</div>`;
  $("#pageBody").innerHTML = `
    <div class="detail-actions"><a class="btn ghost" href="#/agents">← back</a>
      <button class="btn" id="editAgentBtn">Edit profile</button>
      <button class="btn danger" id="delAgentBtn">Delete profile</button></div>
    ${kpis([
      { label: "Callable tools", value: catalogs.tools?.length ?? 0 },
      { label: "Assignable playbooks", value: catalogs.playbooks?.length ?? 0 },
      { label: "Default visibility", value: a.default_visibility || "internal" },
    ])}
    <div class="panel" style="min-height:140px;margin-top:14px">
      <div class="panel-head"><span>Tool grants</span>
        <button class="btn mini" id="grantBtn">Grant tool</button>
        <button class="btn mini danger" id="revokeAllBtn" ${grantedTools.length ? "" : "hidden"}>Revoke all</button>
      </div>
      <div style="padding:8px">${table(["Tool", "Status", "Granted at", "Action"],
        perms.map((p) => ({
          cells: [
            esc(p.tool_id),
            p.granted ? "<span class='run-state ok'>granted</span>" : "<span class='run-state failed'>denied</span>",
            fmtTs(p.granted_at),
            `<button class="btn mini danger" data-tool="${esc(p.tool_id)}" data-toolact="revoke">Revoke</button>`,
          ],
        })), "No tool grants yet. Grant one to allow this agent to call a tool.")}</div>
    </div>
    <div class="panel" style="min-height:140px;margin-top:14px">
      <div class="panel-head"><span>Playbook assignments</span>
        <button class="btn mini" id="assignBtn">Assign playbook</button>
        <button class="btn mini danger" id="unassignAllBtn" ${assignedPlaybooks.length ? "" : "hidden"}>Unassign all</button>
      </div>
      <div style="padding:8px">${table(["Playbook", "Status", "Granted at", "Action"],
        asgs.map((x) => ({
          cells: [
            esc(x.playbook_id),
            x.granted ? "<span class='run-state ok'>assigned</span>" : "<span class='run-state failed'>blocked</span>",
            fmtTs(x.granted_at),
            `<button class="btn mini danger" data-pb="${esc(x.playbook_id)}" data-pbact="unassign">Unassign</button>`,
          ],
        })), "No playbook assignments yet.")}</div>
    </div>`;
  $("#editAgentBtn").addEventListener("click", () => editAgentProfile(id));
  $("#grantBtn").addEventListener("click", async () => {
    const b = $("#grantBtn");
    busyBtn(b, true);
    try {
      const d = await api("/tools?limit=500");
      const opts = (d.tools || []).map((t) => t.tool_id).filter(Boolean).sort();
      if (!opts.length) { busyBtn(b, false); return toast("No tools registered to grant", true); }
      const v = await formModal({
        title: "Grant tool",
        subtitle: `Allows profile “${esc(id)}” to call this tool.`,
        fields: [{ name: "tool_id", label: "Tool", type: "select", options: opts }],
        submitLabel: "Grant",
      });
      if (!v) { busyBtn(b, false); return; }
      await api(`/agents/${encodeURIComponent(id)}/tools/${encodeURIComponent(v.tool_id)}`, { method: "PUT", body: "{}" });
      toast("Granted");
    } catch (err) { toast(err.message, true); }
    busyBtn(b, false);
    pageAgentDetail(id);
  });
  $("#revokeAllBtn").addEventListener("click", async () => {
    if (!confirm(`Revoke all ${grantedTools.length} tool grant${grantedTools.length === 1 ? "" : "s"} for “${esc(id)}”?`)) return;
    const failed = [];
    for (const p of grantedTools) {
      try {
        await api(`/agents/${encodeURIComponent(id)}/tools/${encodeURIComponent(p.tool_id)}`, { method: "DELETE" });
      } catch { failed.push(p.tool_id); }
    }
    const okCount = grantedTools.length - failed.length;
    if (failed.length) return toast(`Revoked ${okCount}/${grantedTools.length}; failed: ${failed.join(", ")}`, true);
    toast(`Revoked ${grantedTools.length} tool grant${grantedTools.length === 1 ? "" : "s"}`);
    pageAgentDetail(id);
  });
  $("#assignBtn").addEventListener("click", async () => {
    const b = $("#assignBtn");
    busyBtn(b, true);
    try {
      const d = await api("/playbooks");
      const opts = (d.playbooks || []).map((p) => p.playbook_id).filter(Boolean).sort();
      if (!opts.length) { busyBtn(b, false); return toast("No playbooks available to assign", true); }
      const v = await formModal({
        title: "Assign playbook",
        subtitle: `Allows profile “${esc(id)}” to run this playbook.`,
        fields: [{ name: "playbook_id", label: "Playbook", type: "select", options: opts }],
        submitLabel: "Assign",
      });
      if (!v) { busyBtn(b, false); return; }
      await api(`/agents/${encodeURIComponent(id)}/playbooks/${encodeURIComponent(v.playbook_id)}`, { method: "PUT", body: "{}" });
      toast("Assigned");
    } catch (err) { toast(err.message, true); }
    busyBtn(b, false);
    pageAgentDetail(id);
  });
  $("#unassignAllBtn").addEventListener("click", async () => {
    if (!confirm(`Unassign all ${assignedPlaybooks.length} playbook${assignedPlaybooks.length === 1 ? "" : "s"} from “${esc(id)}”?`)) return;
    const failed = [];
    for (const x of assignedPlaybooks) {
      try {
        await api(`/agents/${encodeURIComponent(id)}/playbooks/${encodeURIComponent(x.playbook_id)}`, { method: "DELETE" });
      } catch { failed.push(x.playbook_id); }
    }
    const okCount = assignedPlaybooks.length - failed.length;
    if (failed.length) return toast(`Unassigned ${okCount}/${assignedPlaybooks.length}; failed: ${failed.join(", ")}`, true);
    toast(`Unassigned ${assignedPlaybooks.length} playbook${assignedPlaybooks.length === 1 ? "" : "s"}`);
    pageAgentDetail(id);
  });
  $("#delAgentBtn").addEventListener("click", async () => {
    if (!confirm(`Delete agent profile ${esc(id)}?`)) return;
    const b = $("#delAgentBtn");
    busyBtn(b, true);
    try {
      await api(`/agents/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("Deleted");
      location.hash = "#/agents";
    } catch (err) { toast(err.message, true); busyBtn(b, false); }
    busyBtn(b, false);
  });
  $$("button[data-tool]", $("#pageBody")).forEach((b) => b.addEventListener("click", async () => {
    busyBtn(b, true);
    try {
      await api(`/agents/${encodeURIComponent(id)}/tools/${encodeURIComponent(b.dataset.tool)}`, { method: "DELETE" });
    } catch (err) { toast(err.message, true); }
    busyBtn(b, false);
    pageAgentDetail(id);
  }));
  $$("button[data-pb]", $("#pageBody")).forEach((b) => b.addEventListener("click", async () => {
    busyBtn(b, true);
    try {
      await api(`/agents/${encodeURIComponent(id)}/playbooks/${encodeURIComponent(b.dataset.pb)}`, { method: "DELETE" });
    } catch (err) { toast(err.message, true); }
    busyBtn(b, false);
    pageAgentDetail(id);
  }));
}

/* ── route: findings ───────────────────────────────────────────────── */

/* rows cached by id so the row click can open the full detail modal */
let findingsCache = [];
let fndPage = { limit: 25, offset: 0 };
let fndSort = { col: null, dir: "asc" };
let findingsTimer = null;

function findingDetailModal(f) {
  const bd = document.createElement("div");
  bd.className = "modal-backdrop show";
  const rows = [
    ["Severity", BADGE(f.severity) || esc(f.severity || "—")],
    ["Tool", esc(f.tool || "—")],
    ["Target", esc(f.target || `#${f.target_id ?? "—"}`)],
    ["Type", esc(f.finding_type || "—")],
    ["Status", esc(f.status || f.validation_status || "—")],
    ["Port / service", f.port ? `${esc(String(f.port))}${f.service ? " · " + esc(f.service) : ""}` : esc(f.service || "—")],
    ["URL", f.url
      ? `<a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer">${esc(f.url)}</a>`
      : "—"],
    ["Source tools", esc(f.source_tools || "—")],
    ["Created", fmtTs(f.created_at)],
    ["Updated", fmtTs(f.updated_at)],
  ];
  const block = (label, content, emptyNote) => `
    <div class="run-row">
      <div class="run-row-head"><b>${esc(label)}</b></div>
      ${content || `<p class="dim" style="margin:0">${esc(emptyNote)}</p>`}
    </div>`;
  bd.innerHTML = `
    <div class="modal fnd-detail-modal">
      <h3>${esc(f.title || f.test_name || f.checkname || f.name || "Finding")}</h3>
      <div class="fnd-detail-grid">
        ${rows.map(([k, v]) => `<div class="fnd-detail-kv"><span class="fnd-detail-k">${esc(k)}</span><span class="fnd-detail-v">${v}</span></div>`).join("")}
      </div>
      ${block("Description", f.description ? `<p style="margin:0;white-space:pre-wrap;font-size:12.5px;line-height:1.55">${esc(f.description)}</p>` : "", "No description recorded.")}
      ${block("Evidence", f.evidence
        ? `<pre class="mono" style="margin:0;white-space:pre-wrap;font-size:11.5px;max-height:220px;overflow:auto">${esc(f.evidence)}</pre>`
        : "", "No evidence captured.")}
      <div class="modal-actions">
        <button class="btn" id="fndDetailClose">Close</button>
      </div>
    </div>`;
  document.body.appendChild(bd);
  bd.addEventListener("click", (e) => {
    if (e.target === bd) bd.remove();
  });
  bd.querySelector("#fndDetailClose").addEventListener("click", () => bd.remove());
}

/* Move the matching count to the page-top right corner (minimal number badge). */
function updateFindingsCountBadge(total) {
  const headRight = $(".head-right");
  if (!headRight) return;
  const old = headRight.querySelector(".fnd-count-badge");
  if (old) old.remove();
  const badge = document.createElement("span");
  badge.className = "fnd-count-badge";
  badge.textContent = `${total ?? 0} findings`;
  badge.style.cssText = "padding:4px 10px; background:var(--panel2); border:1px solid var(--line); border-radius:9px; font-size:12.5px; color:var(--muted); margin-left:8px; white-space:nowrap;";
  headRight.appendChild(badge);
}

/* Column indexes: 0 Title, 1 Severity, 2 Tool, 3 Target, 4 Status, 5 Created.
   Sorting and pagination are performed by the API (see findings_api); the
   frontend renders the returned page verbatim — no second slice. */
const FND_SORT_FIELDS = ["title", "severity", "tool", "target", "status", "created_at"];

async function refreshFindings() {
  const el = $("#findingTable");
  const pg = $("#findingPagination");
  if (!el) return;
  const params = new URLSearchParams();
  const sev = $("#fndSeverity")?.value;
  const tgt = $("#fndTarget")?.value?.trim();
  const tool = $("#fndTool")?.value?.trim();
  if (sev && sev !== "all") params.set("severity", sev);
  if (tgt) params.set("target", tgt);
  if (tool) params.set("tool", tool);
  params.set("limit", fndPage.limit);
  params.set("offset", fndPage.offset);
  const sortField = FND_SORT_FIELDS[fndSort.col];
  if (sortField) {
    params.set("sort", sortField);
    params.set("dir", fndSort.dir);
  }
  el.innerHTML = `<div class="panel table-panel"><p class="empty">Loading findings…</p></div>`;
  if (pg) pg.innerHTML = "";
  try {
    const d = await api(`/findings?${params}`);
    findingsCache = d.findings || [];
    const total = d.count ?? 0;
    const pageRows = findingsCache; // backend already applied sort + pagination
    const from = total ? fndPage.offset + 1 : 0;
    const to = Math.min(fndPage.offset + fndPage.limit, total);
    const hasPrev = fndPage.offset > 0;
    const hasNext = fndPage.offset + fndPage.limit < total;
    if (!pageRows.length) {
      el.innerHTML = emptyStatePanel("No findings match these filters", "Try clearing filters or check that scan data exists.", "Retry");
      el.querySelector("[data-retry]")?.addEventListener("click", refreshFindings);
      if (pg) pg.innerHTML = "";
      updateFindingsCountBadge(0);
      return;
    }
    const byId = new Map(pageRows.map((f) => [String(f.id), f]));
    el.innerHTML = table(["Title", "Severity", "Tool", "Target", "Status", "Created"],
      pageRows.map((f) => ({
        id: String(f.id),
        cells: [esc(f.title || f.test_name || f.checkname || f.name || "—"), BADGE(f.severity), esc(f.tool), esc(f.target || "—"), esc(f.status || f.validation_status || "—"), fmtTs(f.created_at)],
        menu: `<button class="btn ghost fnd-view" data-fid="${esc(String(f.id))}">View</button>`,
      })),
      { sortable: [0, 1, 2, 3, 4, 5], sortState: fndSort, selectable: true });
    /* row click opens the detail modal — delegation survives table re-render */
    el.onclick = (e) => {
      if (e.target.tagName === "INPUT" && e.target.type === "checkbox") return;
      const btn = e.target.closest(".fnd-view");
      const row = e.target.closest(".table-row");
      if (btn || row) {
        const fid = btn?.dataset.fid || row?.querySelector(".fnd-view")?.dataset.fid;
        const f = byId.get(String(fid));
        if (f) findingDetailModal(f);
      }
    };
    /* Select all / per-row checkbox wiring */
    const selAll = $("#selAll");
    const selRows = $$(".sel-row", el);
    if (selAll) {
      selAll.addEventListener("change", () => {
        const checked = selAll.checked;
        selRows.forEach((cb) => { cb.checked = checked; });
      });
      selRows.forEach((cb) => cb.addEventListener("change", () => {
        const all = selRows.length > 0 && selRows.every((c) => c.checked);
        selAll.checked = all;
      }));
    }
    wireSortHeaders(el, (col) => {
      if (fndSort.col === col) fndSort.dir = fndSort.dir === "asc" ? "desc" : "asc";
      else { fndSort.col = col; fndSort.dir = "asc"; }
      fndPage.offset = 0; // a new sort starts from the first page
      refreshFindings();
    });
    if (pg) {
      pg.innerHTML = paginationHtml({ page: Math.floor(fndPage.offset / fndPage.limit) + 1, pageSize: fndPage.limit, total, hasPrev, hasNext, from, to });
      wirePagination(pg, {
        onPrev: () => { fndPage.offset = Math.max(0, fndPage.offset - fndPage.limit); refreshFindings(); },
        onNext: () => { fndPage.offset += fndPage.limit; refreshFindings(); },
        onSize: (size) => { fndPage.limit = +size; fndPage.offset = 0; refreshFindings(); },
      });
    }
    updateFindingsCountBadge(total);
    const bulk = $("#fndBulk");
    if (bulk) bulk.style.display = pageRows.length ? "" : "none";
  } catch (err) {
    el.innerHTML = emptyStatePanel("Failed to load findings", err.message, "Retry");
    el.querySelector("[data-retry]")?.addEventListener("click", refreshFindings);
    if (pg) pg.innerHTML = "";
    updateFindingsCountBadge(0);
  }
}

async function pageFindings() {
  setPageMeta({
    crumb: "Findings", title: "Findings", subtitle: "Normalized findings across all scans",
    actions: [{ label: "Refresh", fn: refreshFindings }],
  });
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">FINDINGS</div><h1>Findings</h1>
    <div class="subtitle">Query normalized findings. Filters hit the database directly.</div>`;
  /* Bulk actions toolbar (shown only when there are findings to act on). */
  $("#pageHeadActions").innerHTML = `
    <div class="bulk-toolbar" id="fndBulk" style="display:none;align-items:center;gap:8px;flex-wrap:wrap">
      <span class="dim">Bulk actions:</span>
      <select class="select" id="fndBulkAction" style="width:230px">
        <option value="">— Choose action —</option>
        <option value="resolved">Mark selected as resolved</option>
        <option value="open">Mark selected as open</option>
        <option value="delete">Delete selected findings</option>
      </select>
    </div>`;
  $("#pageBody").innerHTML = `
    <div class="filterbar" style="gap:8px">
      <select id="fndSeverity" class="select" style="width:150px">
        <option value="all">All severities</option>
        <option value="critical">critical</option><option value="high">high</option>
        <option value="medium">medium</option><option value="low">low</option>
        <option value="info">info</option>
      </select>
      <input class="select" id="fndTarget" placeholder="target…" style="width:180px" />
      <input class="select" id="fndTool" placeholder="tool…" style="width:180px" />
      <button class="btn primary" id="fndGo">Apply</button>
      <label class="checkline"><input type="checkbox" id="fndAuto" /> auto-refresh every 5s</label>
    </div>
    <div id="findingPagination"></div>
    <div id="findingTable">Loading…</div>`;
  $("#fndGo").addEventListener("click", () => { fndPage.offset = 0; refreshFindings(); });
  $("#fndSeverity").addEventListener("change", () => { fndPage.offset = 0; refreshFindings(); });
  $("#fndAuto").addEventListener("change", () => {
    if ($("#fndAuto").checked) findingsTimer = setInterval(refreshFindings, 5000);
    else if (findingsTimer) { clearInterval(findingsTimer); findingsTimer = null; }
  });
  $("#fndBulkAction").addEventListener("change", (e) => {
    const action = e.target.value;
    if (!action) return;
    e.target.value = ""; // reset so re-picking the same action re-triggers
    bulkFindingsAction(action);
  });
  await refreshFindings();
}

/* Bulk actions on selected findings: resolve / open / delete.
   Confirms, then calls PUT /api/findings/{id} or DELETE /api/findings/{id}
   per finding and toasts the count. */
async function bulkFindingsAction(action) {
  const el = $("#findingTable");
  if (!el) return;
  const cbs = $$(".sel-row:checked", el);
  const fids = cbs.map((cb) => cb.dataset.fid);
  if (!fids.length) return toast("No findings selected", true);
  const plural = fids.length !== 1 ? "s" : "";
  if (action === "delete") {
    if (!confirm(`Delete ${fids.length} finding${plural}?`)) return;
  } else if (!confirm(`Mark ${fids.length} findings as ${action}?`)) return;
  let ok = 0, fail = 0;
  for (const fid of fids) {
    try {
      if (action === "delete") {
        await api(`/findings/${encodeURIComponent(fid)}`, { method: "DELETE" });
      } else {
        await api(`/findings/${encodeURIComponent(fid)}`, {
          method: "PUT", body: JSON.stringify({ status: action }),
        });
      }
      ok++;
    } catch { fail++; }
  }
  if (action === "delete") toast(`Deleted ${ok} finding${ok !== 1 ? "s" : ""}.`);
  else toast(`Marked ${ok} findings as ${action}.${fail ? ` (${fail} failed)` : ""}`, fail > 0 && fail === ok);
  await refreshFindings();
}

/* ── route: settings ───────────────────────────────────────────────── */

async function pageSettings() {
  setPageMeta({ crumb: "Settings", title: "Settings", subtitle: "Server runtime configuration from config.py (MCP_* env vars).",
    actions: [{ label: "⟳ Refresh config", cls: "ghost", fn: () => { pageSettings().catch(() => {}); } }] });
  const s = await api("/server");
  $("#pageHeadInfo").innerHTML = `
    <div class="eyebrow">SETTINGS</div><h1>Server</h1>
    <div class="subtitle">Runtime configuration comes from config.py (MCP_* environment variables) — shown read-only.</div>`;
  $("#pageBody").innerHTML = `
    ${kpis([
      { label: "Server", value: `${s.server_name} v${s.server_version}` },
      { label: "Bind", value: `${s.host}:${s.port}` },
      { label: "Transport", value: s.transport || "—" },
      { label: "Auth", value: s.auth_enabled ? "token required" : "disabled (insecure)" },
    ])}
    <div class="two-col" style="margin-top:14px">
      <div class="panel"><div class="panel-head"><span>Limits</span></div>
        <div style="padding:12px;line-height:2;font-size:12px">
          Default timeout: <strong>${esc(s.default_timeout)}s</strong><br/>
          Max timeout: <strong>${esc(s.max_timeout)}s</strong><br/>
          Max concurrent operations: <strong>${esc(s.max_concurrent)}</strong><br/>
          Debug mode:
          <label class="checkline" style="margin:0 0 0 8px;display:inline-block">
            <input type="checkbox" id="debugToggle" ${s.debug ? "checked" : ""}>
            <span id="debugToggleLabel">${s.debug ? "on" : "off"}</span>
          </label>
          ${s.uptime_seconds != null ? `<br/>Server uptime: <strong>${fmtUptime(s.uptime_seconds)}</strong>` : ""}
          ${s.active_executions != null ? `<br/>Active executions: <strong>${esc(String(s.active_executions))}</strong>` : ""}
          ${s.total_requests != null ? `<br/>Total requests: <strong>${esc(String(s.total_requests))}</strong>` : ""}</div></div>
      <div class="panel"><div class="panel-head"><span>Token</span></div>
        <div style="padding:12px;font-size:12px;line-height:1.8">
          ${s.auth_enabled
            ? "This server requires a bearer token for every /api call. It is stored in this browser only."
            : "No token required (KALI_MCP_INSECURE or unset MCP_API_TOKEN)."}
          <div style="margin-top:10px"><button class="btn" id="resetTokenBtn">Clear stored token</button></div>
        </div></div>
    </div>`;
  $("#resetTokenBtn").addEventListener("click", () => {
    localStorage.removeItem(TOKEN_KEY);
    toast("Stored token cleared");
    refreshAuthBadge();
  });
  const debugToggle = $("#debugToggle");
  if (debugToggle) {
    debugToggle.addEventListener("change", async (e) => {
      const el = e.target;
      el.disabled = true;
      try {
        await api("/server/debug", { method: "PUT", body: JSON.stringify({ debug: el.checked }) });
        $("#debugToggleLabel").textContent = el.checked ? "on" : "off";
        toast(`Debug mode ${el.checked ? "on" : "off"}`);
      } catch (err) {
        el.checked = !el.checked;
        toast(err.message, true);
      } finally {
        el.disabled = false;
      }
    });
  }
}

/* ── auth badge ────────────────────────────────────────────────────── */

async function refreshAuthBadge() {
  const badge = $("#authState");
  const srv = $("#serverMeta");
  try {
    const s = await api("/server");
    if (badge) badge.textContent = s.auth_enabled ? "Token protected" : "Insecure mode";
    if (srv) srv.textContent = `${s.server_name} v${s.server_version}`;
  } catch {
    if (badge) badge.textContent = "Token required";
    if (srv) srv.textContent = "unauthenticated";
  }
}

/* ── router ────────────────────────────────────────────────────────── */

const ROUTES = {
  overview: { title: "Overview", render: pageOverview, crumb: "Overview" },
  playbooks: { title: "Playbook Studio", render: pagePlaybooks, crumb: "Playbook Studio" },
  executions: { title: "Executions", render: pageExecutions, crumb: "Executions" },
  tools: { title: "Tool Registry", render: pageTools, crumb: "Tool Registry" },
  connections: { title: "MCP Connections", render: pageConnections, crumb: "MCP Connections" },
  agents: { title: "Agents", render: pageAgents, crumb: "Agents" },
  findings: { title: "Findings", render: pageFindings, crumb: "Findings" },
  settings: { title: "Settings", render: pageSettings, crumb: "Settings" },
};

function parseHash() {
  const hash = (location.hash || "").replace(/^#\/?/, "");
  const [route, id] = hash.split("/");
  return { route: ROUTES[route] ? route : "overview", id };
}

async function route() {
  if (execTimer) { clearInterval(execTimer); execTimer = null; }
  if (findingsTimer) { clearInterval(findingsTimer); findingsTimer = null; }
  if (runStatusTimer) { clearInterval(runStatusTimer); runStatusTimer = null; }
  if (execDetailTimer) { clearInterval(execDetailTimer); execDetailTimer = null; }
  // persist any unsaved inspector edits when leaving the editor page
  const { route: currRoute, id: currId } = parseHash();
  if (currRoute === "playbooks" && currId && graphSel) {
    const s = stepById(graphSel);
    if (s) applyTabFields(s);
  }
  const { route: name, id } = parseHash();
  $$("#mainNav .nav-item").forEach((btn) => btn.classList.toggle("active", btn.dataset.route === name));
  setPageMeta({ crumb: ROUTES[name].crumb, title: ROUTES[name].title });
  await render();
  try {
    if (name === "executions" && id) await pageExecutionDetail(decodeURIComponent(id));
    else if (name === "connections" && id) await pageConnectionDetail(decodeURIComponent(id));
    else if (name === "agents" && id) await pageAgentDetail(decodeURIComponent(id));
    else if (name === "playbooks" && id) await pagePlaybookEditor(decodeURIComponent(id));
    else await ROUTES[name].render();
  } catch (err) {
    $("#pageBody").insertAdjacentHTML("beforeend", `<p class="error" style="padding:20px">${esc(err.message)}</p>`);
  }
}

/* ── keyboard shortcuts ──────────────────────────────────────────────
 * ?: show/hide a keyboard-help panel
 * Ctrl/Cmd + K: focus the current page's search input
 * Escape: close any open modal (run, playbook create, finder detail, token…)
 * Escape also dismisses the help panel.
 */
let keyHelpOpen = false;

function focusSearch() {
  const sel = ["#pbSearch", "#toolSearch", "#fndTarget", "#fndTool"];
  const el = sel.map((s) => $(s)).find((e) => e && !e.hidden && e.offsetParent !== null);
  if (el) {
    el.focus();
    try { el.select(); } catch { } // select text in text inputs
  }
}

function toggleKeyHelp() {
  keyHelpOpen = !keyHelpOpen;
  let el = $("#keyHelp");
  if (!el) {
    el = document.createElement("div");
    el.id = "keyHelp";
    el.innerHTML = `
      <div class="panel" style="position:fixed;top:110px;left:50%;transform:translateX(-50%);min-width:280px;max-width:92vw;width:430px;padding:22px 26px;z-index:260">
        <h3 style="margin:0 0 10px">Keyboard shortcuts</h3>
        <div style="display:grid;gap:6px;font-size:12.5px;color:var(--muted)">
          <div><b>?</b> Show this help</div>
          <div><b>Ctrl/Cmd + K</b> Focus search</div>
          <div><b>Escape</b> Close open modals</div>
          <div><b>Ctrl/Cmd + Z</b> Undo · <b>Ctrl/Cmd + Y</b> Redo (graph &amp; DSL)</div>
          <div><b>Delete / Backspace</b> Delete selected graph node</div>
        </div>
        <button class="btn primary" id="keyHelpClose" style="margin-top:12px">Got it</button>
      </div>`;
    document.body.appendChild(el);
    el.querySelector("#keyHelpClose").addEventListener("click", toggleKeyHelp);
  }
  el.hidden = !keyHelpOpen;
  if (keyHelpOpen) el.querySelector("#keyHelpClose")?.focus();
}

function closeOpenModals() {
  const open = $$(".modal-backdrop.show");
  if (open.length) { open[open.length - 1].remove(); return true; }
  if (keyHelpOpen) { toggleKeyHelp(); return true; }
  return false;
}

function bootShell() {
  $$("#mainNav .nav-item").forEach((btn) =>
    btn.addEventListener("click", () => { location.hash = `#/${btn.dataset.route}`; }));
  const wsBtn = $("#workspaceBtn");
  if (wsBtn) wsBtn.addEventListener("click", () => toast("Single workspace — the running server is this workspace."));
  $("#tokenSave").addEventListener("click", saveTokenAndRetry);
  $("#tokenInput").addEventListener("keydown", (e) => { if (e.key === "Enter") saveTokenAndRetry(); });

  /* sidebar minimize toggle — works on every page */
  const sidebarToggle = $("#sidebarToggle");
  if (sidebarToggle) {
    const sidebarHidden = localStorage.getItem("kmcp_sidebar_minimized") === "1";
    if (sidebarHidden) $("#app")?.classList.add("sidebar-minimized");
    sidebarToggle.addEventListener("click", () => {
      const isMinimized = $("#app").classList.toggle("sidebar-minimized");
      localStorage.setItem("kmcp_sidebar_minimized", isMinimized ? "1" : "0");
    });
  }

  window.addEventListener("hashchange", route);

  /* ── global tooltip bubble (position: fixed so it never gets clipped) ── */
  const bubble = document.createElement("div");
  bubble.className = "tooltip-bubble";
  document.body.appendChild(bubble);

  let hoverTimer = null;
  const showTip = (tip) => {
    clearTimeout(hoverTimer);
    const text = tip.getAttribute("data-tip");
    if (!text) return;
    bubble.textContent = text;
    const rect = tip.getBoundingClientRect();
    const bubbleW = bubble.offsetWidth || 200;
    const bubbleH = bubble.offsetHeight || 40;
    // try above, then below, staying within viewport
    let top = rect.top - bubbleH - 8;
    let placeBelow = false;
    if (top < 8) {
      top = rect.bottom + 8;
      placeBelow = true;
    }
    if (top + bubbleH > window.innerHeight - 8) {
      top = window.innerHeight - bubbleH - 8;
    }
    if (top < 8) top = 8;
    // center horizontally, clamp to viewport
    let left = rect.left + rect.width / 2 - bubbleW / 2;
    left = Math.min(Math.max(left, 8), window.innerWidth - bubbleW - 8);
    bubble.style.top = `${top}px`;
    bubble.style.left = `${left}px`;
    bubble.style.borderRadius = "9px";
    bubble.style.display = "block";
  };
  const hideTip = () => {
    hoverTimer = setTimeout(() => { bubble.style.display = "none"; }, 120);
  };
  document.body.addEventListener("mouseover", (e) => {
    const tip = e.target.closest(".help-tip");
    if (tip) { clearTimeout(hoverTimer); showTip(tip); }
  });
  document.body.addEventListener("mouseout", (e) => {
    const tip = e.target.closest(".help-tip");
    if (tip) hideTip();
  });
  bubble.addEventListener("mouseenter", () => { clearTimeout(hoverTimer); });
  bubble.addEventListener("mouseleave", () => { hideTip(); });

  /* Global keyboard shortcuts. */
  document.addEventListener("keydown", (e) => {
    const active = document.activeElement;
    const typing = active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA"
      || active.isContentEditable);
    if (e.key === "Escape") { closeOpenModals(); return; }
    if (typing) return;
    if (e.key === "?") { e.preventDefault(); toggleKeyHelp(); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      focusSearch();
    }
  });
}

(() => {
  bootShell();
  refreshAuthBadge().then(() => route()).catch(() => route());
})();
