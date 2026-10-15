/* Revision evidence, file versions and submission checks. Uses app.js state/API. */
function renderRevisionBanner() {
  const r = state.project?.revision;
  if (!r) return;
  const cache = state.project.state.review?.cache;
  $("revisionBanner").innerHTML = `
    <strong>File versions</strong> · Review: ${esc(r.review)} · Gap analysis: ${esc(r.gap_analysis)}
    ${cache ? `<div class="muted small-text">Last review: ${cache.reused_calls} saved calls reused, ${cache.new_calls} new calls.</div>` : ""}
    ${r.changed_files.length ? `<details><summary>${r.changed_files.length} files changed since the last snapshot</summary><ul>${r.changed_files.map(p => `<li>${esc(p)}</li>`).join("")}</ul></details>` : ""}
    <details><summary>${r.snapshots.length} saved manuscript versions</summary>
      ${r.snapshots.slice(-8).reverse().map(s => `<div>${esc(new Date(s.created_at * 1000).toLocaleString())} · ${esc(s.label)} · <code>${esc(s.id.slice(0, 10))}</code></div>`).join("")}
    </details>
    <button id="saveRevisionSnapshot" class="small">Save snapshot</button>
    <button id="refreshRevisionSources" class="small">Refresh file versions</button>`;
  $("saveRevisionSnapshot").onclick = async () => {
    try { await api("POST", `${P()}/snapshots`, {}); await refreshProject(); renderAll(); }
    catch (e) { alert(e.message); }
  };
  $("refreshRevisionSources").onclick = async () => {
    try { await refreshProject(); renderAll(); } catch (e) { alert(e.message); }
  };
}

function renderRevisionChecklist(c) {
  const view = state.project.checklists?.[c.id];
  const el = $("revisionChecklist");
  if (!view) { el.innerHTML = ""; return; }
  const files = state.project.source_files || [];
  const patchIds = c.patch_ids || [];
  el.innerHTML = `<h4>Revision checklist · ${view.completed}/${view.total} supported</h4>
    <p class="muted small-text">Link each subrequest to exact source evidence or an applied edit. For a rebuttal, set the stance to rebut or the status to won't fix and record your reason.</p>
    <p class="small-text">Assessment: ${esc(state.project.artifact_versions?.[c.id]?.assessment || "unknown")} · Gap: ${esc(state.project.artifact_versions?.[c.id]?.gap || "unknown")}</p>
    ${view.actions.map((a, i) => `<details class="revision-action" ${i === 0 && !a.complete ? "open" : ""}>
      <summary>${a.complete ? "✓" : "○"} ${esc(a.text)}</summary>
      <label class="field-label" for="actionText${i}">Required action</label>
      <textarea id="actionText${i}" rows="2">${esc(a.text)}</textarea>
      <label><input id="actionDone${i}" type="checkbox" ${a.done ? "checked" : ""} /> I have completed this action</label>
      ${a.done && !a.complete ? '<p class="unverified">Completion needs current evidence or a rebuttal rationale.</p>' : ""}
      <div class="action-evidence">${(a.evidence || []).map((e, j) => `<div class="evidence-link">
        <strong>${e.verified && e.freshness === "current" ? "Verified" : "Needs review"}</strong> · ${esc(e.path)}
        · ${esc(e.location || `line ${e.line || "?"}`)} · ${esc(e.freshness)}
        <blockquote>${esc(e.quote)}</blockquote>
        <button class="small remove-evidence" data-i="${i}" data-j="${j}">Remove evidence link</button></div>`).join("")}</div>
      <label class="field-label" for="evidenceFile${i}">Add source evidence</label>
      <select id="evidenceFile${i}" aria-label="Evidence source"><option value="">Choose a project file…</option>
        ${files.map(f => `<option value="${esc(f.path)}">${esc(f.path)}</option>`).join("")}</select>
      <textarea id="evidenceQuote${i}" rows="2" placeholder="Paste the exact supporting passage from that file"></textarea>
      <input id="evidenceLocation${i}" aria-label="Evidence location" placeholder="Section, table, or page (optional)" />
      ${patchIds.length ? `<label class="field-label" for="actionPatch${i}">Linked edits (Ctrl/Cmd to select several)</label><select id="actionPatch${i}" multiple>
        ${patchIds.map(pid => { const p = state.project.state.patches.find(p => p.id === pid); return `<option value="${esc(pid)}" ${(a.patch_ids || []).includes(pid) ? "selected" : ""}>${esc(pid)} · ${esc(p?.status || "missing")} · ${esc(p?.rationale || "")}</option>`; }).join("")}</select>` : ""}
      ${(a.patches || []).map(p => `<div class="small-text ${p.verified ? "" : "unverified"}">Edit ${esc(p.id)}: ${esc(p.status)} · ${p.verified ? "present in current file" : "needs review"}</div>`).join("")}
      <label class="field-label" for="actionRationale${i}">Reason for rebuttal or no change</label>
      <textarea id="actionRationale${i}" rows="2">${esc(a.rationale || "")}</textarea>
      <label class="field-label" for="actionResponse${i}">Linked response paragraph (optional exact quote)</label>
      <textarea id="actionResponse${i}" rows="2">${esc(a.response_excerpt || "")}</textarea>
      ${a.response_excerpt && !a.response_link_valid ? '<p class="unverified">This paragraph is absent from the saved response.</p>' : ""}
      <div class="pane-actions"><button class="save-action" data-i="${i}">Save action and evidence</button>
      <button class="delete-action small" data-i="${i}">Remove action</button></div>
    </details>`).join("")}
    <label class="field-label" for="newActionText">Add a subrequest</label>
    <textarea id="newActionText" rows="2" placeholder="One concrete action…"></textarea>
    <div class="pane-actions"><button id="addRevisionAction">Add action</button><button id="gapSelectedComment">Reassess this comment</button></div>`;
  const endpoint = `${P()}/comments/${enc(c.id)}/actions`;
  const save = async (path, fields, method = "POST") => {
    try { await api(method, path, fields); await refreshProject(); renderAll(); }
    catch (e) { alert(`Could not save checklist: ${e.message}`); }
  };
  el.querySelectorAll(".save-action").forEach(button => button.onclick = async () => {
    const i = Number(button.dataset.i), a = view.actions[i];
    const evidence = (a.evidence || []).map(e => ({ path: e.path, quote: e.quote, location: e.location, id: e.id }));
    const quote = $("evidenceQuote" + i).value;
    if (quote.trim()) evidence.push({ path: $("evidenceFile" + i).value, quote, location: $("evidenceLocation" + i).value });
    await withBusy(button, "Saving…", () => save(`${endpoint}/${enc(a.id)}`, {
      text: $("actionText" + i).value, done: $("actionDone" + i).checked, evidence,
      patch_ids: $("actionPatch" + i) ? Array.from($("actionPatch" + i).selectedOptions).map(o => o.value) : [],
      rationale: $("actionRationale" + i).value, response_excerpt: $("actionResponse" + i).value }));
  });
  el.querySelectorAll(".remove-evidence").forEach(button => button.onclick = () => {
    const a = view.actions[Number(button.dataset.i)];
    save(`${endpoint}/${enc(a.id)}`, { evidence: a.evidence.filter((_, j) => j !== Number(button.dataset.j)) });
  });
  el.querySelectorAll(".delete-action").forEach(button => button.onclick = () => {
    if (confirm("Remove this required action from the checklist?")) save(`${endpoint}/${enc(view.actions[Number(button.dataset.i)].id)}`, {}, "DELETE");
  });
  $("addRevisionAction").onclick = () => save(endpoint, { text: $("newActionText").value });
  $("gapSelectedComment").onclick = () => startJob(`${P()}/gap-analysis`, { comment_ids: [c.id], force: $("freshReviewToggle").checked });
}

