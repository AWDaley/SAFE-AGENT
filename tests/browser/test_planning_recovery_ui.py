"""Real Chromium UI tests with an in-page simulated fetch backend.

These exercise the shipped DOM/JavaScript without navigation or networking.
They do not test HTTP transport, W4 SQL or provider correctness.
Run explicitly; a browser prerequisite failure is an error, never a silent skip.
"""
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "src/safe_agent/operator_ui"
pytestmark = pytest.mark.skipif(
    os.environ.get("SAFE_AGENT_TEST_RECOVERY_BROWSER") != "1",
    reason="requires explicit recovery browser test job",
)
TOKEN = "local-browser-fixture-only"


def view(task="task_browser", status="RESULT_RECOVERABLE"):
    return {
        "schema_version": "safe_agent.planning_recovery.v1", "task_id": task,
        "task_phase": "ASSIGNED", "task_state_version": 0, "workspace_version": 1,
        "attempts_used": 1, "status": status,
        "inference_state": "RESPONSE_VALIDATED", "inference_deadline": "2099-01-01T00:00:00Z",
        "recovery_revision": "sha256:" + "a" * 64,
        "allowed_actions": ["refresh", "resume_saved_result"] if status == "RESULT_RECOVERABLE" else ["refresh"],
        "advisory_only": True, "automatic_retry_allowed": False,
        "new_inference_requested": False, "provider_effect_performed": False, "billing_changed": False,
    }


