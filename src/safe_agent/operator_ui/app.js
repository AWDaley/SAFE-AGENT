(() => {
  "use strict";

  // Local-only bearer token. Never stored in cookies, storage, URLs, or DOM.
  let token = "";
  let after = null;
  let inbox = [];
  let activeTask = null;
  let activeSnapshot = null;
  let activePreview = null;
  let requestGeneration = 0;

  const node = (id) => document.getElementById(id);
  const setText = (id, value) => { node(id).textContent = value == null ? "—" : String(value); };
  const status = (message) => { setText("status", message); };
  const dateLabel = (raw) => {
    if (!raw) return "—";
    const date = new Date(raw);
    return Number.isNaN(date.getTime()) ? "Invalid time" : date.toLocaleString();
  };
  const briefDigest = (digest) =>
    typeof digest === "string" ? digest.slice(0, 19) + "…" : "—";

  const disconnect = () => {
    token = "";
    inbox = [];
    after = null;
    activeTask = null;
    activeSnapshot = null;
    activePreview = null;
    requestGeneration += 1;
    node("workspace").hidden = true;
    node("signInPanel").hidden = false;
    node("tokenInput").value = "";
    node("taskList").replaceChildren();
    node("eventList").replaceChildren();
    node("previewPanel").hidden = true;
    node("eventsPanel").hidden = true;
    node("taskFacts").hidden = true;
    status("Session cleared from page memory.");
  };

  async function request(path, method, payload) {
    if (!token) throw new Error("No active operator session.");
    const headers = { Authorization: "Bearer " + token, Accept: "application/json" };
    const init = {
      method: method || "GET", headers, cache: "no-store",
      credentials: "omit", redirect: "error"
    };
    if (payload !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(payload);
    }
    const reply = await fetch(path, init);
    if (reply.status === 401) {
      disconnect();
      throw new Error("Local operator session expired or is invalid.");
    }
    if (!reply.ok) throw new Error("Operation refused (" + reply.status + ").");
    return reply.json();
  }

  function drawInbox() {
    node("taskList").replaceChildren();
    for (const item of inbox) {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("aria-current", String(item.task_id === activeTask));
      button.appendChild(document.createTextNode(item.task_id));
      const phase = document.createElement("span");
      phase.className = "phase";
      phase.textContent = item.phase + (item.approval_pending ? " · Review requested" : "");
      button.appendChild(phase);
      button.addEventListener("click", () => { void selectTask(item.task_id); });
      li.appendChild(button);
      node("taskList").appendChild(li);
    }
    setText("inboxCount", inbox.length + " tasks");
    node("loadMoreBtn").hidden = after === null;
    node("emptyInbox").hidden = inbox.length !== 0;
  }

  async function loadInbox(reset) {
    if (reset) { inbox = []; after = null; }
    const cursor = after ? "&after=" + encodeURIComponent(after) : "";
    const data = await request("/v1/tasks?limit=20" + cursor);
    if (data.schema_version !== "safe_agent.operator_inbox_page.v1" ||
        !Array.isArray(data.items)) throw new Error("Unexpected inbox response.");
    inbox = inbox.concat(data.items);
    after = data.next_cursor || null;
    drawInbox();
  }

  function drawFacts(container, entries) {
    container.replaceChildren();
    for (const [label, value] of entries) {
      const outer = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value == null ? "—" : String(value);
      if (label.includes("digest")) dd.className = "hash";
      outer.append(dt, dd);
      container.appendChild(outer);
    }
  }

  const decisionEnabled = () => Boolean(
    activeSnapshot && activePreview &&
    activeSnapshot.phase === "AWAITING_APPROVAL" &&
    activePreview.approval_required === true &&
    activePreview.proposal_hash === activeSnapshot.proposal_hash &&
    activePreview.state_version === activeSnapshot.state_version &&
    node("reviewedCheck").checked && node("expiresInput").value
  );

  function refreshDecisionButtons() {
    const available = decisionEnabled();
    node("approveBtn").disabled = !available;
    node("denyBtn").disabled = !available;
  }

  async function selectTask(taskId) {
    activeTask = taskId;
    activeSnapshot = null;
    activePreview = null;
    const generation = ++requestGeneration;
    node("previewPanel").hidden = true;
    node("eventsPanel").hidden = true;
    node("taskFacts").hidden = true;
    node("decisionPanel").hidden = true;
    node("reviewedCheck").checked = false;
    node("expiresInput").value = "";
    setText("detailTitle", taskId);
    status("Loading trusted task details…");
    drawInbox();

    try {
      const snap = await request("/v1/tasks/" + encodeURIComponent(taskId));
      if (generation !== requestGeneration) return;
      activeSnapshot = snap;
      node("taskFacts").hidden = false;
      setText("taskPhase", snap.phase);
      setText("taskVersion", snap.state_version);
      setText("taskManifest", briefDigest(snap.manifest_hash));
      setText("taskProposal", briefDigest(snap.proposal_hash));
      setText("taskEffects", snap.external_effects);
      setText("taskExecution", "No provider status");
      setText("detailLead", "Immutable task state · Organization-scoped");
      try {
        const execution = await request("/v1/tasks/" + encodeURIComponent(taskId) + "/execution");
        if (generation === requestGeneration) {
          setText("taskExecution", execution.provider_kind + " · " + execution.status);
        }
      } catch (_) {
        if (generation === requestGeneration) setText("taskExecution", "No outbox recorded");
      }
      if (snap.proposal_hash) {
        try {
          const preview = await request("/v1/tasks/" + encodeURIComponent(taskId) + "/preview");
          if (generation !== requestGeneration) return;
          activePreview = preview;
          const items = [
            ["Operation", preview.operation], ["Customer handle", preview.customer_ref],
            ["Source", preview.source], ["Simulation", "Advisory; no reservation"]
          ];
          if (preview.operation === "calendar.create_event") {
            items.push(["Calendar handle", preview.calendar_ref], ["Template", preview.template_id],
              ["Start", dateLabel(preview.start_at)], ["End", dateLabel(preview.end_at)],
              ["Time zone", preview.time_zone]);
          } else {
            items.push(["Task type", preview.task_code], ["Due", dateLabel(preview.due_at)]);
          }
          drawFacts(node("previewFacts"), items);
          node("previewPanel").hidden = false;
          node("decisionPanel").hidden = !(
            snap.phase === "AWAITING_APPROVAL" && preview.approval_required === true
          );
          refreshDecisionButtons();
        } catch (_) {
          if (generation === requestGeneration) status("Preview unavailable or not authorized.");
        }
      }
      try {
        const audit = await request("/v1/tasks/" + encodeURIComponent(taskId) + "/events");
        if (generation !== requestGeneration) return;
        node("eventList").replaceChildren();
        for (const event of audit) {
          const item = document.createElement("li");
          const kind = document.createElement("strong");
          kind.textContent = event.kind;
          const when = document.createElement("time");
          when.textContent = dateLabel(event.occurred_at);
          item.append(kind, when);
          node("eventList").appendChild(item);
        }
        node("eventsPanel").hidden = false;
        setText("auditNotice", audit.length ? "" : "No audit evidence available.");
      } catch (_) {
        if (generation === requestGeneration) {
          node("eventsPanel").hidden = false;
          setText("auditNotice", "Audit permission unavailable.");
        }
      }
      if (generation === requestGeneration) status("Task loaded. No provider writes are available.");
    } catch (error) {
      if (generation === requestGeneration) status(error.message);
    }
  }

  async function submitDecision(approved) {
    if (!decisionEnabled()) return;
    const expires = new Date(node("expiresInput").value);
    if (Number.isNaN(expires.getTime()) || expires.getTime() <= Date.now()) {
      status("Choose a future approval expiration.");
      return;
    }
    const message = (approved ? "Approve" : "Deny") +
      " this exact proposal? The decision will be recorded in the audit ledger.";
    if (!window.confirm(message)) return;
    node("approveBtn").disabled = true;
    node("denyBtn").disabled = true;
    try {
      await request(
        "/v1/tasks/" + encodeURIComponent(activeTask) + "/approvals", "POST", {
          proposal_hash: activePreview.proposal_hash,
          expected_state_version: activePreview.state_version,
          approved,
          expires_at: expires.toISOString()
        }
      );
      const selected = activeTask;
      await loadInbox(true);
      await selectTask(selected);
      status(approved ? "Approval recorded; no provider effect was dispatched." :
        "Proposal denial recorded; no provider effect was dispatched.");
    } catch (error) {
      status(error.message + " Refresh the task before deciding again.");
      refreshDecisionButtons();
    }
  }

  node("connectForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const entered = node("tokenInput").value.trim();
    node("tokenInput").value = "";
    if (!entered) { status("Enter a local operator token."); return; }
    token = entered;
    try {
      await loadInbox(true);
      node("signInPanel").hidden = true;
      node("workspace").hidden = false;
      status("Connected for scoped task review. No live Google writes enabled.");
    } catch (error) {
      disconnect();
      status(error.message);
    }
  });
  node("disconnectBtn").addEventListener("click", disconnect);
  node("refreshBtn").addEventListener("click", async () => {
    try { await loadInbox(true); status("Inbox refreshed."); } catch (error) { status(error.message); }
  });
  node("loadMoreBtn").addEventListener("click", async () => {
    try { await loadInbox(false); } catch (error) { status(error.message); }
  });
  node("reviewedCheck").addEventListener("change", refreshDecisionButtons);
  node("expiresInput").addEventListener("change", refreshDecisionButtons);
  node("approveBtn").addEventListener("click", () => { void submitDecision(true); });
  node("denyBtn").addEventListener("click", () => { void submitDecision(false); });
})();
