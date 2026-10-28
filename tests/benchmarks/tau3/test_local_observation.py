"""Bounded local observation over mocked HTTP; no model or server is launched."""

import hashlib
import json
import stat
import threading
import time

import httpcore
import httpx
import pytest

from safe_agent.benchmarks.tau3 import local_observation as observation
from safe_agent.inference import deadline_http

BASE = "http://127.0.0.1:11434/v1"
SECRET = "SECRET-PROMPT-CREDENTIAL-OR-ERROR"


def mock_http(monkeypatch, replies):
    """Keep real HTTPX routing and deadline transport; replace only its I/O pool."""
    requests, pools, clients = [], [], []
    real_client = httpx.Client

    def client(*args, **kwargs):
        clients.append(kwargs)
        return real_client(*args, **kwargs)

    class Pool:
        def __init__(self, **kwargs):
            pools.append(kwargs)
            self.budget = kwargs["network_backend"].budget

        def handle_request(self, request):
            requests.append(request)
            reply = replies[min(len(requests) - 1, len(replies) - 1)]
            if callable(reply):
                return reply(self.budget)
            if isinstance(reply, Exception):
                raise reply
            return reply

        def close(self):
            pass

    monkeypatch.setattr(observation.httpx, "Client", client)
    monkeypatch.setattr(deadline_http.httpcore, "ConnectionPool", Pool)
    return requests, pools, clients


def response(value, *, status=200, headers=()):
    return httpcore.Response(status, headers=headers, content=json.dumps(value).encode())


def finish(observer):
    assert observer._thread is not None
    observer._thread.join(timeout=3)
    assert not observer._thread.is_alive()


def read_samples(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("next_array", [False, True])
def test_only_safe_fields_are_persisted_with_numeric_loopback_read(monkeypatch, tmp_path, next_array):
    next_token = {"n_decoded": 4, "n_remain": 2044, "stopping_word": SECRET}
    slots = [{"id": 0, "id_task": 7, "n_ctx": 32768, "is_processing": True,
              "n_prompt_tokens": 812, "n_prompt_tokens_processed": 700,
              "n_prompt_tokens_cache": 112, "prompt": SECRET, "generated": SECRET,
              "next_token": [next_token] if next_array else next_token,
              "params": {"n_predict": 2048, "seed": 300, "temperature": 0.0,
                         "chat_format": "peg-simple", "grammar": SECRET}}]
    requests, pools, clients = mock_http(monkeypatch, [response(slots)])
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, SECRET)
    path = tmp_path / "slots.jsonl"
    with observation.LocalLlamaObserver(BASE, path, max_samples=1) as observer:
        finish(observer)
    assert len(requests) == len(pools) == len(clients) == 1
    request = requests[0]
    assert request.method == b"GET" and request.url.host == b"127.0.0.1"
    assert request.url.target == b"/slots" and request.url.port == 11434
    headers = {k.lower(): v for k, v in request.headers}
    assert b"authorization" not in headers and b"cookie" not in headers
    assert headers[b"accept-encoding"] == b"identity"
    assert clients[0]["trust_env"] is clients[0]["follow_redirects"] is False
    assert pools[0]["retries"] == 0 and pools[0]["max_connections"] == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_bytes()
    assert SECRET.encode() not in raw
    row = read_samples(path)[0]
    assert row["status"] == "observed" and row["error_code"] is None
    assert row["slots"][0]["next_token"] == {"n_decoded": 4, "n_remain": 2044}
    assert row["slots"][0]["unavailable_fields"] == {}
    assert 0 <= row["started_elapsed_seconds"] <= row["finished_elapsed_seconds"]
    assert row["duration_seconds"] == pytest.approx(
        row["finished_elapsed_seconds"] - row["started_elapsed_seconds"])
    summary = observer.summary()
    assert summary["samples"] == summary["observed_samples"] == 1 and summary["errors"] == 0
    assert summary["samples_sha256"] == hashlib.sha256(raw).hexdigest()
    assert summary["diagnostic_only"] and not summary["eligible_for_qualification"]
    assert summary["inference_requests"] == 0 and summary["stop_reason"] == "sample_limit"


def test_absent_counters_and_negative_sentinels_remain_unknown(monkeypatch, tmp_path):
    mock_http(monkeypatch, [response([{"id": 0, "id_task": -1, "is_processing": False,
                                     "next_token": [{"n_remain": -1}],
                                     "params": {"n_predict": -1, "chat_format": SECRET}}])])
    path = tmp_path / "partial.jsonl"
    with observation.LocalLlamaObserver(BASE, path, max_samples=1) as observer:
        finish(observer)
    slot = read_samples(path)[0]["slots"][0]
    assert "id_task" not in slot and "n_prompt_tokens_processed" not in slot
    assert slot["next_token"] == {} and slot["params"] == {}
    assert slot["unavailable_fields"]["n_prompt_tokens_processed"] == "ABSENT"
    assert slot["unavailable_fields"]["next_token.n_remain"] == "NEGATIVE_SENTINEL"
    assert slot["unavailable_fields"]["params.chat_format"] == "UNSUPPORTED_LABEL"
    assert SECRET not in path.read_text()