function renderResponseAudit(c) {
  const check = state.project.response_checks?.[c.id];
  $("checkResponseBtn").classList.toggle("hidden", c.origin === "ai-review");
  $("responseAudit").innerHTML = check ? `<p class="small-text">Response draft version: ${esc(state.project.artifact_versions?.[c.id]?.response_draft || "unknown")}</p><p><strong>Response audit: ${esc(check.freshness)}</strong>${check.ok ? " · supported" : " · review needed"}</p>
    <p>${esc(check.summary || "")}</p>
    ${(check.unanswered_requests || []).map(x => `<p class="unverified">Unanswered: ${esc(x)}</p>`).join("")}
    ${(check.claims || []).map(cl => `<details class="evidence-link"><summary>${esc(cl.verdict)}: ${esc(cl.response_quote)}</summary>
      <p>${esc(cl.reason)}</p>${cl.evidence_quote ? `<blockquote>${esc(cl.evidence_quote)}</blockquote><p class="small-text">${esc(cl.source_path)}</p>` : ""}</details>`).join("")}` : "";
}

async function showSubmissionReview() {
  $("submissionModal").classList.remove("hidden");
  $("submissionBundleLink").href = `${P()}/submission.zip`;
  $("submissionResults").textContent = "Checking current files…";
  try {
    const r = await api("GET", `${P()}/submission`);
    $("submissionResults").innerHTML = `<h3>${r.ready ? "Ready for author review" : "Draft — unresolved items remain"}</h3>
      <p>${r.blockers} blockers · ${r.warnings} warnings</p>
      <ul>${r.issues.map(i => `<li><strong>${esc(i.level)}</strong> ${i.comment_id ? `<button class="small issue-comment" data-id="${esc(i.comment_id)}">${esc(i.comment_id)}</button> ` : ""}${esc(i.message)}</li>`).join("")}</ul>
      <p class="muted small-text">${esc(r.note)} Downloads include the response letter, change summary, checklist, remaining issues and source manifest.</p>`;
    $("submissionResults").querySelectorAll(".issue-comment").forEach(button => button.onclick = () => {
      $("submissionModal").classList.add("hidden"); selectComment(button.dataset.id);
    });
  } catch (e) { $("submissionResults").textContent = e.message; }
}

function wireWorkflowEvents() {
  $("submissionBtn").onclick = showSubmissionReview;
  $("refreshSubmissionBtn").onclick = showSubmissionReview;
  $("submissionCloseBtn").onclick = () => $("submissionModal").classList.add("hidden");
  $("auditAllResponsesBtn").onclick = () => {
    $("submissionModal").classList.add("hidden"); startJob(`${P()}/submission/check`, { force: $("freshReviewToggle").checked });
  };
  $("checkResponseBtn").onclick = () => {
    const c = getSelectedComment();
    if (!c) return;
    if ($("responseText").value !== (c.response_text || "")) { alert("Save your response text before checking its claims."); return; }
    startJob(`${P()}/comments/${enc(c.id)}/check-response`, { force: $("freshReviewToggle").checked });
  };
}