@pytest.fixture(scope="module")
def browser_engine():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        path = os.environ.get("SAFE_AGENT_CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(headless=True, **({"executable_path": path} if path else {}))
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def browser_rig(browser_engine):
    browser = browser_engine
    context = browser.new_context()
    page = context.new_page()
    page.route("**/*", lambda route: route.abort())
    # No browser navigation and no server connection. Load the exact shipped
    # DOM, replacing only external asset loading with local script injection.
    html = (ASSETS / "recovery.html").read_text()
    html = re.sub(r'<script[^>]*src=[^>]*></script>', '', html)
    html = re.sub(r'<link[^>]*>', '', html)
    page.set_content(html)
    page.evaluate(r"""(initial) => {
      window.fixtureState = {status: 'RESULT_RECOVERABLE', code: 200,
        post_code: 200, patch: {}, requests: [], hold: false, started: false};
      window.storageAccesses = [];
      for (const name of ['localStorage','sessionStorage']) {
        Object.defineProperty(window, name, {get() {window.storageAccesses.push(name); throw new Error('Storage forbidden');}});
      }
      let releases = [];
      window.releasePending = () => { for (const release of releases) release(); releases = []; };
      window.fetch = async (url, options) => {
        const state = window.fixtureState;
        const method = options.method;
        const body = options.body ? JSON.parse(options.body) : null;
        state.requests.push([method, url, body]);
        if (!/^\/v1\/tasks\/[A-Za-z][A-Za-z0-9._:-]*\/planning\/(recovery|resume-saved)$/.test(url)) {
          throw new Error('Unexpected fixture request');
        }
        const task = url.split('/')[3];
        if (options.headers.Authorization !== 'Bearer local-browser-fixture-only') {
          return {status: 401, ok: false, json: async () => ({})};
        }
        let data = {...initial, task_id: task, status: state.status,
          allowed_actions: state.status === 'RESULT_RECOVERABLE' ? ['refresh','resume_saved_result'] : ['refresh'],
          ...state.patch};
        let code = method === 'POST' ? state.post_code : state.code;
        if (method === 'POST' && code === 200) {
          state.status = 'PREPARED';
          data = {...data, status: 'PREPARED', task_phase: 'AWAITING_APPROVAL',
            task_state_version: 3, workspace_version: 2, allowed_actions: ['refresh']};
        }
        state.started = true;
        if (state.hold) {
          await new Promise((resolve,reject) => {
            releases.push(resolve);
            options.signal.addEventListener('abort', () => reject(new DOMException('Aborted','AbortError')), {once:true});
          });
        }
        return {status: code, ok: code === 200,
          json: async () => code === 200 ? data : {detail:'UNTRUSTED_EXCEPTION_MARKER'}};
      };
    }""", view())
    page.add_script_tag(content=(ASSETS / "recovery.js").read_text())

    class StateProxy:
        def __getitem__(self, key):
            return page.evaluate("key => window.fixtureState[key]", key)

        def __setitem__(self, key, value):
            page.evaluate("([key,value]) => {window.fixtureState[key] = value;}", [key, value])

    try:
        yield SimpleNamespace(page=page, context=context, state=StateProxy())
    finally:
        context.close()


def inspect(rig):
    rig.page.locator("#recoveryToken").fill(TOKEN)
    rig.page.locator("#recoveryTask").fill("task_browser")
    rig.page.locator("#inspectRecoveryBtn").click()


def test_resume_is_explicit_and_does_not_send_model_or_execution_requests(browser_rig):
    from playwright.sync_api import expect

    rig = browser_rig
    inspect(rig)
    expect(rig.page.locator("#recoveryState")).to_have_text("RESULT RECOVERABLE")
    assert not any(method == "POST" for method, _, _ in rig.state["requests"])
    rig.page.on("dialog", lambda dialog: dialog.accept())
    rig.page.locator("#resumeSavedResultBtn").click()
    expect(rig.page.locator("#recoveryState")).to_have_text("PREPARED")
    expect(rig.page.locator("#resumeSavedResultBtn")).to_be_hidden()
    posts = [entry for entry in rig.state["requests"] if entry[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1] == "/v1/tasks/task_browser/planning/resume-saved"
    assert posts[0][2] == {"schema_version": "safe_agent.resume_saved_result.v1",
                         "expected_state_version": 0, "expected_workspace_version": 1,
                         "expected_recovery_revision": "sha256:" + "a" * 64}


@pytest.mark.parametrize("state", ["INFERENCE_UNCERTAIN", "INFERENCE_PENDING", "RESULT_EXPIRED",
                                    "RESULT_MISSING", "AUTHORITY_CHANGED", "SOURCE_CHANGED",
                                    "POLICY_CHANGED", "NEEDS_CLARIFICATION", "WAITING_TO_DISPATCH"])
def test_blocked_statuses_never_offer_resume(browser_rig, state):
    from playwright.sync_api import expect

    rig = browser_rig
    rig.state["status"] = state
    inspect(rig)
    expect(rig.page.locator("#recoveryState")).to_have_text(state.replace("_", " "))
    expect(rig.page.locator("#resumeSavedResultBtn")).to_be_hidden()
    assert not any(method == "POST" for method, _, _ in rig.state["requests"])


def test_session_is_memory_only_and_clear_removes_details(browser_rig):
    from playwright.sync_api import expect

    rig = browser_rig
    inspect(rig)
    expect(rig.page.locator("#recoveryResult")).to_be_visible()
    assert rig.page.locator("#recoveryToken").input_value() == ""
    assert TOKEN not in rig.page.content() and TOKEN not in rig.page.url
    assert rig.page.evaluate("window.storageAccesses") == []
    assert rig.context.cookies() == []
    rig.page.locator("#clearRecoveryBtn").click()
    expect(rig.page.locator("#recoveryResult")).to_be_hidden()
    assert rig.page.locator("#recoveryState").inner_text() == ""


def test_stale_response_cannot_reenable_previous_task_action(browser_rig):
    from playwright.sync_api import expect

    rig = browser_rig
    rig.state["hold"] = True
    inspect(rig)
    assert rig.state["started"] is True
    rig.page.locator("#recoveryTask").fill("task_other")
    rig.state["hold"] = False
    rig.page.evaluate("window.releasePending()")
    rig.page.wait_for_timeout(200)
    expect(rig.page.locator("#recoveryResult")).to_be_hidden()
    expect(rig.page.locator("#resumeSavedResultBtn")).to_be_hidden()
    rig.page.locator("#inspectRecoveryBtn").click()
    expect(rig.page.locator("#recoveryResult")).to_be_visible()
    rig.page.on("dialog", lambda dialog: dialog.accept())
    rig.page.locator("#resumeSavedResultBtn").click()
    expect(rig.page.locator("#recoveryState")).to_have_text("PREPARED")
    posts = [entry for entry in rig.state["requests"] if entry[0] == "POST"]
    assert len(posts) == 1 and "task_other" in posts[0][1]


def test_unexpected_action_or_unsafe_flags_disable_recovery(browser_rig):
    from playwright.sync_api import expect

    rig = browser_rig
    rig.state["patch"] = {"allowed_actions": ["refresh", "approve", "resume_saved_result"]}
    inspect(rig)
    expect(rig.page.locator("#recoveryStatus")).to_contain_text("Unexpected recovery response")
    expect(rig.page.locator("#resumeSavedResultBtn")).to_be_hidden()


def test_lost_or_conflicting_resume_response_requires_explicit_refresh(browser_rig):
    from playwright.sync_api import expect

    rig = browser_rig
    rig.state["post_code"] = 409
    inspect(rig)
    expect(rig.page.locator("#resumeSavedResultBtn")).to_be_visible()
    rig.page.on("dialog", lambda dialog: dialog.accept())
    rig.page.locator("#resumeSavedResultBtn").click()
    expect(rig.page.locator("#recoveryStatus")).to_contain_text("Refresh status")
    assert "UNTRUSTED_EXCEPTION_MARKER" not in rig.page.content()
    expect(rig.page.locator("#resumeSavedResultBtn")).to_be_hidden()
    assert len([x for x in rig.state["requests"] if x[0] == "POST"]) == 1


def test_wrong_task_response_and_html_content_are_not_actionable(browser_rig):
    from playwright.sync_api import expect

    rig = browser_rig
    rig.state["patch"] = {"task_id": "task_other", "task_phase": "<script>window.bad=true</script>"}
    inspect(rig)
    expect(rig.page.locator("#recoveryStatus")).to_contain_text("Unexpected recovery response")
    assert rig.page.evaluate("window.bad === undefined")
    expect(rig.page.locator("#resumeSavedResultBtn")).to_be_hidden()


def test_invalid_token_clears_session_without_retry(browser_rig):
    from playwright.sync_api import expect

    rig = browser_rig
    rig.page.locator("#recoveryToken").fill("invalid-fixture-token")
    rig.page.locator("#recoveryTask").fill("task_browser")
    rig.page.locator("#inspectRecoveryBtn").click()
    expect(rig.page.locator("#recoveryStatus")).to_contain_text("Session cleared")
    expect(rig.page.locator("#resumeSavedResultBtn")).to_be_hidden()
    assert rig.page.locator("#recoveryTask").input_value() == ""
