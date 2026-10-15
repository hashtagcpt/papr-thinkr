// papr-thinkr frontend -- vanilla JS, no build step.
// Everything that came from a document or a model is untrusted: always go
// through esc() before putting it into innerHTML.

const state = {
  slug: null,
  project: null,           // {config, state, summary, jobs, editable}
  selectedCommentId: null,
  chatThreadId: "project-main",
  browseTarget: null,
  browseDirs: false,
  browseCwd: "",
  filter: "all",
  sort: "doc",
  calm: false,
  showOriginal: false,
  search: "",
  backends: [],
  modalMode: "create",
  job: null,               // {id, since}
  jobTimer: null,
};

const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (m) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[m]));
}
const pct = (x) => (x === null || x === undefined || isNaN(x)) ? "n/a" : `${Math.round(Number(x) * 100)}%`;
const label = (s) => String(s || "").replace(/_/g, " ");

function hours(lo, hi) {
  const unit = (h) => h < 1 ? ["min", 60] : h < 16 ? ["h", 1] : h < 160 ? ["d", 1 / 8] : ["wk", 1 / 40];
  const fmt = (x) => x < 10 ? String(+x.toFixed(1)) : String(Math.round(x));
  const [ul, fl] = unit(lo), [uh, fh] = unit(hi);
  return ul === uh ? `${fmt(lo * fl)}–${fmt(hi * fh)} ${uh}` : `${fmt(lo * fl)} ${ul}–${fmt(hi * fh)} ${uh}`;
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) throw new Error((data && data.error) ? data.error : `HTTP ${res.status}`);
  return data;
}

const P = () => `/api/projects/${enc(state.slug)}`;

// ---------------------------------------------------------------- backends --

async function refreshBackends(force) {
  const pill = $("backendPill");
  try {
    const res = await api("GET", "/api/backends" + (force ? "?refresh=1" : ""));
    state.backends = res.backends;
    state.defaultSettings = res.settings;
    const usable = res.backends.filter((b) => b.available);
    const proj = state.project && state.project.config.llm && state.project.config.llm.backend
      ? state.project.config.llm : res.settings;
    const cur = res.backends.find((b) => b.id === proj.backend);
    const model = proj.model || (cur && cur.default_model) || "default";
    pill.textContent = `${proj.backend}: ${model}`;
    pill.className = "status-pill " + (cur && cur.available ? "ok" : "bad");
    pill.title = `${usable.length} usable backend(s): ${usable.map((b) => b.id).join(", ")}`;
    $("introBackends").innerHTML = "<strong>Available on this machine:</strong> " +
      (usable.map((b) => `${esc(b.label)}${b.models.length && b.kind === "local" ? ` (${b.models.length} models)` : ""}`).join(" · ") || "none detected");
  } catch (e) {
    pill.textContent = "server error";
    pill.className = "status-pill bad";
  }
}

function fillBackendSelect(sel, current, includeDefault) {
  sel.innerHTML = (includeDefault ? `<option value="">server default</option>` : "") +
    state.backends.map((b) => `<option value="${esc(b.id)}">${esc(b.label)}${b.available ? "" : " — unavailable"}</option>`).join("");
  sel.value = current || "";
}

function fillModelList(datalist, backendId) {
  const b = state.backends.find((x) => x.id === backendId);
  datalist.innerHTML = (b ? b.models : []).map((m) => `<option value="${esc(m)}">`).join("");
}

function renderBackendsModal() {
  $("backendsList").innerHTML = state.backends.map((b) => `
    <div class="backend-row">
      <span class="name">${esc(b.id)}</span>
      <span class="${b.available ? "ok" : "no"}">${b.available ? "✓ available" : "✗ unavailable"}</span>
      <span class="muted">${esc(b.detail)}</span>
    </div>`).join("");
  fillBackendSelect($("defaultBackend"), state.defaultSettings.backend, false);
  $("defaultModel").value = state.defaultSettings.model || "";
  fillModelList($("defaultModelList"), state.defaultSettings.backend);
  loadKeys();
}

async function loadKeys() {
  try {
    const res = await api("GET", "/api/keys");
    $("keysWhere").textContent = `Stored in ${res.stored_in}`;
    $("keysList").innerHTML = res.keys.map((k) => `
      <div class="key-row">
        <div><strong>${esc(k.name)}</strong> <span class="${k.set ? "ok" : "muted"}">${k.set ? `✓ ${esc(k.masked)} (${esc(k.source)})` : "not set"}</span>
          <div class="muted small-text">${esc(k.description)}</div></div>
        <input type="password" autocomplete="off" data-key="${esc(k.name)}" placeholder="${k.name === "OPENAI_BASE_URL" ? "http://127.0.0.1:1234/v1" : "paste key"}" />
        <button class="small keySave" data-key="${esc(k.name)}">Save</button>
        ${k.set && k.source !== "environment" ? `<button class="small keyRemove" data-key="${esc(k.name)}">Remove</button>` : ""}
      </div>`).join("");
    $("keysList").querySelectorAll(".keySave").forEach((b) => b.addEventListener("click", async () => {
      const input = $("keysList").querySelector(`input[data-key="${b.dataset.key}"]`);
      if (!input.value.trim()) return;
      await api("POST", "/api/keys", { name: b.dataset.key, value: input.value.trim() });
      await refreshBackends(true);
      renderBackendsModal();
    }));
    $("keysList").querySelectorAll(".keyRemove").forEach((b) => b.addEventListener("click", async () => {
      await api("POST", "/api/keys", { name: b.dataset.key, value: "" });
      await refreshBackends(true);
      renderBackendsModal();
    }));
  } catch (e) {
    $("keysList").textContent = `Could not load keys: ${e.message}`;
  }
}

// -------------------------------------------------------------- projects ---

async function loadProjectsList() {
  const projects = await api("GET", "/api/projects");
  const sel = $("projectSelect");
  sel.innerHTML = '<option value="">Select a project…</option>' +
    projects.map((p) => `<option value="${esc(p.slug)}">${esc(p.name)}${p.load_error ? " (error)" : ""}</option>`).join("");
  if (state.slug) sel.value = state.slug;
}

async function selectProject(slug) {
  stopJobPolling();
  state.slug = slug || null;
  state.selectedCommentId = null;
  state.chatThreadId = "project-main";
  $("editProjectBtn").classList.toggle("hidden", !slug);
  if (!slug) {
    state.project = null;
    $("emptyState").classList.remove("hidden");
    $("projectView").classList.add("hidden");
    $("jobBar").classList.add("hidden");
    refreshBackends();
    return;
  }
  try { localStorage.setItem("papr.slug", slug); } catch (e) { /* ignore */ }
  await refreshProject();
  $("emptyState").classList.add("hidden");
  configureReviewModel();
  $("projectView").classList.remove("hidden");
  $("reportDocxLink").href = `${P()}/report.docx`;
  $("letterDocxLink").href = `${P()}/response-letter.docx`;
  renderAll();
  refreshBackends();
  await loadChat();
  const running = (state.project.jobs || []).find((j) => j.status === "running" || j.status === "queued");
  if (running) trackJob(running);
}

async function refreshProject() {
  state.project = await api("GET", P());
  renderRevisionBanner();
}

function renderAll() {
  renderRevisionBanner();
  renderComments();
  if (getSelectedComment()) renderDetail(); else showOverview();
  const hasReview = !!(state.project && state.project.state.review);
  $("runReviewBtn").textContent = hasReview ? "↻ Re-run review" : "▶ Run review";
}

// ---------------------------------------------------------------- items ----