def test_task_identity_is_retained_without_carrying_counters_across_samples(monkeypatch, tmp_path):
    requests, _, _ = mock_http(monkeypatch, [
        response([{"id": 0, "id_task": 42, "n_prompt_tokens_processed": 812,
                   "next_token": {"n_decoded": 10}}]),
        response([{"id": 0, "id_task": 43, "next_token": [{"n_decoded": 1}]}]),
    ])
    path = tmp_path / "tasks.jsonl"
    with observation.LocalLlamaObserver(BASE, path, interval_seconds=1, max_samples=2) as observer:
        finish(observer)
    assert len(requests) == 2
    first, second = [row["slots"][0] for row in read_samples(path)]
    assert (first["id_task"], second["id_task"]) == (42, 43)
    assert (first["next_token"]["n_decoded"], second["next_token"]["n_decoded"]) == (10, 1)
    assert "n_prompt_tokens_processed" not in second


@pytest.mark.parametrize("value", [
    {}, [None], [{}], [{"id": True}], [{"id": 0}, {"id": 0}],
    [{"id": 0, "n_ctx": -1}], [{"id": 0, "n_prompt_tokens_processed": "12"}],
    [{"id": 0, "next_token": []}], [{"id": 0, "next_token": [{}, {}]}],
    [{"id": 0, "params": []}], [{"id": 0, "is_processing": 1}],
    [{"id": 0, "params": {"temperature": float("nan")}}],
    [{"id": 0, "next_token": {"n_decoded": -1}}],
])
def test_malformed_or_invalid_numeric_payloads_have_fixed_errors(monkeypatch, tmp_path, value):
    mock_http(monkeypatch, [response(value)])
    path = tmp_path / "invalid.jsonl"
    with observation.LocalLlamaObserver(BASE, path, max_samples=1) as observer:
        finish(observer)
    row = read_samples(path)[0]
    assert row["slots"] is None and row["error_code"] == "LOCAL_OBSERVATION_RESPONSE_INVALID"
    assert observer.summary()["errors"] == 1


@pytest.mark.parametrize("body,headers,code", [
    (b'[{"id":0,"id":1}]', (), "LOCAL_OBSERVATION_RESPONSE_INVALID"),
    (b'\xff', (), "LOCAL_OBSERVATION_RESPONSE_INVALID"),
    (b" " * (observation.MAX_RESPONSE_BYTES + 1), (), "LOCAL_OBSERVATION_RESPONSE_LIMIT"),
    (b"[]", ((b"content-length", b"262145"),), "LOCAL_OBSERVATION_RESPONSE_LIMIT"),
    (b"[]", ((b"content-encoding", b"gzip"),), "LOCAL_OBSERVATION_CONTENT_ENCODING_DENIED"),
])
def test_strict_json_and_response_byte_bound(monkeypatch, tmp_path, body, headers, code):
    mock_http(monkeypatch, [httpcore.Response(200, headers=headers, content=body)])
    path = tmp_path / "bounded.jsonl"
    with observation.LocalLlamaObserver(BASE, path, max_samples=1) as observer:
        finish(observer)
    assert read_samples(path)[0]["error_code"] == code


@pytest.mark.parametrize("reply,code", [
    (response({}, status=307, headers=((b"location", b"https://example.invalid/secret"),)),
     "LOCAL_OBSERVATION_HTTP_STATUS"),
    (httpcore.ConnectError(SECRET), "LOCAL_OBSERVATION_TRANSPORT_ERROR"),
    (httpcore.ReadTimeout(SECRET), "LOCAL_OBSERVATION_TIMEOUT"),
])
def test_no_redirect_retry_or_private_error_text(monkeypatch, tmp_path, reply, code):
    requests, _, _ = mock_http(monkeypatch, [reply])
    path = tmp_path / "error.jsonl"
    with observation.LocalLlamaObserver(BASE, path, max_samples=1) as observer:
        finish(observer)
        diagnostic_result = "unchanged"
    assert diagnostic_result == "unchanged" and len(requests) == 1
    assert read_samples(path)[0]["error_code"] == code
    assert SECRET not in path.read_text() and SECRET not in json.dumps(observer.summary())


def test_stop_cancels_current_poll_and_preserves_body_exception(monkeypatch, tmp_path):
    entered = threading.Event()

    def blocked(budget):
        entered.set()
        while not budget.cancelled:
            budget.remaining()
            time.sleep(0.002)
        raise httpcore.ReadError(SECRET)

    requests, _, _ = mock_http(monkeypatch, [blocked])
    observer = observation.LocalLlamaObserver(BASE, tmp_path / "cancel.jsonl", timeout_seconds=0.2)
    with pytest.raises(RuntimeError, match="diagnostic body failure"), observer:
        assert entered.wait(timeout=1)
        started = time.monotonic()
        raise RuntimeError("diagnostic body failure")
    assert time.monotonic() - started < 0.5
    assert not observer.summary()["worker_alive"] and len(requests) == 1
    assert observer.summary()["error_counts"] == {"LOCAL_OBSERVATION_CANCELLED": 1}


