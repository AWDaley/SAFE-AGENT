"""Entirely offline M5 demonstration using an HTTP simulator and synthetic OAuth."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
from cryptography.fernet import Fernet

from safe_agent.canonical import sha256
from safe_agent.contracts.control import Task, TaskManifest
from safe_agent.integrations.google_calendar import (
    GoogleCalendarHTTP, GoogleCredentialVault, GoogleOAuthService,
    GoogleTestCalendarService, OAuthClientConfig,
)
from safe_agent.integrations.google_calendar.errors import GoogleTestWriteBlocked
from safe_agent.integrations.google_calendar.oauth import GOOGLE_SCOPES
from safe_agent.security.principal import Principal
from safe_agent.storage.repository import LedgerRepository


def print_m5_demo() -> None:
    now = datetime.fromisoformat("2026-10-14T13:00:00-04:00")
    start = now + timedelta(days=1)
    end = start + timedelta(hours=3)
    # Only synthetic identifiers/credentials; no Google network or OAuth prompts.
    test_calendar = "synthetic-test@group.calendar.google.com"

    def fake_google(req: httpx.Request) -> httpx.Response:
        if req.url.host == "oauth2.googleapis.com" and req.url.path == "/token":
            return httpx.Response(200, json={
                "access_token": "synthetic_test_access_token",
                "scope": " ".join(sorted(GOOGLE_SCOPES)),
            })
        if req.url.host == "www.googleapis.com" and req.url.path.endswith("/freeBusy"):
            return httpx.Response(200, json={
                "calendars": {test_calendar: {"busy": [{
                    "start": (start + timedelta(minutes=30)).isoformat(),
                    "end": (start + timedelta(minutes=60)).isoformat(),
                }], "errors": []}},
            })
        raise RuntimeError("M5 demo prohibits any other HTTP request")

    http = httpx.Client(transport=httpx.MockTransport(fake_google), trust_env=False)
    repo = LedgerRepository.testing_sqlite()
    try:
        principal = Principal(
            organization_id="org_demo", actor_id="employee_demo",
            scopes=("connection:manage", "connection:read", "task:read"),
            authenticated_until=now + timedelta(days=3),
        )
        manifest = TaskManifest(
            manifest_id="manifest_m5_demo", manifest_version=1,
            organization_id="org_demo", initiator_id="employee_demo",
            task_id="task_m5_demo", calendar_ref="ref_demo_calendar",
            customer_ref="ref_demo_customer",
            permitted_operations=("calendar.create_event",),
            allowed_start=start, allowed_end=end,
            time_zone="America/New_York", template_id="follow_up_v1",
        )
        task = Task(
            task_id=manifest.task_id, organization_id="org_demo",
            initiator_id="employee_demo", manifest_id=manifest.manifest_id,
            manifest_hash=sha256(manifest), objective_code="schedule_follow_up",
            created_at=now - timedelta(minutes=5), deadline=now + timedelta(days=2),
        )
        repo.create_task(task, manifest, actor="trusted_demo", now=now)
        vault = GoogleCredentialVault(repo.engine, fernet_key=Fernet.generate_key(),
                                     allowed_test_calendar_ids=(test_calendar,))
        connection = vault.save(principal=principal,
                                refresh_token="synthetic_test_refresh_token",
                                calendar_id=test_calendar, now=now)
        oauth = GoogleOAuthService(vault, OAuthClientConfig(
            client_id="synthetic_demo_app", client_secret="synthetic_demo_secret",
            redirect_uri="http://127.0.0.1:8765/callback",
        ), http)
        service = GoogleTestCalendarService(oauth, GoogleCalendarHTTP(http), repo)
        observed = service.observe(
            principal=principal, connection_ref=connection, task_id=task.task_id,
            calendar_ref=manifest.calendar_ref, from_at=start, until_at=end, now=now,
        )
        denied = False
        try:
            service.execute_model_proposal("calendar.create_event")
        except GoogleTestWriteBlocked:
            denied = True
        print(json.dumps({
            "status": "M5_OFFLINE_GOOGLE_TEST_ADAPTER",
            "provider_requests": "HTTP_SIMULATOR_ONLY",
            "sanitized_free_slots": len(observed.free_slots),
            "projection_provenance": observed.provenance,
            "model_write_denied": denied,
            "upstream_tal_calendar_domain_certified": False,
            "external_effects": repo.replay_task(task.task_id).external_effects,
            "credentials_or_provider_ids_exposed": False,
        }, indent=2))
    finally:
        http.close()
        repo.close()