function effortOf(c) {
  return (c.triage && c.triage.effort) || (c.critique && c.critique.effort) || null;
}
const SEV = { critical: 0, major: 1, minor: 2, editorial: 3, unclear: 4 };

function needsAttention(c) {
  const d = c.decoded || {};
  return (d.misunderstanding && d.misunderstanding !== "none") || (d.clarity && d.clarity !== "clear") ||
    ["curt", "hostile"].includes(d.tone);
}

function isFigureItem(c) {
  return (c.figure_refs || []).length > 0 || ((c.critique || {}).category === "figures_tables") ||
    /^Figure \d/.test(c.location_ref || "");
}

function visibleComments() {
  let list = (state.project && state.project.state.comments) || [];
  const f = state.filter;
  if (f === "reviewer") list = list.filter((c) => (c.origin || "reviewer") === "reviewer");
  if (f === "attention") list = list.filter(needsAttention);
  if (f === "ai") list = list.filter((c) => c.origin === "ai-review");
  if (f === "science") list = list.filter((c) => c.critique && c.critique.scope);
  if (f === "strengthening") list = list.filter((c) => c.critique && c.critique.issue_kind === "strengthening");
  if (f === "uncaught") list = list.filter((c) => c.critique?.reviewer_coverage?.relationship === "not_raised");
  if (f === "figures") list = list.filter(isFigureItem);
  if (f === "open") list = list.filter((c) => !["addressed", "wontfix"].includes(c.status));
  const q = state.search.trim().toLowerCase();
  if (q) list = list.filter((c) => (c.comment_text + " " + c.reviewer + " " + c.id + " " +
    ((c.decoded || {}).plain_language || "")).toLowerCase().includes(q));
  const s = state.sort;
  const byKey = (fn) => list.slice().sort((a, b) => fn(a) - fn(b));
  if (s === "severity") list = byKey((c) => SEV[c.severity] ?? 5);
  if (s === "effort") list = byKey((c) => -((effortOf(c) || {}).hours_high || 0));
  if (s === "confidence") list = byKey((c) => -(((c.critique || {}).confidence) ?? ((c.triage || {}).agreement_confidence) ?? 0));
  if (s === "stance") list = byKey((c) => ({ rebut: 0, clarify: 1, address_partially: 2 }[(c.triage || {}).stance] ?? 3));
  return list;
}

function calmText(c) {
  const d = c.decoded || {};
  if (!state.calm || (c.origin || "reviewer") !== "reviewer") return c.comment_text;
  if (["curt", "hostile"].includes(d.tone) && d.neutral_rewrite) return d.neutral_rewrite;
  return d.plain_language || c.comment_text;
}

function renderComments() {
  const list = $("commentsList");
  const all = (state.project && state.project.state.comments) || [];
  const comments = visibleComments();
  const nRev = all.filter((c) => (c.origin || "reviewer") === "reviewer").length;
  $("itemsTitle").textContent = `Items (${all.length})` + (nRev ? ` · ${nRev} from reviewers` : "");
  if (all.length === 0) {
    const mode = state.project && state.project.summary.mode;
    list.innerHTML = `<div class="empty-state small">${mode === "revision"
      ? "Reviewer comments are being extracted, or none were found."
      : "No items yet. Click <strong>▶ Run review</strong> to have papr-thinkr critique this draft."}</div>`;
    return;
  }
  if (comments.length === 0) {
    list.innerHTML = '<div class="empty-state small">Nothing matches this filter.</div>';
    return;
  }
  let lastGroup = null;
  const html = [];
  for (const c of comments) {
    const group = state.sort === "doc" ? ((c.origin || "reviewer") === "reviewer" ? c.reviewer : "papr-thinkr critique") : null;
    if (group && group !== lastGroup) {
      html.push(`<div class="group-label">${esc(group)}</div>`);
      lastGroup = group;
    }
    const e = effortOf(c);
    const t = c.triage || {};
    const cr = c.critique || {};
    const who = c.origin === "ai-review" ? (cr.personas || []).slice(0, 1).join("") || "AI" : c.reviewer;
    html.push(`
      <div class="comment-card${c.id === state.selectedCommentId ? " selected" : ""}${c.stale ? " stale" : ""}" data-id="${esc(c.id)}">
        <div class="meta">
          <strong>${esc(who)}</strong>
          <span class="badge severity-${esc(c.severity || "unclear")}">${esc(c.severity || "unclear")}</span>
          ${c.status && c.status !== "unaddressed" ? `<span class="badge ${c.status === "addressed" ? "status-addressed" : ""}">${esc(label(c.status))}</span>` : ""}
          ${t.stance && t.stance !== "address" ? `<span class="badge stance-${esc(t.stance)}">${esc(label(t.stance))}</span>` : ""}
          ${(c.decoded || {}).misunderstanding && c.decoded.misunderstanding !== "none" ? `<span class="badge misread" title="${esc(c.decoded.misunderstanding_explanation)}">misread?</span>` : ""}
          ${(c.decoded || {}).clarity && c.decoded.clarity !== "clear" ? `<span class="badge unclear" title="The comment is ambiguous">unclear</span>` : ""}
          ${["curt", "hostile"].includes((c.decoded || {}).tone) ? `<span class="badge harsh" title="Harsh tone. Turn on calm view to read it in neutral words.">${esc(c.decoded.tone)}</span>` : ""}
          ${(c.resolved_refs || []).length ? `<span class="badge" title="Line references resolved against the submitted PDF">↦ ${esc(c.resolved_refs.map((r) => r.ref).join(", "))}</span>` : ""}
          ${e ? `<span class="badge effort" title="Estimated effort (confidence ${pct(e.confidence)})">${esc(hours(e.hours_low, e.hours_high))}</span>` : ""}
          ${cr.confidence !== undefined ? `<span class="badge ai" title="Confidence this is a real problem">${pct(cr.confidence)}</span>` : ""}
          ${c.stale ? `<span class="badge" title="Not found again in the latest review">stale</span>` : ""}
          <button class="deleteCommentBtn" title="Not a real issue / boilerplate — remove it">✕</button>
        </div>
        <div class="snippet">${esc(calmText(c))}</div>
      </div>`);
  }
  list.innerHTML = html.join("");
  list.querySelectorAll(".comment-card").forEach((div) => {
    div.addEventListener("click", () => selectComment(div.dataset.id));
    div.querySelector(".deleteCommentBtn").addEventListener("click", (ev) => {
      ev.stopPropagation();
      deleteComment(div.dataset.id);
    });
  });
}

function getSelectedComment() {
  if (!state.project) return null;
  return state.project.state.comments.find((c) => c.id === state.selectedCommentId) || null;
}

function selectComment(id) {
  state.selectedCommentId = id;
  state.showOriginal = false;
  renderComments();
  renderDetail();
}

function showOverview() {
  state.selectedCommentId = null;
  $("detailContent").classList.add("hidden");
  $("overview").classList.remove("hidden");
  renderOverview();
  renderComments();
}