def test_stop_interrupts_long_sampling_interval(monkeypatch, tmp_path):
    requests, _, _ = mock_http(monkeypatch, [response([])])
    with observation.LocalLlamaObserver(BASE, tmp_path / "interval.jsonl", interval_seconds=30) as observer:
        until = time.monotonic() + 1
        while observer.summary()["samples"] == 0 and time.monotonic() < until:
            time.sleep(0.002)
        assert observer.summary()["samples"] == 1
        started = time.monotonic()
    assert time.monotonic() - started < 0.5
    assert not observer.summary()["worker_alive"] and len(requests) == 1


def test_thread_start_failure_is_observational_only(monkeypatch, tmp_path):
    def fail(_):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(observation.threading.Thread, "start", fail)
    with observation.LocalLlamaObserver(BASE, tmp_path / "start.jsonl") as observer:
        result = 19
    assert result == 19 and observer.summary()["error_counts"] == {"LOCAL_OBSERVATION_START_FAILED": 1}
    assert SECRET not in json.dumps(observer.summary())


def test_trickled_response_cannot_reset_absolute_sample_deadline(monkeypatch, tmp_path):
    def chunks():
        for _ in range(20):
            time.sleep(0.01)
            yield b" "

    requests, _, _ = mock_http(monkeypatch, [httpcore.Response(200, content=chunks())])
    path = tmp_path / "trickle.jsonl"
    with observation.LocalLlamaObserver(BASE, path, timeout_seconds=0.04, max_samples=1) as observer:
        finish(observer)
    assert len(requests) == 1 and read_samples(path)[0]["error_code"] == "LOCAL_OBSERVATION_TIMEOUT"
    assert not observer.summary()["worker_alive"]


def test_recording_failure_does_not_change_diagnostic_outcome(monkeypatch, tmp_path):
    original = observation._new_file

    class BrokenWriter:
        def __init__(self, path):
            self.stream = original(path)

        def write(self, _):
            raise OSError(SECRET)

        def flush(self):
            self.stream.flush()

        def close(self):
            self.stream.close()

    monkeypatch.setattr(observation, "_new_file", BrokenWriter)
    mock_http(monkeypatch, [response([])])
    path = tmp_path / "write-error.jsonl"
    with observation.LocalLlamaObserver(BASE, path, max_samples=1) as observer:
        finish(observer)
        result = 23
    summary = observer.summary()
    assert result == 23 and summary["samples"] == 0 and summary["attempted_samples"] == 1
    assert summary["error_counts"] == {"LOCAL_OBSERVATION_WRITE_FAILED": 1}
    assert not summary["recording_complete"] and summary["samples_sha256"] is None
    assert SECRET not in json.dumps(summary) and path.read_bytes() == b""


@pytest.mark.parametrize("changes", [
    {"interval_seconds": 0.9}, {"interval_seconds": 31}, {"interval_seconds": float("nan")},
    {"timeout_seconds": 0}, {"timeout_seconds": 6}, {"timeout_seconds": float("inf")},
    {"max_samples": 0}, {"max_samples": 513}, {"max_samples": True},
])
def test_configuration_bounds_are_preconditions(tmp_path, changes):
    with pytest.raises(observation.LocalObservationError, match="BOUNDS_INVALID"):
        observation.LocalLlamaObserver(BASE, tmp_path / "unused.jsonl", **changes)
    assert not (tmp_path / "unused.jsonl").exists()


@pytest.mark.parametrize("base", [
    "http://localhost:11434/v1", "https://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v2",
    "http://127.0.0.1:11434/v1/", "http://secret@127.0.0.1:11434/v1",
])
def test_only_declared_numeric_loopback_v1_origin_is_allowed(tmp_path, base):
    with pytest.raises(observation.LocalObservationError, match="API_BASE_DENIED"):
        observation.LocalLlamaObserver(base, tmp_path / "unused.jsonl")


def test_existing_files_and_symlink_components_are_never_followed(tmp_path):
    existing = tmp_path / "existing.jsonl"
    existing.write_text("keep me")
    link = tmp_path / "link.jsonl"
    link.symlink_to(existing)
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    for target in (existing, link, alias / "new.jsonl", actual / ".." / "escape.jsonl"):
        with pytest.raises(observation.LocalObservationError, match="OUTPUT_INVALID"):
            with observation.LocalLlamaObserver(BASE, target):
                pytest.fail("Invalid output path started observation")
    assert existing.read_text() == "keep me" and not list(actual.iterdir())
