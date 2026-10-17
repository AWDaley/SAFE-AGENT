(() => {
  "use strict";
  let token = "";
  let inspected = null;
  let generation = 0;
  let controller = null;
  let busy = false;
  const node = (id) => document.getElementById(id);
  const text = (id, value) => { node(id).textContent = String(value ?? "—"); };
  const identifier = /^[A-Za-z][A-Za-z0-9._:-]{0,127}$/;
  const digest = /^sha256:[0-9a-f]{64}$/;
  const explanations = Object.freeze({
    NOT_STARTED: "No coordinated planning input is recorded. Start an authorized planning task in the task-review workspace; recovery does not run inference.",
    INPUT_MISSING: "The original coordinated input is unavailable. Ask the trusted operator to review the evidence. Do not create replacement work to bypass it.",
    INPUT_SAVED: "The original input is saved, but inference admission is not recorded. There is no saved result to prepare. Recovery does not dispatch this request.",
    WAITING_TO_DISPATCH: "Admission is recorded, but no dispatch marker is recorded. Inspect the existing coordinator; this page cannot send the request.",
    INFERENCE_PENDING: "Dispatch started and no validated result is recorded. Refresh status; do not run a replacement inference call.",
    INFERENCE_UNCERTAIN: "The inference outcome is uncertain. Preserve the original attempt and cost hold. A trusted operator must reconcile the evidence; no automatic retry is permitted.",
    INFERENCE_STOPPED: "Inference stopped before a dispatch marker. This status does not release accounting holds or authorize replacement work.",
    RESULT_RECOVERABLE: "A validated result is saved. An authorized initiating operator can prepare that exact result for review without making another model call.",
    RESULT_EXPIRED: "The original inference, task, projection, or planning deadline has expired. Recovery cannot renew it or generate a replacement result.",
    RESULT_MISSING: "Validation is recorded but its reusable result is missing. Do not repeat inference to recreate missing evidence.",
    SOURCE_CHANGED: "The saved source can no longer be revalidated against the current source. No proposal can be prepared from this snapshot.",
    AUTHORITY_CHANGED: "The task's reference permissions are unavailable or revoked. Recovery cannot replace or renew those permissions.",
    POLICY_CHANGED: "The configured policy no longer matches this attempt. A trusted operator must review the configuration; recovery will not substitute a new policy.",
    PREPARED: "The final proposal is already prepared. Review its exact contents and approval status in the task-review workspace. No new inference or execution is needed here.",
    NEEDS_CLARIFICATION: "The saved result requested clarification. This task is not automatically replanned; a reviewed retirement and replanning process is still required.",
    TASK_ADVANCED: "The task has moved beyond planning preparation. Inspect its approval and execution evidence in the task-review workspace. Recovery cannot repeat it.",
    BLOCKED: "This task is not eligible for the configured coordinated recovery workflow. No recovery action is enabled."
  });

  function resetResults() {
    inspected = null;
    busy = false;
    generation += 1;
    if (controller) controller.abort();
    controller = null;
    node("recoveryResult").hidden = true;
    node("resumeSavedResultBtn").hidden = true;
    node("resumeSavedResultBtn").disabled = true;
    node("inspectRecoveryBtn").disabled = false;
  }

  function clearSession() {
    resetResults();
    token = "";
    node("recoveryToken").value = "";
    node("recoveryTask").value = "";
    for (const id of ["recoveryState", "recoveryGuidance", "recoveryPhase",
                      "recoveryInference", "recoveryDeadline", "recoveryAttempts"]) text(id, "");
    text("recoveryStatus", "Session cleared from page memory. Refresh after signing in to inspect any request already submitted.");
  }

  function validView(data, task) {
    const flags = data && data.advisory_only === true && data.automatic_retry_allowed === false &&
      data.new_inference_requested === false && data.provider_effect_performed === false &&
      data.billing_changed === false;
    const actions = data && Array.isArray(data.allowed_actions) ? data.allowed_actions : [];
    return flags && data.schema_version === "safe_agent.planning_recovery.v1" &&
      data.task_id === task && Object.hasOwn(explanations, data.status) &&
      digest.test(data.recovery_revision) && Number.isSafeInteger(data.task_state_version) &&
      data.task_state_version >= 0 && Number.isSafeInteger(data.workspace_version) &&
      data.workspace_version >= 0 && Number.isSafeInteger(data.attempts_used) &&
      data.attempts_used >= 0 && data.attempts_used <= 10 &&
      (JSON.stringify(actions) === '["refresh"]' ||
       (data.status === "RESULT_RECOVERABLE" &&
        JSON.stringify(actions) === '["refresh","resume_saved_result"]'));
  }

  function show(data, task) {
    if (!validView(data, task)) throw new Error("Unexpected recovery response. No action is enabled.");
    inspected = data;
    text("recoveryState", data.status.replaceAll("_", " "));
    text("recoveryGuidance", explanations[data.status]);
    text("recoveryPhase", data.task_phase);
    text("recoveryInference", data.inference_state ?? "Not admitted");
    text("recoveryDeadline", data.inference_deadline ?? "Not recorded");
    text("recoveryAttempts", data.attempts_used);
    node("recoveryResult").hidden = false;
    const canResume = data.allowed_actions.includes("resume_saved_result");
    node("resumeSavedResultBtn").hidden = !canResume;
    node("resumeSavedResultBtn").disabled = !canResume;
  }

  async function request(task, action, payload, signal, currentGeneration) {
    const reply = await fetch("/v1/tasks/" + encodeURIComponent(task) + "/planning/" + action, {
      method: payload ? "POST" : "GET",
      headers: { Authorization: "Bearer " + token, Accept: "application/json",
                 ...(payload ? {"Content-Type": "application/json"} : {}) },
      body: payload ? JSON.stringify(payload) : undefined,
      cache: "no-store", credentials: "omit", redirect: "error", signal
    });
    if (reply.status === 401) {
      if (currentGeneration === generation) clearSession();
      throw new Error("Operator session expired or is invalid.");
    }
    if (!reply.ok) {
      // Never render response/error bodies, which may contain untrusted text.
      throw new Error(reply.status === 409 ? "State changed or recovery is blocked. Refresh status; do not start replacement work." :
        reply.status === 404 ? "Task or coordinated recovery is unavailable under this session." :
        "Recovery is unavailable. Refresh to inspect the recorded state before continuing.");
    }
    return reply.json();
  }

  node("recoveryForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    const entered = node("recoveryToken").value.trim();
    node("recoveryToken").value = "";
    if (entered) token = entered;
    const task = node("recoveryTask").value.trim();
    resetResults();
    if (!token || !identifier.test(task)) {
      text("recoveryStatus", "Enter a local operator token and a valid existing task ID.");
      return;
    }
    const currentGeneration = generation;
    controller = new AbortController();
    busy = true;
    node("inspectRecoveryBtn").disabled = true;
    text("recoveryStatus", "Reading recorded status. No inference will be started.");
    try {
      const data = await request(task, "recovery", null, controller.signal, currentGeneration);
      if (generation !== currentGeneration) return;
      show(data, task);
      text("recoveryStatus", "Status inspected. No inference, approval, execution or billing changes requested.");
    } catch (error) {
      if (generation === currentGeneration) text("recoveryStatus", error.message);
    } finally {
      if (generation === currentGeneration) { busy = false; node("inspectRecoveryBtn").disabled = false; }
    }
  });

  node("resumeSavedResultBtn").addEventListener("click", async () => {
    if (busy || !inspected || !inspected.allowed_actions.includes("resume_saved_result")) return;
    const task = node("recoveryTask").value.trim();
    if (task !== inspected.task_id) { resetResults(); return; }
    if (!window.confirm("Prepare this saved result for human review? No new model call, approval, queueing or billing reconciliation will be requested.")) return;
    const payload = { schema_version: "safe_agent.resume_saved_result.v1",
      expected_state_version: inspected.task_state_version,
      expected_workspace_version: inspected.workspace_version,
      expected_recovery_revision: inspected.recovery_revision };
    const currentGeneration = generation;
    controller = new AbortController();
    busy = true;
    node("resumeSavedResultBtn").disabled = true;
    node("inspectRecoveryBtn").disabled = true;
    try {
      const data = await request(task, "resume-saved", payload, controller.signal, currentGeneration);
      if (generation !== currentGeneration) return;
      show(data, task);
      text("recoveryStatus", "Recorded state inspected after saved-result recovery. Review any prepared proposal separately before approval or execution.");
    } catch (error) {
      if (generation === currentGeneration) {
        inspected = null;
        node("resumeSavedResultBtn").hidden = true;
        text("recoveryStatus", error.message + " A preparation request may already be recorded; inspect it before continuing.");
      }
    } finally {
      if (generation === currentGeneration) { busy = false; node("inspectRecoveryBtn").disabled = false; }
    }
  });

  node("clearRecoveryBtn").addEventListener("click", clearSession);
  for (const id of ["recoveryTask", "recoveryToken"]) {
    node(id).addEventListener("input", () => {
      resetResults();
      if (id === "recoveryToken") token = "";
      text("recoveryStatus", "Selection or session changed. Inspect the task before taking an action.");
    });
  }
})();