function renderOverview() {
  const el = $("overview");
  const p = state.project;
  const r = p.state.review;
  const mode = p.summary.mode;
  if (!r) {
    el.innerHTML = `
      <h3>No review yet</h3>
      <p>${mode === "revision"
        ? `This project has <strong>${p.summary.n_reviewer_comments}</strong> reviewer comment(s). <strong>▶ Run review</strong> will triage every comment (is the reviewer right? how much work, with what confidence? should you rebut?), look for conflicts between reviewers, and${p.config.review.independent_review ? " run an independent critique to catch what they missed." : " skip the independent critique (disabled in project settings)."}`
        : `No reviewer comments: <strong>▶ Run review</strong> will critique this draft like a panel of demanding referees (depth: <strong>${esc(p.config.review.depth)}</strong>).`}</p>
      <p class="muted">The result is a Word report with an overall effort estimate and confidence, plus trackable items here.
      ${(p.config.code_paths || []).length || (p.config.data_paths || []).length ? "Code and data will be audited too." : "Add code/data folders in ⚙ Project to have them audited against the paper."}</p>`;
    return;
  }
  const o = r.overall || {};
  const eff = r.effort || {};
  const rec = { accept: "Accept", minor_revision: "Minor revision", major_revision: "Major revision", reject: "Reject" }[o.recommendation] || "—";
  const kpi = (labelTxt, value, sub, meter) => `
    <div class="kpi"><div class="label">${esc(labelTxt)}</div><div class="value">${esc(value)}</div>
      ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}
      ${meter !== undefined ? `<div class="meter"><div style="width:${Math.round((meter || 0) * 100)}%"></div></div>` : ""}</div>`;
  const comments = p.state.comments;
  const rev = comments.filter((c) => (c.origin || "reviewer") === "reviewer");
  const ai = comments.filter((c) => c.origin === "ai-review" && !c.stale);
  const stances = {};
  rev.forEach((c) => { const s = (c.triage || {}).stance; if (s) stances[s] = (stances[s] || 0) + 1; });
  const idLinks = (ids) => (ids || []).map((id) => `<span class="idlink" data-id="${esc(id)}">${esc(id)}</span>`).join(", ");
  const list = (arr) => (arr || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const when = new Date(r.generated_at * 1000).toLocaleString();
  el.innerHTML = `
    <h3>Overall assessment</h3>
    <div class="muted small-text">${esc(r.mode === "revision" ? "Revision assessment" : "First-draft review")} · ${esc(r.depth)} · ${esc(r.backend)} · ${esc(when)}</div>
    <div class="kpis" style="margin-top:10px">
      ${kpi("Recommendation", rec, `confidence ${pct(o.recommendation_confidence)}`, o.recommendation_confidence)}
      ${kpi("Revision effort", o.effort_hours_high ? hours(o.effort_hours_low, o.effort_hours_high) : "—", label(o.effort_level || ""))}
      ${kpi("Effort confidence", pct(o.effort_confidence), "", o.effort_confidence)}
      ${rev.length ? kpi("Reviewer comments", String(rev.length), Object.entries(stances).map(([k, v]) => `${label(k)} ${v}`).join(" · ")) : ""}
      ${ai.length ? kpi("Independent findings", String(ai.length), ["critical", "major", "minor", "editorial"].map((s) => { const n = ai.filter((c) => c.severity === s).length; return n ? `${s} ${n}` : ""; }).filter(Boolean).join(" · ")) : ""}
    </div>
    ${o.effort_rationale ? `<section><h4>Effort</h4><p>${esc(o.effort_rationale)}</p>
      <p class="muted small-text">Bottom-up sums: ${eff.reviewer_comments ? `reviewer comments ${hours(eff.reviewer_comments.hours_low, eff.reviewer_comments.hours_high)} (conf. ${pct(eff.reviewer_comments.confidence)}); ` : ""}${eff.independent_major ? `critical+major findings ${hours(eff.independent_major.hours_low, eff.independent_major.hours_high)}; ` : ""}${eff.independent ? `all findings ${hours(eff.independent.hours_low, eff.independent.hours_high)}` : ""}</p></section>` : ""}
    ${o.summary ? `<section><h4>Summary</h4><p>${esc(o.summary)}</p></section>` : ""}
    ${(o.top_priorities || []).length ? `<section><h4>Top priorities</h4><ol>${o.top_priorities.map((t) => `<li><strong>${esc(t.title)}</strong> — ${esc(t.why)} ${idLinks(t.item_ids)}</li>`).join("")}</ol></section>` : ""}
    ${o.rebuttal_strategy ? `<section><h4>Rebuttal strategy</h4><p>${esc(o.rebuttal_strategy)}</p></section>` : ""}
    ${(o.key_weaknesses || []).length ? `<section><h4>Key weaknesses</h4><ol>${list(o.key_weaknesses)}</ol></section>` : ""}
    ${(o.strengths || []).length ? `<section><h4>Strengths</h4><ul>${list(o.strengths)}</ul></section>` : ""}
    ${r.cross_comment && r.cross_comment.conflicts && r.cross_comment.conflicts.length ? `<section><h4>Conflicting reviewer requests</h4><ul>${r.cross_comment.conflicts.map((g) => `<li>${idLinks(g.comment_ids)}: ${esc(g.description)} <em>${esc(g.suggested_resolution)}</em></li>`).join("")}</ul></section>` : ""}
    ${r.cross_comment && r.cross_comment.duplicates && r.cross_comment.duplicates.length ? `<section><h4>Raised by several reviewers</h4><ul>${r.cross_comment.duplicates.map((g) => `<li>${idLinks(g.comment_ids)}: ${esc(g.theme)}</li>`).join("")}</ul></section>` : ""}
    ${(r.cross_comment || {}).reviewers && r.cross_comment.reviewers.length ? `<section><h4>Who the reviewers are</h4>${r.cross_comment.reviewers.map((v) => `
      <div class="profile"><strong>${esc(v.reviewer)}</strong> <span class="muted">— ${esc(v.expertise_guess)}; tone: ${esc(v.tone)}</span>
        ${(v.main_concerns || []).length ? `<div class="small-text">Cares about: ${esc(v.main_concerns.join("; "))}</div>` : ""}
        <div class="small-text"><em>How to respond:</em> ${esc(v.how_to_respond)}</div></div>`).join("")}</section>` : ""}
    ${rev.some(needsAttention) ? `<section><h4>Comments to read twice</h4><ul>${rev.filter(needsAttention).map((c) => {
      const d = c.decoded || {};
      const why = [d.misunderstanding && d.misunderstanding !== "none" ? "may misread the paper" : "", d.clarity && d.clarity !== "clear" ? "unclear" : "", ["curt", "hostile"].includes(d.tone) ? d.tone : ""].filter(Boolean).join(", ");
      return `<li><span class="idlink" data-id="${esc(c.id)}">${esc(c.id)}</span> (${esc(why)}): ${esc(d.plain_language || c.comment_text.slice(0, 140))}</li>`;
    }).join("")}</ul></section>` : ""}
    ${(r.figures || []).length ? `<section><h4>Figures and their code</h4><div class="figure-grid">${r.figures.map(figureCard).join("")}</div></section>` : ""}
    ${o.likely_outcome ? `<section><h4>Likely outcome</h4><p>${esc(o.likely_outcome)}</p></section>` : ""}
    ${(r.warnings || []).length || (r.passes || []).some((x) => !x.ok) ? `<section><h4>Notes</h4><ul class="warn-list">${(r.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("")}${(r.passes || []).filter((x) => !x.ok).map((x) => `<li>Pass failed: ${esc(x.name)} — ${esc((x.error || "").slice(0, 160))}</li>`).join("")}</ul></section>` : ""}
    <p class="muted small-text">${(r.usage || {}).calls || 0} model calls · ${((r.elapsed_seconds || 0) / 60).toFixed(1)} min · ${r.n_raw_findings || 0} raw findings consolidated.</p>`;
  el.querySelectorAll(".idlink").forEach((a) => a.addEventListener("click", () => selectComment(a.dataset.id)));
}

function renderDetail() {
  const c = getSelectedComment();
  if (!c) { showOverview(); return; }
  $("overview").classList.add("hidden");
  $("detailContent").classList.remove("hidden");

  $("detailReviewer").textContent = c.reviewer;
  $("detailSeverity").className = `badge severity-${c.severity || "unclear"}`;
  $("detailSeverity").textContent = c.severity || "unclear";
  $("detailStatus").value = c.status || "unaddressed";
  $("detailId").textContent = c.id;
  const d = c.decoded || {};
  const calmVersion = (["curt", "hostile"].includes(d.tone) && d.neutral_rewrite) ? d.neutral_rewrite : null;
  const useCalm = state.calm && calmVersion && !state.showOriginal;
  $("detailText").textContent = useCalm ? calmVersion : c.comment_text;
  $("detailText").classList.toggle("calm", !!useCalm);
  $("toggleOriginalBtn").classList.toggle("hidden", !(state.calm && calmVersion));
  $("toggleOriginalBtn").textContent = useCalm ? "Show original wording" : "Show calm version";
  renderRefs(c);
  renderDecoded(c);
  renderFigureBlock(c);
  $("ventBlock").classList.toggle("hidden", (c.origin || "reviewer") !== "reviewer");
  $("ventResult").classList.add("hidden");
  $("detailNotes").value = c.author_notes || "";
  $("editableNote").classList.toggle("hidden", state.project.editable !== false);

  const cr = c.critique;
  const cb = $("critiqueBlock");
  if (cr) {
    const e = cr.effort || {};
    cb.innerHTML = `
      <h4>Critique</h4>
      <div class="row"><strong>Category:</strong> ${esc(label(cr.category))}${cr.location ? ` · <strong>Location:</strong> ${esc(cr.location)}` : ""}</div>
      ${cr.quote ? `<blockquote>“${esc(cr.quote)}”${cr.quote_verified === false ? ` <span class="unverified">(quote not found verbatim — check it)</span>` : ""}</blockquote>` : ""}
      ${cr.rationale ? `<div class="row"><strong>Why it matters:</strong> ${esc(cr.rationale)}</div>` : ""}
      ${cr.suggestion ? `<div class="row"><strong>Suggested fix:</strong> ${esc(cr.suggestion)}</div>` : ""}
      <div class="row"><strong>Effort:</strong> ${esc(label(e.level))} · ${esc(hours(e.hours_low || 0, e.hours_high || 0))} (confidence ${pct(e.confidence)})${e.needs_new_data ? " · <strong>needs new data</strong>" : ""}</div>
      <div class="row"><strong>Confidence it's a real problem:</strong> ${pct(cr.confidence)}${cr.support > 1 ? ` · raised by ${cr.support} referees (${esc((cr.personas || []).join(", "))})` : ` · ${esc((cr.personas || []).join(", "))}`}</div>`;
    cb.classList.remove("hidden");
  } else {
    cb.classList.add("hidden");
  }

  const t = c.triage;
  const tb = $("triageBlock");
  if (t && t.agreement) {
    const e = t.effort || {};
    tb.innerHTML = `
      <h4>Revision assessment</h4>
      <div class="row"><strong>Reviewer is:</strong> ${esc({ agree: "right", partially_agree: "partly right", disagree: "mistaken" }[t.agreement] || t.agreement)} (confidence ${pct(t.agreement_confidence)})</div>
      <div class="row"><strong>Recommended response:</strong>
        <select id="stanceSelect">
          ${["address", "address_partially", "clarify", "rebut"].map((s) => `<option value="${s}"${s === t.stance ? " selected" : ""}>${label(s)}</option>`).join("")}
        </select>${t.stance_set_by_author ? ' <span class="muted small-text">(set by you)</span>' : ""}</div>
      <div class="row"><strong>Effort:</strong> ${esc(label(e.level))} · ${esc(hours(e.hours_low || 0, e.hours_high || 0))} (confidence ${pct(e.confidence)})${e.needs_new_data ? " · <strong>needs new data</strong>" : ""}</div>
      <div class="row"><strong>Already addressed:</strong> ${esc(t.already_addressed)}</div>
      ${t.rationale ? `<div class="row"><strong>Rationale:</strong> ${esc(t.rationale)}</div>` : ""}
      ${t.evidence ? `<blockquote>${esc(t.evidence)}</blockquote>` : ""}
      ${(t.required_changes || []).length ? `<div class="row"><strong>Required changes:</strong><ul>${t.required_changes.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
      ${t.rebuttal ? `<div class="row"><strong>Draft rebuttal</strong> (chance the editor accepts it: ${pct(t.rebuttal_confidence)}):</div>
        <div class="rebuttal">${esc(t.rebuttal)}</div>
        <button id="useRebuttalBtn" class="small">Use as response</button>` : ""}`;
    tb.classList.remove("hidden");
    $("stanceSelect").addEventListener("change", (ev) => saveStance(ev.target.value));
    if ($("useRebuttalBtn")) $("useRebuttalBtn").addEventListener("click", () => {
      $("responseText").value = t.rebuttal;
      saveResponseText();
    });
  } else {
    tb.classList.add("hidden");
  }

  if (c.status_evidence || (c.suggested_action && !cr)) {
    $("detailGap").classList.remove("hidden");
    $("detailEvidence").textContent = c.status_evidence || "(none)";
    $("detailAction").textContent = c.suggested_action || "(none)";
  } else {
    $("detailGap").classList.add("hidden");
  }
  $("responseText").value = c.response_text || "";
  $("draftResponseBtn").classList.toggle("hidden", c.origin === "ai-review");
  $("responseBlock").classList.toggle("hidden", c.origin === "ai-review" && !c.response_text);
  renderPatchList(c);
  renderRevisionChecklist(c);
  renderResponseAudit(c);
  renderScientificDetail(c);
}

function renderRefs(c) {
  const el = $("refsBlock");
  const refs = c.resolved_refs || [];
  el.classList.toggle("hidden", !refs.length);
  el.innerHTML = refs.map((r) => `<div><strong>${esc(r.ref)}</strong> in the PDF they read${r.page ? ` (p. ${esc(r.page)})` : ""}: <q>${esc(r.text)}</q></div>`).join("");
}

const MISREAD = {
  missed_existing_text: "asks for something your manuscript already contains",
  misread: "misread what your manuscript says",
  terminology: "uses a term differently than you do",
  wrong_premise: "rests on an incorrect premise",
  different_field_norms: "applies conventions from another field",
};

function renderDecoded(c) {
  const el = $("decodedBlock");
  const d = c.decoded;
  if (!d || !d.plain_language) { el.classList.add("hidden"); return; }
  const mis = d.misunderstanding && d.misunderstanding !== "none";
  el.innerHTML = `
    <h4>What they actually want</h4>
    <div class="plain">${esc(d.plain_language)}</div>
    ${d.underlying_concern ? `<div class="row"><strong>Behind it:</strong> ${esc(d.underlying_concern)}</div>` : ""}
    ${(d.what_would_satisfy || []).length ? `<div class="row"><strong>What would satisfy them:</strong><ul>${d.what_would_satisfy.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    ${d.clarity !== "clear" && (d.interpretations || []).length ? `
      <div class="row"><strong>This comment is ${esc(label(d.clarity))}. Possible readings:</strong>
        ${d.interpretations.map((i) => `<div class="reading"><div class="meter small"><div style="width:${Math.round(i.likelihood * 100)}%"></div></div><span>${esc(i.reading)} <span class="muted">(${pct(i.likelihood)})</span></span></div>`).join("")}
      </div>
      ${d.clarification_request ? `<div class="row"><strong>Say how you read it:</strong> <q>${esc(d.clarification_request)}</q> <button class="small copyBtn" data-copy="${esc(d.clarification_request)}">Copy</button></div>` : ""}` : ""}
    ${mis ? `
      <div class="misread-box">
        <div><strong>Possible misreading:</strong> the reviewer ${esc(MISREAD[d.misunderstanding] || label(d.misunderstanding))}. ${esc(d.misunderstanding_explanation || "")}</div>
        ${d.manuscript_evidence ? `<blockquote>${esc(d.manuscript_evidence)}${d.evidence_location ? ` <span class="muted">— ${esc(d.evidence_location)}</span>` : ""}${d.evidence_verified === false ? ` <span class="unverified">(not found verbatim — check)</span>` : ""}</blockquote>` : ""}
        ${d.clarifying_edit ? `<div><strong>Clarify it so nobody misreads it again:</strong> ${esc(d.clarifying_edit)}</div>
          <button class="small" id="proposeClarifyBtn">Propose this clarification as an edit</button>` : ""}
      </div>` : ""}
    ${["curt", "hostile"].includes(d.tone) && d.neutral_rewrite && !state.calm ? `<div class="row muted"><strong>Calm version:</strong> ${esc(d.neutral_rewrite)}</div>` : ""}`;
  el.classList.remove("hidden");
  el.querySelectorAll(".copyBtn").forEach((b) => b.addEventListener("click", () => navigator.clipboard.writeText(b.dataset.copy)));
  if ($("proposeClarifyBtn")) $("proposeClarifyBtn").addEventListener("click", proposePatch);
}

function figuresFor(c) {
  const figs = ((state.project.state.review || {}).figures) || [];
  let nums = c.figure_refs || [];
  if (!nums.length) {
    const m = /^Figure (\d+)/.exec(c.location_ref || "") || /^Figure (\d+)/.exec((c.critique || {}).title || "");
    if (m) nums = [Number(m[1])];
  }
  return figs.filter((f) => nums.includes(f.number));
}

function figureCard(f) {
  const code = (f.code || []).map((x) => `${esc(x.file)}:${esc(x.line)}`).join(", ");
  return `<div class="figure-card">
    ${f.image ? `<img src="${P()}/figures/${f.number}" alt="Figure ${esc(f.number)}" loading="lazy" />` : `<div class="noimg">${esc(f.image_note || "no image")}</div>`}
    <div><strong>Figure ${esc(f.number)}.</strong> ${esc(f.caption || "(no caption)")}
      <div class="muted small-text">${code ? `Drawn by ${code}` : "No code found that draws it"}${(f.missing || []).length ? ` · missing file: ${esc(f.missing.join(", "))}` : ""}</div></div>
  </div>`;
}

function renderFigureBlock(c) {
  const el = $("figureBlock");
  const figs = figuresFor(c);
  el.classList.toggle("hidden", !figs.length);
  el.innerHTML = figs.length ? `<h4>Figure${figs.length > 1 ? "s" : ""}</h4>` + figs.map(figureCard).join("") : "";
}

async function ventToReply() {
  const c = getSelectedComment();
  const text = $("ventText").value.trim();
  if (!c || !text) return;
  await withBusy($("ventBtn"), "Rewriting…", async () => {
    try {
      const r = await api("POST", `${P()}/comments/${enc(c.id)}/vent`, { text });
      const box = $("ventResult");
      box.innerHTML = `
        <div class="rebuttal">${esc(r.response_text)}</div>
        <button class="small" id="ventUseBtn">Use as response</button>
        ${r.manuscript_change ? `<div class="row"><strong>Manuscript change to back it up:</strong> ${esc(r.manuscript_change)}</div>` : ""}
        ${r.notes_for_author ? `<div class="row muted"><strong>Note:</strong> ${esc(r.notes_for_author)}</div>` : ""}`;
      box.classList.remove("hidden");
      $("ventUseBtn").addEventListener("click", () => { $("responseText").value = r.response_text; saveResponseText(); });
    } catch (e) { alert(`Could not rewrite: ${e.message}`); }
  });
}

function renderPatchList(comment) {
  const container = $("patchList");
  const ids = comment.patch_ids || [];
  const patches = (state.project.state.patches || []).filter((p) => ids.includes(p.id));
  container.innerHTML = patches.slice().reverse().map((p) => `
    <div class="patch-card">
      <div class="patch-status ${esc(p.status)}">${esc(p.status)}${p.file ? ` · ${esc(p.file.split(/[\\/]/).pop())}` : ""}</div>
      <div class="patch-rationale">${esc(p.rationale || "")}</div>
      ${p.find ? `<pre class="patch-diff" data-patch="${esc(p.id)}">(loading diff…)</pre>` : `<em>No text edit proposed for this comment.</em>`}
      ${p.validated === false && p.status === "proposed" ? `<div class="validation-warning">⚠ ${esc(p.validation_message || "")}</div>` : ""}
      ${p.backup_path ? `<div class="muted small-text">Backup: ${esc(p.backup_path)}</div>` : ""}
      <div class="patch-actions">
        ${p.status === "proposed" && p.find ? `
          <button class="applyBtn" data-patch="${esc(p.id)}"${p.validated === false ? " disabled" : ""}>Apply</button>
          <button class="rejectBtn" data-patch="${esc(p.id)}">Reject</button>
          <button class="reviseBtn" data-patch="${esc(p.id)}">Ask for revision…</button>` : ""}
        ${p.status === "applied" ? `<button class="revertBtn" data-patch="${esc(p.id)}">Revert</button>` : ""}
      </div>
    </div>`).join("");
  container.querySelectorAll(".patch-diff").forEach((el) => loadDiff(el.dataset.patch, el));
  container.querySelectorAll(".applyBtn").forEach((b) => b.addEventListener("click", () => patchAction(b.dataset.patch, "apply")));
  container.querySelectorAll(".rejectBtn").forEach((b) => b.addEventListener("click", () => patchAction(b.dataset.patch, "reject")));
  container.querySelectorAll(".revertBtn").forEach((b) => b.addEventListener("click", () => patchAction(b.dataset.patch, "revert")));
  container.querySelectorAll(".reviseBtn").forEach((b) => b.addEventListener("click", () => revisePatchPrompt(b.dataset.patch)));
}

async function loadDiff(patchId, el) {
  try {
    const res = await api("GET", `${P()}/patches/${enc(patchId)}/diff`);
    el.innerHTML = res.diff.split("\n").map((ln) => {
      const cls = ln.startsWith("+++") || ln.startsWith("---") || ln.startsWith("@@") ? "diff-meta"
        : ln.startsWith("+") ? "diff-add" : ln.startsWith("-") ? "diff-del" : "";
      return cls ? `<span class="${cls}">${esc(ln)}</span>` : esc(ln) + "\n";
    }).join("");
  } catch (e) {
    el.textContent = `(could not load diff: ${e.message})`;
  }
}

async function deleteComment(id) {
  if (!confirm("Remove this item? (e.g. boilerplate, or a criticism you've decided is wrong)")) return;
  try {
    await api("DELETE", `${P()}/comments/${enc(id)}`);
  } catch (e) { alert(e.message); }
  if (state.selectedCommentId === id) state.selectedCommentId = null;
  await refreshProject();
  renderAll();
}

async function updateComment(fields) {
  const c = getSelectedComment();
  if (!c) return;
  try {
    await api("POST", `${P()}/comments/${enc(c.id)}`, fields);
    await refreshProject();
    renderComments();
    renderDetail();
  } catch (e) { alert(`Could not save: ${e.message}`); }
}
const saveNotes = () => updateComment({ author_notes: $("detailNotes").value });
const saveStatus = () => updateComment({ status: $("detailStatus").value });
const saveStance = (stance) => updateComment({ stance });
const saveResponseText = () => updateComment({ response_text: $("responseText").value });

async function withBusy(btn, busyText, fn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = busyText;
  try { await fn(); } finally { btn.disabled = false; btn.textContent = old; }
}

async function proposePatch() {
  const c = getSelectedComment();
  if (!c) return;
  await withBusy($("proposePatchBtn"), "Thinking…", async () => {
    try {
      await api("POST", `${P()}/comments/${enc(c.id)}/propose-patch`, {});
      await refreshProject();
      renderDetail();
    } catch (e) { alert(`Could not propose an edit: ${e.message}`); }
  });
}

async function patchAction(patchId, action) {
  const prompts = {
    apply: "Apply this edit to the actual manuscript file? A backup is made first.",
    revert: "Undo this edit in the manuscript file? A backup is made first.",
  };
  if (prompts[action] && !confirm(prompts[action])) return;
  try {
    await api("POST", `${P()}/patches/${enc(patchId)}/${action}`, {});
  } catch (e) { alert(`Could not ${action} patch: ${e.message}`); }
  await refreshProject();
  renderDetail();
}

async function revisePatchPrompt(patchId) {
  const feedback = prompt("What should change about this proposed edit?");
  if (!feedback) return;
  try {
    await api("POST", `${P()}/patches/${enc(patchId)}/revise`, { feedback });
    await refreshProject();
    renderDetail();
  } catch (e) { alert(`Could not revise patch: ${e.message}`); }
}

async function draftResponse() {
  const c = getSelectedComment();
  if (!c) return;
  await withBusy($("draftResponseBtn"), "Drafting…", async () => {
    try {
      await api("POST", `${P()}/comments/${enc(c.id)}/draft-response`, {});
      await refreshProject();
      renderDetail();
    } catch (e) { alert(`Could not draft a response: ${e.message}`); }
  });
}

async function exportLetter() {
  try {
    const res = await api("GET", `${P()}/response-letter`);
    $("letterText").value = res.markdown;
    $("letterModal").classList.remove("hidden");
  } catch (e) { alert(e.message); }
}

// -------------------------------------------------------------------- jobs --

async function startJob(path, body) {
  try {
    const res = await api("POST", path, body || {});
    if (res.job) trackJob(res.job);
  } catch (e) { alert(e.message); }
}

function trackJob(job) {
  stopJobPolling();
  state.job = { id: job.id, since: 0 };
  $("jobBar").classList.remove("hidden", "error");
  $("jobTitle").textContent = job.title || "Working…";
  $("jobLog").textContent = "";
  $("jobCancelBtn").classList.remove("hidden");
  $("jobCloseBtn").classList.add("hidden");
  $("runReviewBtn").disabled = true;
  $("gapAnalysisBtn").disabled = true;
  pollJob();
}

function stopJobPolling() {
  if (state.jobTimer) clearTimeout(state.jobTimer);
  state.jobTimer = null;
}

async function pollJob() {
  if (!state.job) return;
  let j;
  try {
    j = await api("GET", `/api/jobs/${enc(state.job.id)}?since=${state.job.since}`);
  } catch (e) {
    $("jobLast").textContent = e.message;
    finishJobUi(true);
    return;
  }
  state.job.since = j.log_len;
  const log = $("jobLog");
  for (const entry of j.log) log.textContent += entry.msg + "\n";
  log.scrollTop = log.scrollHeight;
  if (j.log.length) $("jobLast").textContent = j.log[j.log.length - 1].msg;
  const fill = $("jobProgress");
  if (j.progress === null || j.progress === undefined) {
    fill.classList.add("indeterminate");
  } else {
    fill.classList.remove("indeterminate");
    fill.style.width = `${Math.round(j.progress * 100)}%`;
  }
  if (["done", "error", "cancelled"].includes(j.status)) {
    $("jobTitle").textContent = `${j.title}: ${j.status}`;
    if (j.status === "error") $("jobLast").textContent = j.error || "failed";
    finishJobUi(j.status === "error");
    if (state.slug === j.slug) {
      await refreshProject();
      if (j.kind === "review" && j.status === "done") state.selectedCommentId = null;
      renderAll();
    }
    return;
  }
  state.jobTimer = setTimeout(pollJob, 1500);
}

function finishJobUi(isError) {
  stopJobPolling();
  $("jobBar").classList.toggle("error", !!isError);
  $("jobCancelBtn").classList.add("hidden");
  $("jobCloseBtn").classList.remove("hidden");
  $("runReviewBtn").disabled = false;
  $("gapAnalysisBtn").disabled = false;
  state.job = null;
}

// ------------------------------------------------------------------ chat ---

function renderChatMessages(thread) {
  const container = $("chatMessages");
  container.innerHTML = "";
  for (const m of thread.messages || []) {
    const div = document.createElement("div");
    div.className = `chat-msg ${m.role === "user" ? "user" : "assistant"}`;
    div.textContent = m.content;
    container.appendChild(div);
  }
  container.scrollTop = container.scrollHeight;
}

async function loadChat() {
  const thread = await api("GET", `${P()}/chat/${enc(state.chatThreadId)}`);
  renderChatMessages(thread);
  $("chatTitle").textContent = thread.comment_id ? `Discussing ${thread.comment_id}` : "Co-author chat";
  $("unfocusChatBtn").classList.toggle("hidden", !thread.comment_id);
  return thread;
}

async function sendChatMessage(text) {
  const input = $("chatInput");
  const message = (text || input.value).trim();
  if (!message) return;
  input.value = "";
  input.disabled = true;
  $("chatSendBtn").disabled = true;
  const container = $("chatMessages");
  const userDiv = document.createElement("div");
  userDiv.className = "chat-msg user";
  userDiv.textContent = message;
  container.appendChild(userDiv);
  const thinking = document.createElement("div");
  thinking.className = "chat-msg assistant";
  thinking.textContent = "…thinking…";
  container.appendChild(thinking);
  container.scrollTop = container.scrollHeight;
  try {
    const res = await api("POST", `${P()}/chat/${enc(state.chatThreadId)}`, { message });
    thinking.textContent = res.reply;
  } catch (e) {
    thinking.textContent = `[error: ${e.message}]`;
  } finally {
    input.disabled = false;
    $("chatSendBtn").disabled = false;
    input.focus();
  }
}

async function focusChatOnComment() {
  const c = getSelectedComment();
  if (!c) return;
  state.chatThreadId = `comment-${c.id}`.replace(/[^A-Za-z0-9._:-]/g, "-").slice(0, 160);
  await api("POST", `${P()}/chat/${enc(state.chatThreadId)}`, { message: "", comment_id: c.id });
  const thread = await loadChat();
  if (!(thread.messages || []).length) {
    await sendChatMessage(`Let's discuss this ${c.origin === "ai-review" ? "criticism" : "reviewer comment"}: is it valid, and what's the best way to handle it?`);
  }
}

async function unfocusChat() {
  state.chatThreadId = "project-main";
  await loadChat();
}

// ---------------------------------------------------------- project modal --

function parseCommentPaths(raw) {
  return raw.split("\n").map((l) => l.trim()).filter(Boolean).map((line) => {
    const idx = line.indexOf(":");
    // Windows paths have a colon after the drive letter (C:\...): treat as
    // "label: path" only if the colon comes before the path itself starts.
    const sepIdx = Math.min(...["\\", "/"].map((s) => line.indexOf(s)).filter((i) => i >= 0), Infinity);
    if (idx > 0 && idx < sepIdx - 2) {
      return { label: line.slice(0, idx).trim(), path: line.slice(idx + 1).trim() };
    }
    return { label: line.split(/[\\/]/).pop(), path: line };
  });
}

function openProjectModal(mode) {
  state.modalMode = mode;
  const edit = mode === "edit";
  const cfg = edit ? state.project.config : null;
  $("pmTitle").textContent = edit ? "Project settings" : "New project";
  $("pmSaveBtn").textContent = edit ? "Save" : "Create project";
  $("pmDeleteBtn").classList.toggle("hidden", !edit);
  $("pmStartWrap").classList.toggle("hidden", edit);
  $("pmName").value = edit ? cfg.name : "";
  $("pmTitleInput").value = edit ? (cfg.title || "") : "";
  $("pmJournal").value = edit ? (cfg.journal || "") : "";
  $("pmField").value = edit ? (cfg.field || "") : "";
  $("pmManuscript").value = edit ? ((cfg.manuscript_files[0] || {}).path || "") : "";
  $("pmComments").value = edit ? cfg.comment_sources.map((s) => `${s.label}: ${s.path}`).join("\n") : "";
  $("pmCode").value = edit ? (cfg.code_paths || []).join("\n") : "";
  $("pmData").value = edit ? (cfg.data_paths || []).join("\n") : "";
  $("pmStandards").value = edit ? (cfg.standards_paths || []).join("\n") : "";
  $("pmGapPlan").value = edit ? (cfg.gap_plan_file || "") : "";
  $("pmSubmitted").value = edit ? (cfg.submitted_pdf || "") : "";
  $("pmFigures").value = edit ? (cfg.figures_dir || "") : "";
  $("pmGapPlan").disabled = edit;
  const llm = edit ? (cfg.llm || {}) : {};
  fillBackendSelect($("pmBackend"), llm.backend || "", true);
  $("pmModel").value = llm.model || "";
  fillModelList($("pmModelList"), llm.backend || state.defaultSettings.backend);
  $("pmThink").value = llm.think === true ? "on" : llm.think === false ? "off" : "";
  $("pmDepth").value = edit ? cfg.review.depth : "thorough";
  $("pmIndependent").checked = edit ? cfg.review.independent_review !== false : true;
  $("pmStart").checked = true;
  $("pmStatus").textContent = "";
  updateBackendNote();
  $("projectModal").classList.remove("hidden");
}

function updateBackendNote() {
  const id = $("pmBackend").value || state.defaultSettings.backend;
  const b = state.backends.find((x) => x.id === id);
  fillModelList($("pmModelList"), id);
  $("pmBackendNote").textContent = b
    ? `${b.label}: ${b.detail}.` + (b.kind === "local" ? " Runs entirely on this machine." : " The manuscript text will be sent to this cloud provider.")
    : "";
}

async function saveProjectModal() {
  const name = $("pmName").value.trim();
  const manuscript = $("pmManuscript").value.trim().replace(/^"|"$/g, "");
  if (!name) { $("pmStatus").textContent = "Project name is required."; return; }
  if (!manuscript) { $("pmStatus").textContent = "A manuscript file is required."; return; }
  const think = { on: true, off: false }[$("pmThink").value];
  const backend = $("pmBackend").value;
  const body = {
    name,
    title: $("pmTitleInput").value.trim(),
    journal: $("pmJournal").value.trim(),
    field: $("pmField").value.trim(),
    manuscript_path: manuscript,
    comment_sources: parseCommentPaths($("pmComments").value),
    code_paths: $("pmCode").value,
    data_paths: $("pmData").value,
    standards_paths: $("pmStandards").value,
    submitted_pdf: $("pmSubmitted").value.trim() || null,
    figures_dir: $("pmFigures").value.trim() || null,
    llm: backend ? { backend, model: $("pmModel").value.trim() || null, think: think === undefined ? null : think } : null,
    review: { depth: $("pmDepth").value, independent_review: $("pmIndependent").checked },
  };
  $("pmSaveBtn").disabled = true;
  $("pmStatus").textContent = state.modalMode === "edit" ? "Saving…" : "Creating project…";
  try {
    if (state.modalMode === "edit") {
      const res = await api("POST", `${P()}/settings`, body);
      $("projectModal").classList.add("hidden");
      await loadProjectsList();
      await refreshProject();
      renderAll();
      refreshBackends();
      if (res.job) trackJob(res.job);
    } else {
      body.gap_plan_file = $("pmGapPlan").value.trim() || null;
      body.start_review = $("pmStart").checked;
      const summary = await api("POST", "/api/projects", body);
      if (summary.warnings && summary.warnings.length) alert("Project created, with warnings:\n\n" + summary.warnings.join("\n"));
      $("projectModal").classList.add("hidden");
      await loadProjectsList();
      $("projectSelect").value = summary.slug;
      await selectProject(summary.slug);
      if (summary.job) trackJob(summary.job);
    }
  } catch (e) {
    $("pmStatus").textContent = `Error: ${e.message}`;
  } finally {
    $("pmSaveBtn").disabled = false;
  }
}

async function deleteProject() {
  if (!confirm(`Delete project "${state.project.config.name}"? This removes papr-thinkr's workspace (items, chat, backups, reports) but never your manuscript or data files.`)) return;
  await api("DELETE", P());
  $("projectModal").classList.add("hidden");
  state.slug = null;
  try { localStorage.removeItem("papr.slug"); } catch (e) { /* ignore */ }
  await loadProjectsList();
  await selectProject(null);
}

// ------------------------------------------------------------------ browse -

async function openBrowse(targetInputId, dirs) {
  state.browseTarget = targetInputId;
  state.browseDirs = !!dirs;
  $("browseUseDirBtn").classList.toggle("hidden", !dirs);
  $("browseModal").classList.remove("hidden");
  let start = "";
  try { start = localStorage.getItem("papr.browse") || ""; } catch (e) { /* ignore */ }
  try { await navigateBrowse(start); } catch (e) { await navigateBrowse(""); }
}

async function navigateBrowse(path) {
  const res = await api("GET", `/api/browse?path=${enc(path)}`);
  state.browseCwd = res.cwd;
  state.browseHome = res.home;
  if (res.cwd) { try { localStorage.setItem("papr.browse", res.cwd); } catch (e) { /* ignore */ } }
  $("browsePath").textContent = res.cwd || "(drives)";
  $("browseUseDirBtn").disabled = !res.cwd;
  const list = $("browseList");
  list.innerHTML = "";
  if (res.cwd) {
    const up = document.createElement("div");
    up.className = "browse-item dir";
    up.textContent = ".. (up)";
    up.addEventListener("click", () => navigateBrowse(res.parent || ""));
    list.appendChild(up);
  }
  for (const entry of res.entries) {
    const div = document.createElement("div");
    div.className = "browse-item " + (entry.is_dir ? "dir" : "file");
    div.textContent = entry.name;
    div.addEventListener("click", () => entry.is_dir ? navigateBrowse(entry.path) : selectBrowsed(entry.path));
    list.appendChild(div);
  }
}

function selectBrowsed(path) {
  if (state.browseTarget === "__discover__") {
    $("browseModal").classList.add("hidden");
    fillFromFolder(path);
    return;
  }
  const target = $(state.browseTarget);
  if (target.tagName === "TEXTAREA") {
    const line = state.browseTarget === "pmComments" ? `${path.split(/[\\/]/).pop()}: ${path}` : path;
    target.value = target.value.trim() ? target.value.trim() + "\n" + line : line;
  } else {
    target.value = path;
  }
  $("browseModal").classList.add("hidden");
}

async function fillFromFolder(folder) {
  $("pmStatus").textContent = "Looking through the folder…";
  try {
    const f = await api("GET", `/api/discover?path=${enc(folder)}`);
    if (f.manuscript) $("pmManuscript").value = f.manuscript;
    if (f.reviews.length) $("pmComments").value = f.reviews.map((r) => `${r.split(/[\\/]/).pop()}: ${r}`).join("\n");
    if (f.code_paths.length) $("pmCode").value = f.code_paths.join("\n");
    if (f.data_paths.length) $("pmData").value = f.data_paths.join("\n");
    if (f.submitted_pdf) $("pmSubmitted").value = f.submitted_pdf;
    if (f.figures_dir) $("pmFigures").value = f.figures_dir;
    if (!$("pmName").value.trim()) $("pmName").value = folder.split(/[\\/]/).filter(Boolean).pop();
    const s = f.summary;
    $("pmStatus").textContent = `Found: ${f.manuscript ? "manuscript" : "NO manuscript"}, ${f.reviews.length} review file(s), ` +
      `${f.submitted_pdf ? "a line-numbered PDF" : "no line-numbered PDF"}, code (${Object.entries(s.code_languages).map(([k, v]) => `${v} ${k}`).join(", ") || "none"}), ` +
      `data (${Object.entries(s.data_formats).map(([k, v]) => `${v} ${k}`).join(", ") || "none"}). ` +
      (f.notes.length ? "\n" + f.notes.join("\n") : "") + "\nCheck the fields, then create the project.";
  } catch (e) {
    $("pmStatus").textContent = `Could not read the folder: ${e.message}`;
  }
}

// -------------------------------------------------------------------- init -

function wireEvents() {
  $("projectSelect").addEventListener("change", (e) => selectProject(e.target.value));
  $("newProjectBtn").addEventListener("click", () => openProjectModal("create"));
  $("editProjectBtn").addEventListener("click", () => openProjectModal("edit"));
  $("pmCancelBtn").addEventListener("click", () => $("projectModal").classList.add("hidden"));
  $("pmSaveBtn").addEventListener("click", saveProjectModal);
  $("pmDeleteBtn").addEventListener("click", deleteProject);
  $("pmBackend").addEventListener("change", updateBackendNote);

  document.querySelectorAll(".browseBtn").forEach((b) =>
    b.addEventListener("click", () => openBrowse(b.dataset.browseTarget, b.dataset.browseDirs === "1")));
  $("browseCancelBtn").addEventListener("click", () => $("browseModal").classList.add("hidden"));
  $("browseUseDirBtn").addEventListener("click", () => state.browseCwd && selectBrowsed(state.browseCwd));
  $("browseHomeBtn").addEventListener("click", () => navigateBrowse(state.browseHome || ""));

  $("backendPill").addEventListener("click", async () => {
    await refreshBackends(true);
    renderBackendsModal();
    $("backendsModal").classList.remove("hidden");
  });
  $("backendsCloseBtn").addEventListener("click", () => $("backendsModal").classList.add("hidden"));
  $("refreshBackendsBtn").addEventListener("click", async () => { await refreshBackends(true); renderBackendsModal(); });
  $("defaultBackend").addEventListener("change", (e) => fillModelList($("defaultModelList"), e.target.value));
  $("saveDefaultBackendBtn").addEventListener("click", async () => {
    try {
      await api("POST", "/api/settings", { backend: $("defaultBackend").value, model: $("defaultModel").value.trim() || null });
      await refreshBackends(true);
      renderBackendsModal();
    } catch (e) { alert(e.message); }
  });

  $("runReviewBtn").addEventListener("click", () => {
    const p = state.project;
    const n = p.summary.n_reviewer_comments;
    const what = n ? `triage ${n} reviewer comment(s)${p.config.review.independent_review ? " and run an independent critique" : ""}`
      : "critique this draft";
    if (confirm(`Run a ${p.config.review.depth} review to ${what}? This can take from a few minutes (cloud) to an hour (large local model). You can keep working meanwhile.`)) {
      startJob(`${P()}/review`, { force: $("freshReviewToggle").checked, llm: reviewModelOptions() });
    }
  });
  $("gapAnalysisBtn").addEventListener("click", () => startJob(`${P()}/gap-analysis`, { force: $("freshReviewToggle").checked }));
  $("overviewBtn").addEventListener("click", showOverview);
  $("exportLetterBtn").addEventListener("click", exportLetter);
  $("letterCloseBtn").addEventListener("click", () => $("letterModal").classList.add("hidden"));
  $("letterCopyBtn").addEventListener("click", async () => {
    await navigator.clipboard.writeText($("letterText").value);
    $("letterCopyBtn").textContent = "Copied!";
    setTimeout(() => { $("letterCopyBtn").textContent = "Copy to clipboard"; }, 1500);
  });

  $("jobCancelBtn").addEventListener("click", async () => {
    if (state.job) await api("POST", `/api/jobs/${enc(state.job.id)}/cancel`, {}).catch(() => {});
  });
  $("jobCloseBtn").addEventListener("click", () => $("jobBar").classList.add("hidden"));
  $("jobLogToggle").addEventListener("click", () => $("jobLog").classList.toggle("hidden"));

  $("filterSelect").addEventListener("change", (e) => { state.filter = e.target.value; renderComments(); });
  $("calmToggle").addEventListener("change", (e) => {
    state.calm = e.target.checked;
    state.showOriginal = false;
    try { localStorage.setItem("papr.calm", state.calm ? "1" : ""); } catch (err) { /* ignore */ }
    renderComments();
    if (getSelectedComment()) renderDetail();
  });
  $("toggleOriginalBtn").addEventListener("click", () => { state.showOriginal = !state.showOriginal; renderDetail(); });
  $("ventBtn").addEventListener("click", ventToReply);
  $("pmDiscoverBtn").addEventListener("click", () => openBrowse("__discover__", true));
  $("sortSelect").addEventListener("change", (e) => { state.sort = e.target.value; renderComments(); });
  $("searchInput").addEventListener("input", (e) => { state.search = e.target.value; renderComments(); });

  $("detailStatus").addEventListener("change", saveStatus);
  $("saveNotesBtn").addEventListener("click", saveNotes);
  $("proposePatchBtn").addEventListener("click", proposePatch);
  $("draftResponseBtn").addEventListener("click", draftResponse);
  $("saveResponseBtn").addEventListener("click", saveResponseText);
  $("focusChatBtn").addEventListener("click", focusChatOnComment);
  $("unfocusChatBtn").addEventListener("click", unfocusChat);

  $("chatSendBtn").addEventListener("click", () => sendChatMessage());
  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChatMessage();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".modal").forEach((m) => m.classList.add("hidden"));
  });
}

async function init() {
  wireEvents();
  wireWorkflowEvents();
  try { state.calm = !!localStorage.getItem("papr.calm"); } catch (e) { /* ignore */ }
  $("calmToggle").checked = state.calm;
  await refreshBackends();
  setInterval(() => refreshBackends(), 60000);
  await loadProjectsList();
  let last = new URLSearchParams(location.search).get("project");
  if (!last) { try { last = localStorage.getItem("papr.slug"); } catch (e) { /* ignore */ } }
  if (last && [...$("projectSelect").options].some((o) => o.value === last)) {
    $("projectSelect").value = last;
    await selectProject(last);
    const item = new URLSearchParams(location.search).get("item");
    if (item && getSelectedComment() === null && state.project.state.comments.some((c) => c.id === item)) selectComment(item);
  }
}

init();
