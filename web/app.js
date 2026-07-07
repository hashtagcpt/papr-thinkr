// papr-thinkr frontend -- vanilla JS, no build step.

const state = {
  slug: null,
  project: null,       // {config, state}
  selectedCommentId: null,
  chatThreadId: "project-main",
  browseTarget: null,  // input id currently being filled by the browse modal
};

const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    const msg = (data && data.error) ? data.error : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

function severityBadge(sev) {
  return `<span class="badge severity-${sev || 'unclear'}">${sev || 'unclear'}</span>`;
}
function statusBadge(status) {
  const cls = status === 'addressed' ? 'status-addressed' : '';
  return `<span class="badge ${cls}">${(status || 'unaddressed').replace('_', ' ')}</span>`;
}

// ---------------------------------------------------------------- health ---

async function refreshHealth() {
  const el = $("ollamaStatus");
  try {
    const h = await api("GET", "/api/health");
    if (h.ollama_available) {
      el.textContent = `Ollama ✓ (${h.default_model})`;
      el.className = "status-pill ok";
      if (!h.models.includes(h.default_model)) {
        el.textContent += " — model not pulled!";
        el.className = "status-pill bad";
      }
    } else {
      el.textContent = "Ollama unreachable";
      el.className = "status-pill bad";
    }
  } catch (e) {
    el.textContent = "server error";
    el.className = "status-pill bad";
  }
}

// -------------------------------------------------------------- projects ---

async function loadProjectsList() {
  const projects = await api("GET", "/api/projects");
  const sel = $("projectSelect");
  sel.innerHTML = '<option value="">Select a project…</option>';
  for (const p of projects) {
    const opt = document.createElement("option");
    opt.value = p.slug;
    opt.textContent = p.name;
    sel.appendChild(opt);
  }
  if (state.slug) sel.value = state.slug;
}

async function selectProject(slug) {
  state.slug = slug;
  state.selectedCommentId = null;
  state.chatThreadId = "project-main";
  if (!slug) {
    $("emptyState").classList.remove("hidden");
    $("projectView").classList.add("hidden");
    return;
  }
  const data = await api("GET", `/api/projects/${slug}`);
  state.project = data;
  $("emptyState").classList.add("hidden");
  $("projectView").classList.remove("hidden");
  renderComments();
  clearDetail();
  await loadChat();
}

// -------------------------------------------------------------- comments ---

function renderComments() {
  const list = $("commentsList");
  list.innerHTML = "";
  const comments = (state.project && state.project.state.comments) || [];
  if (comments.length === 0) {
    list.innerHTML = '<div class="empty-state small">No comments extracted yet.</div>';
    return;
  }
  for (const c of comments) {
    const div = document.createElement("div");
    div.className = "comment-card" + (c.id === state.selectedCommentId ? " selected" : "");
    div.dataset.id = c.id;
    div.innerHTML = `
      <div class="meta">
        <strong>${c.reviewer}</strong>
        ${severityBadge(c.severity)}
        ${statusBadge(c.status)}
        <button class="deleteCommentBtn" title="Not a real reviewer comment (e.g. boilerplate) — remove it">✕</button>
      </div>
      <div class="snippet">${escapeHtml(c.comment_text)}</div>
    `;
    div.addEventListener("click", () => selectComment(c.id));
    div.querySelector(".deleteCommentBtn").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteComment(c.id);
    });
    list.appendChild(div);
  }
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (m) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[m]));
}

function getSelectedComment() {
  if (!state.project) return null;
  return state.project.state.comments.find((c) => c.id === state.selectedCommentId) || null;
}

function clearDetail() {
  state.selectedCommentId = null;
  $("detailEmpty").classList.remove("hidden");
  $("detailContent").classList.add("hidden");
}

async function selectComment(id) {
  state.selectedCommentId = id;
  renderComments();
  renderDetail();
}

function renderDetail() {
  const c = getSelectedComment();
  if (!c) { clearDetail(); return; }
  $("detailEmpty").classList.add("hidden");
  $("detailContent").classList.remove("hidden");

  $("detailReviewer").textContent = c.reviewer;
  $("detailSeverity").className = `badge severity-${c.severity || 'unclear'}`;
  $("detailSeverity").textContent = c.severity || "unclear";
  $("detailStatus").value = c.status || "unaddressed";
  $("detailText").textContent = c.comment_text;
  $("detailNotes").value = c.author_notes || "";

  if (c.status_evidence || c.suggested_action) {
    $("detailGap").classList.remove("hidden");
    $("detailEvidence").textContent = c.status_evidence || "(none)";
    $("detailAction").textContent = c.suggested_action || "(none)";
  } else {
    $("detailGap").classList.add("hidden");
  }

  $("responseBlock").classList.toggle("hidden", !c.response_text);
  $("responseText").value = c.response_text || "";

  renderPatchList(c);
}

