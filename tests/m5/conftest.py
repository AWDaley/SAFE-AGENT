"""Google Calendar API wire simulator. No test ever contacts Google."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from safe_agent.canonical import sha256
from safe_agent.contracts.control import Task, TaskManifest
from safe_agent.integrations.google_calendar.client import GoogleCalendarHTTP
from safe_agent.integrations.google_calendar.oauth import (
    GOOGLE_SCOPES, GoogleCredentialVault, GoogleOAuthService, OAuthClientConfig,
)
from safe_agent.integrations.google_calendar.service import GoogleTestCalendarService
from safe_agent.security.principal import Principal
from safe_agent.storage.repository import LedgerRepository

NOW = datetime.fromisoformat("2026-10-14T13:00:00-04:00")
START = datetime.fromisoformat("2026-10-15T13:00:00-04:00")
UNTIL = START + timedelta(hours=3)
CALENDAR = "sandbox-only@group.calendar.google.com"


class SimulatedGoogle:
    def __init__(self) -> None:
        self.events: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, str]] = []
        self.insert_calls = 0
        self.auth_calls = 0
        self.revoke_calls = 0
        self.scopes = " ".join(sorted(GOOGLE_SCOPES))
        self.fail: str | None = None
        self.busy = [
            {"start": "2026-10-15T17:30:00Z", "end": "2026-10-15T18:00:00Z"},
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.host == "oauth2.googleapis.com":
            if request.url.path == "/revoke":
                self.revoke_calls += 1
                if self.fail == "revoke":
                    raise httpx.ConnectError("network disconnected", request=request)
                return httpx.Response(200, json={})
            if request.url.path == "/token":
                self.auth_calls += 1
                if self.fail == "oauth":
                    return httpx.Response(400, json={"error": "invalid_grant", "sensitive": "secret"})
                form = request.content.decode()
                if "authorization_code" in form:
                    return httpx.Response(200, json={
                        "scope": self.scopes, "refresh_token": "test_refresh_token_abcdef",
                        "access_token": "test_access_token_abc123", "expires_in": 3600,
                    })
                return httpx.Response(200, json={
                    "scope": self.scopes, "access_token": "test_access_token_abc123",
                    "expires_in": 3600,
                })
        if request.url.host != "www.googleapis.com":
            raise AssertionError("unexpected host")
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return httpx.Response(401, json={"error": "missing_token"})
        if self.fail == "401":
            return httpx.Response(401, json={"error": "secret private content"})
        if self.fail == "429":
            return httpx.Response(429, json={"error": "overquota"})
        if request.url.path.endswith("/freeBusy"):
            payload = json.loads(request.content)
            if payload["items"] != [{"id": CALENDAR}]:
                raise AssertionError("not test calendar")
            return httpx.Response(200, json={"calendars": {CALENDAR: {
                "errors": [], "busy": self.busy,
            }}})
        if request.url.path.endswith("/events") and request.method == "POST":
            self.insert_calls += 1
            event = json.loads(request.content)
            if event["id"] in self.events:
                return httpx.Response(409, json={"error": "duplicate"})
            event["status"] = "confirmed"
            self.events[event["id"]] = event
            if self.fail == "after_insert":
                raise httpx.ReadTimeout("lost provider response", request=request)
            return httpx.Response(200, json=event)
        if "/events/" in request.url.path and request.method == "GET":
            eid = request.url.path.split("/events/")[-1]
            event = self.events.get(eid)
            return httpx.Response(200, json=event) if event else httpx.Response(404)
        raise AssertionError("unsupported simulator endpoint " + request.url.path)


@pytest.fixture
def google_kit(tmp_path):
    repo = LedgerRepository.testing_sqlite(
        f"sqlite+pysqlite:///{tmp_path / 'm5_test.sqlite'}"
    )
    principal = Principal(
        organization_id="org_demo", actor_id="employee_demo",
        scopes=("connection:manage", "connection:read", "task:read"),
        authenticated_until=NOW + timedelta(days=3),
    )
    simulator = SimulatedGoogle()
    http = httpx.Client(transport=httpx.MockTransport(simulator), trust_env=False)
    vault = GoogleCredentialVault(repo.engine, fernet_key=Fernet.generate_key(),
                                  allowed_test_calendar_ids=(CALENDAR,))
    oauth = GoogleOAuthService(vault, OAuthClientConfig(
        client_id="test_oauth_app", client_secret="test_client_secret",
        redirect_uri="http://127.0.0.1:8000/oauth/google/callback",
    ), http)
    api = GoogleCalendarHTTP(http)
    service = GoogleTestCalendarService(oauth, api, repo)
    manifest = TaskManifest(
        manifest_id="manifest_m5", manifest_version=1,
        organization_id="org_demo", initiator_id="employee_demo", task_id="task_m5",
        calendar_ref="ref_calendar_m5", customer_ref="ref_customer_m5",
        permitted_operations=("calendar.create_event",),
        allowed_start=START, allowed_end=UNTIL,
        time_zone="America/New_York", template_id="follow_up_v1",
    )
    task = Task(
        task_id="task_m5", organization_id="org_demo", initiator_id="employee_demo",
        manifest_id="manifest_m5", manifest_hash=sha256(manifest),
        objective_code="schedule_follow_up", created_at=NOW - timedelta(minutes=5),
        deadline=NOW + timedelta(days=2),
    )
    repo.create_task(task, manifest, actor="trusted_operator", now=NOW)
    connection = vault.save(principal=principal, refresh_token="test_refresh_token_abcdef",
                            calendar_id=CALENDAR, now=NOW)
    yield {
        "repo": repo, "principal": principal, "sim": simulator, "http": http,
        "vault": vault, "oauth": oauth, "api": api, "service": service,
        "connection": connection, "task": task, "manifest": manifest,
    }
    http.close()
    repo.close()
