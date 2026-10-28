"""Opt-in, bounded observation of a declared local llama.cpp server.

Only numeric slot state and pinned format labels are retained. Polling is not
inference, qualification evidence, or proof of progress between absent samples.
The server may queue this read behind model work; a timeout leaves counters
unknown. Observation errors never replace the diagnostic body's outcome.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import Counter
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Literal

import httpcore
import httpx

from safe_agent.contracts.episode_inference import strict_json, validate_local_api_base, wire_json
from safe_agent.inference.deadline_http import DeadlineBudget, DeadlineHTTPTransport

MAX_RESPONSE_BYTES = 262_144
MAX_SAMPLES = 512
_CHAT_FORMATS = frozenset({
    "Content-only", "peg-simple", "peg-native", "peg-gemma4", "peg-minimax-m3",
})
_SLOT_COUNTERS = (
    "n_ctx", "id_task", "n_prompt_tokens", "n_prompt_tokens_processed", "n_prompt_tokens_cache",
)
_POLL_ERRORS = frozenset({
    "LOCAL_OBSERVATION_HTTP_STATUS", "LOCAL_OBSERVATION_CONTENT_ENCODING_DENIED",
    "LOCAL_OBSERVATION_RESPONSE_INVALID", "LOCAL_OBSERVATION_RESPONSE_LIMIT",
})


class LocalObservationError(ValueError):
    """Fixed observer codes only; never response or operating-system error text."""


def _integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_INVALID")
    return value


def _counter(source: dict[str, Any], target: dict[str, Any], name: str,
             unavailable: dict[str, str], *, prefix: str = "", sentinel: bool = False) -> None:
    field = prefix + name
    if name not in source or source[name] is None:
        unavailable[field] = "ABSENT" if name not in source else "NULL"
    elif sentinel and type(source[name]) is int and source[name] == -1:
        unavailable[field] = "NEGATIVE_SENTINEL"
    else:
        target[name] = _integer(source[name])


def _slots(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 64:
        raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_INVALID")
    result = []
    ids: set[int] = set()
    for raw in value:
        if not isinstance(raw, dict) or "id" not in raw:
            raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_INVALID")
        identity = _integer(raw["id"])
        if identity in ids:
            raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_INVALID")
        ids.add(identity)
        slot: dict[str, Any] = {"id": identity}
        unavailable: dict[str, str] = {}

        for name in _SLOT_COUNTERS:
            _counter(raw, slot, name, unavailable, sentinel=name == "id_task")
        if "is_processing" not in raw:
            unavailable["is_processing"] = "ABSENT"
        elif type(raw["is_processing"]) is not bool:
            raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_INVALID")
        else:
            slot["is_processing"] = raw["is_processing"]
        next_token = raw.get("next_token", {})
        if isinstance(next_token, list) and len(next_token) == 1:
            next_token = next_token[0]  # Pinned b11936 uses a one-item array.
        if not isinstance(next_token, dict):
            raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_INVALID")
        slot["next_token"] = {}
        for name in ("n_decoded", "n_remain"):
            _counter(next_token, slot["next_token"], name, unavailable, prefix="next_token.",
                     sentinel=name == "n_remain")
        params = raw.get("params", {})
        if not isinstance(params, dict):
            raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_INVALID")
        slot["params"] = {}
        for name in ("n_predict", "seed"):
            _counter(params, slot["params"], name, unavailable, prefix="params.",
                     sentinel=name == "n_predict")
        temperature = params.get("temperature")
        if temperature is None:
            unavailable["params.temperature"] = "ABSENT" if "temperature" not in params else "NULL"
        elif type(temperature) not in (int, float) or not 0 <= temperature <= 1e308:
            raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_INVALID")
        else:
            slot["params"]["temperature"] = temperature
        chat_format = params.get("chat_format")
        if isinstance(chat_format, str) and chat_format in _CHAT_FORMATS:
            slot["params"]["chat_format"] = chat_format
        else:
            # Never retain arbitrary strings supplied in this optional field.
            unavailable["params.chat_format"] = "ABSENT" if chat_format is None else "UNSUPPORTED_LABEL"
        slot["unavailable_fields"] = unavailable
        result.append(slot)
    return result


def _new_file(path: Path) -> BinaryIO:
    """Walk existing directories with openat/O_NOFOLLOW, then create exclusively."""
    directory = -1
    output = -1
    try:
        if ".." in path.parts or not path.name:
            raise OSError
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory = os.open(path.anchor, flags)
        for part in path.parts[1:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        output = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600, dir_fd=directory)
        os.fchmod(output, 0o600)
        stream = os.fdopen(output, "wb")
        output = -1
        return stream
    except (OSError, ValueError):
        raise LocalObservationError("LOCAL_OBSERVATION_OUTPUT_INVALID") from None
    finally:
        if output >= 0:
            os.close(output)
        if directory >= 0:
            os.close(directory)


class LocalLlamaObserver:
    def __init__(self, api_base: str, output_path: Path, *, interval_seconds: float = 5,
                 timeout_seconds: float = 2, max_samples: int = MAX_SAMPLES):
        try:
            self.endpoint = validate_local_api_base(api_base).removesuffix("/v1") + "/slots"
        except ValueError:
            raise LocalObservationError("LOCAL_OBSERVATION_API_BASE_DENIED") from None
        if (type(interval_seconds) not in (int, float) or not 1 <= interval_seconds <= 30
                or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 5
                or type(max_samples) is not int or not 1 <= max_samples <= MAX_SAMPLES):
            raise LocalObservationError("LOCAL_OBSERVATION_BOUNDS_INVALID")
        try:
            self.output_path = Path(output_path).absolute()
        except (TypeError, ValueError, OSError):
            raise LocalObservationError("LOCAL_OBSERVATION_OUTPUT_INVALID") from None
        self.interval_seconds = float(interval_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.max_samples = max_samples
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._budget: DeadlineBudget | None = None
        self._stream: BinaryIO | None = None
        self._digest = hashlib.sha256()
        self._errors: Counter[str] = Counter()
        self._attempts = 0
        self._samples = 0
        self._observed = 0
        self._started: float | None = None
        self._reason = "not_started"
        self._recording_complete = True

    def __enter__(self) -> LocalLlamaObserver:
        if self._started is not None:
            raise LocalObservationError("LOCAL_OBSERVATION_ALREADY_USED")
        self._stream = _new_file(self.output_path)
        self._started = time.monotonic()
        self._reason = "observing"
        try:
            self._thread = threading.Thread(target=self._run, name="safe-local-slot-observer",
                                            daemon=True)
            self._thread.start()
        except Exception:
            self._error("LOCAL_OBSERVATION_START_FAILED")
            self._reason = "start_failed"
            self._close()
        return self

    def _error(self, code: str) -> None:
        with self._lock:
            self._errors[code] += 1

    def _poll(self, budget: DeadlineBudget) -> list[dict[str, Any]]:
        with httpx.Client(transport=DeadlineHTTPTransport(budget), trust_env=False,
                          follow_redirects=False, timeout=self.timeout_seconds,
                          headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as client:
            with client.stream("GET", self.endpoint) as response:
                if response.status_code != 200:
                    raise LocalObservationError("LOCAL_OBSERVATION_HTTP_STATUS")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise LocalObservationError("LOCAL_OBSERVATION_CONTENT_ENCODING_DENIED")
                length = response.headers.get("content-length")
                if length is not None:
                    if not length.isdecimal() or len(length) > 12:
                        raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_INVALID")
                    if int(length) > MAX_RESPONSE_BYTES:
                        raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_LIMIT")
                raw = bytearray()
                for block in response.iter_bytes(chunk_size=8192):
                    budget.remaining()
                    if len(raw) + len(block) > MAX_RESPONSE_BYTES:
                        raise LocalObservationError("LOCAL_OBSERVATION_RESPONSE_LIMIT")
                    raw.extend(block)
                budget.remaining()
                return _slots(strict_json(raw.decode("utf-8"), max_bytes=MAX_RESPONSE_BYTES))

    def _run(self) -> None:
        assert self._started is not None
        try:
            while not self._stop.is_set() and self._attempts < self.max_samples:
                started = time.monotonic()
                budget = DeadlineBudget(started + self.timeout_seconds)
                with self._lock:
                    self._budget = budget
                    self._attempts += 1
                    index = self._attempts
                slots = None
                error = None
                try:
                    slots = self._poll(budget)
                except LocalObservationError as exc:
                    error = str(exc) if str(exc) in _POLL_ERRORS else "LOCAL_OBSERVATION_RESPONSE_INVALID"
                except (httpx.TimeoutException, httpcore.TimeoutException, TimeoutError):
                    error = "LOCAL_OBSERVATION_TIMEOUT"
                except (ValueError, UnicodeError, RecursionError, OverflowError):
                    error = "LOCAL_OBSERVATION_RESPONSE_INVALID"
                except Exception:
                    error = "LOCAL_OBSERVATION_TRANSPORT_ERROR"
                finally:
                    with self._lock:
                        self._budget = None
                if budget.cancelled:
                    error, slots = "LOCAL_OBSERVATION_CANCELLED", None
                finished = time.monotonic()
                row = {"sample": index, "started_elapsed_seconds": started - self._started,
                       "finished_elapsed_seconds": finished - self._started,
                       "duration_seconds": finished - started,
                       "status": "observed" if error is None else "error",
                       "error_code": error, "slots": slots}
                if error is not None:
                    self._error(error)
                payload = (wire_json(row) + "\n").encode("utf-8")
                with self._lock:
                    if self._stream is None:
                        break
                    try:
                        self._stream.write(payload)
                        self._stream.flush()
                    except Exception:
                        self._errors["LOCAL_OBSERVATION_WRITE_FAILED"] += 1
                        self._recording_complete = False
                        self._reason = "write_failed"
                        break
                    self._digest.update(payload)
                    self._samples += 1
                    self._observed += error is None
                if self._attempts >= self.max_samples:
                    self._reason = "sample_limit"
                    break
                if self._stop.wait(max(0, started + self.interval_seconds - time.monotonic())):
                    break
        except Exception:
            self._error("LOCAL_OBSERVATION_WORKER_FAILED")
            self._reason = "worker_failed"
            self._recording_complete = False
        finally:
            if self._reason == "observing":
                self._reason = "stopped"
            self._close()

    def _close(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    self._errors["LOCAL_OBSERVATION_WRITE_FAILED"] += 1
                    self._recording_complete = False
                self._stream = None

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            budget = self._budget
        if budget is not None:
            budget.cancel()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=self.timeout_seconds + 0.25)
            if self._thread.is_alive():
                self._error("LOCAL_OBSERVATION_STOP_TIMEOUT")
                self._reason = "stop_timeout"
                self._recording_complete = False
        self._close()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> Literal[False]:
        try:
            self.stop()
        except Exception:
            self._error("LOCAL_OBSERVATION_STOP_FAILED")
        return False

    def summary(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {
                "schema_version": 1, "kind": "local_llama_observation", "diagnostic_only": True,
                "eligible_for_qualification": False, "inference_requests": 0,
                "raw_samples_path": str(self.output_path), "endpoint": self.endpoint,
                "interval_seconds": self.interval_seconds, "timeout_seconds": self.timeout_seconds,
                "max_samples": self.max_samples, "max_response_bytes": MAX_RESPONSE_BYTES,
                "attempted_samples": self._attempts, "samples": self._samples,
                "observed_samples": self._observed, "errors": sum(self._errors.values()),
                "error_counts": dict(self._errors), "stop_reason": self._reason,
                "worker_alive": running, "recording_complete": self._recording_complete,
                "samples_sha256": self._digest.hexdigest() if self._started is not None
                and not running and self._recording_complete else None,
            }