function renderPatchList(comment) {
  const container = $("patchList");
  container.innerHTML = "";
  const patches = (state.project.state.patches || []).filter((p) => comment.patch_ids.includes(p.id));
  for (const p of patches.slice().reverse()) {
    const card = document.createElement("div");
    card.className = "patch-card";
    const statusClass = p.status === "applied" ? "applied" : (p.status === "rejected" ? "rejected" : "");
    card.innerHTML = `
      <div class="patch-status ${statusClass}">${p.status}</div>
      <div class="patch-rationale">${escapeHtml(p.rationale || "")}</div>
      ${p.find ? `<pre class="patch-diff" data-patch="${p.id}">(loading diff…)</pre>` : `<em>No text edit proposed for this comment.</em>`}
      ${p.validated === false ? `<div class="validation-warning">⚠ ${escapeHtml(p.validation_message || "")}</div>` : ""}
      <div class="patch-actions">
        ${p.status === "proposed" && p.find ? `
          <button class="applyBtn" data-patch="${p.id}">Apply</button>
          <button class="rejectBtn" data-patch="${p.id}">Reject</button>
          <button class="reviseBtn" data-patch="${p.id}">Ask for revision…</button>
        ` : ""}
      </div>
    `;
    container.appendChild(card);
    if (p.find) loadDiff(p.id, card.querySelector(".patch-diff"));
  }

  container.querySelectorAll(".applyBtn").forEach((b) => b.addEventListener("click", () => applyPatch(b.dataset.patch)));
  container.querySelectorAll(".rejectBtn").forEach((b) => b.addEventListener("click", () => rejectPatch(b.dataset.patch)));
  container.querySelectorAll(".reviseBtn").forEach((b) => b.addEventListener("click", () => revisePatchPrompt(b.dataset.patch)));
}

async function loadDiff(patchId, el) {
  try {
    const res = await api("GET", `/api/projects/${state.slug}/patches/${patchId}/diff`);
    el.textContent = res.diff;
  } catch (e) {
    el.textContent = `(could not load diff: ${e.message})`;
  }
}

async function deleteComment(id) {
  if (!confirm("Remove this item from the comment list? (e.g. because it's boilerplate, not an actual reviewer point)")) return;
  await api("DELETE", `/api/projects/${state.slug}/comments/${id}`);
  if (state.selectedCommentId === id) clearDetail();
  await refreshProject();
  renderComments();
}

async function refreshProject() {
  state.project = await api("GET", `/api/projects/${state.slug}`);
}

async function saveNotes() {
  const c = getSelectedComment();
  if (!c) return;
  await api("POST", `/api/projects/${state.slug}/comments/${c.id}`, { author_notes: $("detailNotes").value });
  await refreshProject();
  renderComments();
}

async function saveStatus() {
  const c = getSelectedComment();
  if (!c) return;
  await api("POST", `/api/projects/${state.slug}/comments/${c.id}`, { status: $("detailStatus").value });
  await refreshProject();
  renderComments();
  renderDetail();
}

async function proposePatch() {
  const c = getSelectedComment();
  if (!c) return;
  $("proposePatchBtn").disabled = true;
  $("proposePatchBtn").textContent = "Thinking…";
  try {
    await api("POST", `/api/projects/${state.slug}/comments/${c.id}/propose-patch`, {});
    await refreshProject();
    renderDetail();
  } catch (e) {
    alert(`Could not propose an edit: ${e.message}`);
  } finally {
    $("proposePatchBtn").disabled = false;
    $("proposePatchBtn").textContent = "Propose manuscript edit";
  }
}

async function applyPatch(patchId) {
  if (!confirm("Apply this edit to the actual manuscript file? A backup will be kept.")) return;
  try {
    await api("POST", `/api/projects/${state.slug}/patches/${patchId}/apply`, {});
    await refreshProject();
    renderDetail();
  } catch (e) {
    alert(`Could not apply patch: ${e.message}`);
  }
}

async function rejectPatch(patchId) {
  await api("POST", `/api/projects/${state.slug}/patches/${patchId}/reject`, {});
  await refreshProject();
  renderDetail();
}

async function revisePatchPrompt(patchId) {
  const feedback = prompt("What should change about this proposed edit?");
  if (!feedback) return;
  try {
    await api("POST", `/api/projects/${state.slug}/patches/${patchId}/revise`, { feedback });
    await refreshProject();
    renderDetail();
  } catch (e) {
    alert(`Could not revise patch: ${e.message}`);
  }
}

async function draftResponse() {
  const c = getSelectedComment();
  if (!c) return;
  $("draftResponseBtn").disabled = true;
  try {
    await api("POST", `/api/projects/${state.slug}/comments/${c.id}/draft-response`, {});
    await refreshProject();
    renderDetail();
  } catch (e) {
    alert(`Could not draft a response: ${e.message}`);
  } finally {
    $("draftResponseBtn").disabled = false;
  }
}

async function saveResponseText() {
  const c = getSelectedComment();
  if (!c) return;
  await api("POST", `/api/projects/${state.slug}/comments/${c.id}`, { response_text: $("responseText").value });
  await refreshProject();
}

async function runGapAnalysis() {
  $("gapAnalysisBtn").disabled = true;
  $("gapAnalysisBtn").textContent = "Analyzing…";
  try {
    await api("POST", `/api/projects/${state.slug}/gap-analysis`, {});
    await refreshProject();
    renderComments();
    renderDetail();
  } catch (e) {
    alert(`Gap analysis failed: ${e.message}`);
  } finally {
    $("gapAnalysisBtn").disabled = false;
    $("gapAnalysisBtn").textContent = "Run gap analysis";
  }
}

async function exportLetter() {
  const res = await api("GET", `/api/projects/${state.slug}/response-letter`);
  $("letterText").value = res.markdown;
  $("letterModal").classList.remove("hidden");
}

// ------------------------------------------------------------------ chat ---

function renderChatMessages(thread) {
  const container = $("chatMessages");
  container.innerHTML = "";
  for (const m of thread.messages || []) {
    const div = document.createElement("div");
    div.className = `chat-msg ${m.role}`;
    div.textContent = m.content;
    container.appendChild(div);
  }
  container.scrollTop = container.scrollHeight;
}

async function loadChat() {
  const thread = await api("GET", `/api/projects/${state.slug}/chat/${state.chatThreadId}`);
  renderChatMessages(thread);
  $("chatTitle").textContent = thread.comment_id ? `Discussing: ${thread.comment_id}` : "Co-author chat (whole project)";
  $("unfocusChatBtn").classList.toggle("hidden", !thread.comment_id);
}

async function sendChatMessage() {
  const input = $("chatInput");
  const message = input.value.trim();
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
    const res = await api("POST", `/api/projects/${state.slug}/chat/${state.chatThreadId}`, { message });
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
  state.chatThreadId = `comment-${c.id}`;
  await api("POST", `/api/projects/${state.slug}/chat/${state.chatThreadId}`, { message: `Let's discuss this comment specifically: "${c.comment_text}"`, comment_id: c.id })
    .then(async () => { await loadChat(); })
    .catch(async () => { await loadChat(); });
}

async function unfocusChat() {
  state.chatThreadId = "project-main";
  await loadChat();
}

// ------------------------------------------------------------- new project -

function openNewProjectModal() {
  $("npName").value = "";
  $("npTitle").value = "";
  $("npJournal").value = "";
  $("npManuscriptPath").value = "";
  $("npCommentPaths").value = "";
  $("npGapPlanPath").value = "";
  $("npStatus").textContent = "";
  $("newProjectModal").classList.remove("hidden");
}

function parseCommentPaths(raw) {
  const lines = raw.split("\n").map((l) => l.trim()).filter(Boolean);
  return lines.map((line) => {
    const idx = line.indexOf(":");
    // careful: Windows paths contain a colon after the drive letter (C:\...)
    // Treat as "label: path" only if a colon appears before any path separator.
    const sepIdx = Math.max(line.indexOf("\\"), line.indexOf("/"));
    if (idx > 0 && (sepIdx === -1 || idx < sepIdx - 1)) {
      const label = line.slice(0, idx).trim();
      const path = line.slice(idx + 1).trim();
      return { label, path };
    }
    const name = line.split(/[\\/]/).pop();
    return { label: name, path: line };
  });
}

async function createProject() {
  const name = $("npName").value.trim();
  const manuscriptPath = $("npManuscriptPath").value.trim();
  const commentPaths = parseCommentPaths($("npCommentPaths").value);
  if (!name) { $("npStatus").textContent = "Project name is required."; return; }
  if (!manuscriptPath) { $("npStatus").textContent = "A manuscript file path is required."; return; }
  if (commentPaths.length === 0) { $("npStatus").textContent = "At least one reviewer comment file is required."; return; }

  $("npCreateBtn").disabled = true;
  $("npStatus").textContent = "Creating project and extracting comments with the local model… this can take a minute.";
  try {
    const manuscriptName = manuscriptPath.split(/[\\/]/).pop();
    const body = {
      name,
      title: $("npTitle").value.trim(),
      journal: $("npJournal").value.trim(),
      manuscript_files: [{ path: manuscriptPath, label: manuscriptName }],
      comment_sources: commentPaths,
      gap_plan_file: $("npGapPlanPath").value.trim() || null,
    };
    const summary = await api("POST", "/api/projects", body);
    if (summary.warnings && summary.warnings.length) {
      alert("Project created, but with warnings:\n\n" + summary.warnings.join("\n"));
    }
    $("newProjectModal").classList.add("hidden");
    await loadProjectsList();
    $("projectSelect").value = summary.slug;
    await selectProject(summary.slug);
  } catch (e) {
    $("npStatus").textContent = `Error: ${e.message}`;
  } finally {
    $("npCreateBtn").disabled = false;
  }
}

// ------------------------------------------------------------------ browse -

async function openBrowse(targetInputId) {
  state.browseTarget = targetInputId;
  $("browseModal").classList.remove("hidden");
  await navigateBrowse("");
}

async function navigateBrowse(path) {
  const res = await api("GET", `/api/browse?path=${encodeURIComponent(path)}`);
  $("browsePath").textContent = res.cwd || "(drives)";
  const list = $("browseList");
  list.innerHTML = "";
  if (res.parent) {
    const up = document.createElement("div");
    up.className = "browse-item dir";
    up.textContent = ".. (up)";
    up.addEventListener("click", () => navigateBrowse(res.parent));
    list.appendChild(up);
  }
  for (const entry of res.entries) {
    const div = document.createElement("div");
    div.className = "browse-item " + (entry.is_dir ? "dir" : "file");
    div.textContent = entry.name;
    div.addEventListener("click", () => {
      if (entry.is_dir) {
        navigateBrowse(entry.path);
      } else {
        selectBrowsedFile(entry.path);
      }
    });
    list.appendChild(div);
  }
}

function selectBrowsedFile(path) {
  const target = $(state.browseTarget);
  if (target.tagName === "TEXTAREA") {
    const name = path.split(/[\\/]/).pop();
    target.value = target.value ? target.value + "\n" + `${name}: ${path}` : `${name}: ${path}`;
  } else {
    target.value = path;
  }
  $("browseModal").classList.add("hidden");
}

// -------------------------------------------------------------------- init -

function wireEvents() {
  $("projectSelect").addEventListener("change", (e) => selectProject(e.target.value));
  $("newProjectBtn").addEventListener("click", openNewProjectModal);
  $("npCancelBtn").addEventListener("click", () => $("newProjectModal").classList.add("hidden"));
  $("npCreateBtn").addEventListener("click", createProject);

  document.querySelectorAll(".browseBtn").forEach((b) =>
    b.addEventListener("click", () => openBrowse(b.dataset.browseTarget)));
  $("browseCancelBtn").addEventListener("click", () => $("browseModal").classList.add("hidden"));

  $("gapAnalysisBtn").addEventListener("click", runGapAnalysis);
  $("exportLetterBtn").addEventListener("click", exportLetter);
  $("letterCloseBtn").addEventListener("click", () => $("letterModal").classList.add("hidden"));
  $("letterCopyBtn").addEventListener("click", async () => {
    await navigator.clipboard.writeText($("letterText").value);
    $("letterCopyBtn").textContent = "Copied!";
    setTimeout(() => { $("letterCopyBtn").textContent = "Copy to clipboard"; }, 1500);
  });

  $("detailStatus").addEventListener("change", saveStatus);
  $("saveNotesBtn").addEventListener("click", saveNotes);
  $("proposePatchBtn").addEventListener("click", proposePatch);
  $("draftResponseBtn").addEventListener("click", draftResponse);
  $("saveResponseBtn").addEventListener("click", saveResponseText);
  $("focusChatBtn").addEventListener("click", focusChatOnComment);
  $("unfocusChatBtn").addEventListener("click", unfocusChat);

  $("chatSendBtn").addEventListener("click", sendChatMessage);
  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendChatMessage();
    }
  });
}

async function init() {
  wireEvents();
  await refreshHealth();
  setInterval(refreshHealth, 15000);
  await loadProjectsList();
}

init();
