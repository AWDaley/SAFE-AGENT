"""Opt-in development scheduling and explicit, private episode attribution.

The upstream per-task runner, retry wrapper, evaluator and checkpoint format are
unchanged. Only this explicitly selected development path schedules one episode
at a time, so an infrastructure failure can stop the remaining schedule safely.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .inventory import content_hash

INDEX_PATH = "private/episode-index.json"
INDEX_SCHEMA = "safe_agent.tau3_episode_index.v1"
SCHEDULER = "safe_development_serial_v1"
COUNTERS = frozenset(
    {
        "runtime_steps_started",
        "observed_user_messages",
        "observed_tool_results",
        "message_outputs",
        "tool_outputs",
        "consecutive_identical_messages",
        "max_consecutive_identical_messages",
        "messages_since_tool_result",
        "max_messages_since_tool_result",
    }
)
LIFECYCLE_EVENTS = frozenset(
    {
        "episode_bound",
        "episode_initialized",
        "runtime_step_started",
        "message_output",
        "tool_output",
        "cleanup_started",
        "cleanup_finished",
    }
)
ROW_STATES = frozenset({"not_run", "running", "recorded", "interrupted"})
TERMINATION_REASONS = frozenset(
    {
        "user_stop",
        "agent_stop",
        "max_steps",
        "timeout",
        "too_many_errors",
        "agent_error",
        "user_error",
        "infrastructure_error",
        "context_window_exceeded",
        "unexpected_error",
    }
)


def _graded(reward: object) -> bool:
    return (
        isinstance(reward, (int, float)) and not isinstance(reward, bool) and math.isfinite(reward)
    )


def _write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def public_execution(index: dict[str, Any]) -> dict[str, Any]:
    """Project reviewed fields only; never copy a transcript or private join ID."""
    counts: Counter[str] = Counter(row["state"] for row in index["rows"])
    return {
        "schema_version": "safe_agent.tau3_progress.v1",
        "scheduler": SCHEDULER,
        "status": index["status"],
        "stop_reason": index.get("stop_reason"),
        "scheduled": len(index["rows"]),
        "started": sum(row["state"] != "not_run" for row in index["rows"]),
        "recorded": counts["recorded"],
        "graded": sum(row["graded"] for row in index["rows"]),
        "not_run": counts["not_run"],
        "interrupted": counts["interrupted"],
        "infrastructure_errors": sum(
            row["termination_reason"] == "infrastructure_error" for row in index["rows"]
        ),
        "counter_scope": "adapter observations and outputs; runtime steps may contain multiple model attempts",
        "repetition_interpretation": "mechanical repetition counters do not establish role confusion or task progress",
        "pairs": [
            {
                "task_id": row["task_id"],
                "trial": row["trial"],
                "state": row["state"],
                "graded": row["graded"],
                "termination_reason": row["termination_reason"],
                "episode_binding_available": row["episode_id"] is not None,
                "runtime_initialized": row["runtime_initialized"],
                "last_lifecycle_event": row["last_lifecycle_event"],
                "elapsed_to_last_event_seconds": row["elapsed_to_last_event_seconds"],
                "counters": {name: row["counters"][name] for name in sorted(COUNTERS)},
            }
            for row in index["rows"]
        ],
    }


class EpisodeIndex:
    """Bind the current scheduled pair before SAFE starts its runtime episode.

    This callback receives no Task, text, tool arguments or hidden evaluator
    data. A null episode ID means no SAFE episode was bound, not a guessed join.
    """

    def __init__(self, output_dir: Path, manifest: dict[str, Any]):
        self.output_dir = output_dir
        (output_dir / "private").mkdir(mode=0o700, exist_ok=True)
        (output_dir / "private").chmod(0o700)
        self.data: dict[str, Any] = {
            "schema_version": INDEX_SCHEMA,
            "scheduler": SCHEDULER,
            "run_id": manifest["run_id"],
            "config_sha256": manifest["config_sha256"],
            "status": "preparing",
            "stop_reason": None,
            "rows": [
                {
                    **pair,
                    "state": "not_run",
                    "seed": None,
                    "episode_id": None,
                    "runtime_initialized": False,
                    "simulation_id": None,
                    "simulation_sha256": None,
                    "termination_reason": None,
                    "graded": False,
                    "started_at": None,
                    "last_event_at": None,
                    "last_lifecycle_event": None,
                    "elapsed_to_last_event_seconds": None,
                    "counters": dict.fromkeys(sorted(COUNTERS), 0),
                }
                for pair in manifest["scheduled"]
            ],
        }
        self.active: dict[str, Any] | None = None
        self._start = 0.0
        self.flush()

    def flush(self) -> None:
        _write(self.output_dir / INDEX_PATH, self.data)
        _write(self.output_dir / "progress.json", public_execution(self.data))

    def _tick(self) -> None:
        assert self.active is not None
        self.active["last_event_at"] = datetime.now(UTC).isoformat()
        self.active["elapsed_to_last_event_seconds"] = max(0.0, time.monotonic() - self._start)

    def begin(self, task_id: str, trial: int, seed: int) -> None:
        if self.active is not None:
            raise RuntimeError("EPISODE_INDEX_ACTIVE_PAIR_EXISTS")
        rows = [r for r in self.data["rows"] if (r["task_id"], r["trial"]) == (task_id, trial)]
        if len(rows) != 1 or rows[0]["state"] != "not_run":
            raise RuntimeError("EPISODE_INDEX_PAIR_INVALID")
        self.active = rows[0]
        self._start = time.monotonic()
        self.active.update(state="running", seed=seed, started_at=datetime.now(UTC).isoformat())
        self.data["status"] = "running"
        self._tick()
        self.flush()

    def lifecycle(self, event: str, episode_id: str, counters: dict[str, int]) -> None:
        row = self.active
        if (
            row is None
            or event not in LIFECYCLE_EVENTS
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", episode_id) is None
            or not set(counters) <= COUNTERS
            or any(type(value) is not int or value < 0 for value in counters.values())
        ):
            raise RuntimeError("EPISODE_INDEX_LIFECYCLE_INVALID")
        if event == "episode_bound":
            if row["episode_id"] is not None or any(
                r["episode_id"] == episode_id for r in self.data["rows"]
            ):
                raise RuntimeError("EPISODE_INDEX_DUPLICATE_BINDING")
            row["episode_id"] = episode_id
        elif row["episode_id"] != episode_id:
            raise RuntimeError("EPISODE_INDEX_BINDING_MISMATCH")
        row["runtime_initialized"] |= event == "episode_initialized"
        row["last_lifecycle_event"] = event
        row["counters"].update(counters)
        self._tick()
        self.flush()

    def finish(self, simulation: Any) -> None:
        raw = simulation.model_dump(mode="json")
        row = self.active
        if row is None or any(raw[name] != row[name] for name in ("task_id", "trial", "seed")):
            raise RuntimeError("EPISODE_INDEX_RESULT_MISMATCH")
        reward = (raw.get("reward_info") or {}).get("reward")
        row.update(
            state="recorded",
            simulation_id=raw["id"],
            simulation_sha256=content_hash(raw),
            termination_reason=raw["termination_reason"],
            graded=_graded(reward),
        )
        self._tick()
        self.active = None
        self.flush()

    def finalize(self, status: str, reason: str | None = None) -> None:
        if self.active is not None:
            self.active["state"] = "interrupted"
            self._tick()
            self.active = None
        self.data.update(status=status, stop_reason=reason)
        self.flush()

    def manifest_fields(self) -> dict[str, Any]:
        return {
            "status": self.data["status"],
            "stop_reason": self.data["stop_reason"],
            "execution": public_execution(self.data),
            "episode_index": {
                "path": INDEX_PATH,
                "sha256": hashlib.sha256((self.output_dir / INDEX_PATH).read_bytes()).hexdigest(),
            },
        }


def validate_episode_index(
    index: dict[str, Any],
    manifest: dict[str, Any],
    simulations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate explicit joins against their immutable run and raw result bindings."""
    try:
        if (
            index["schema_version"] != INDEX_SCHEMA
            or index["scheduler"] != SCHEDULER
            or index["run_id"] != manifest["run_id"]
            or index["config_sha256"] != manifest["config_sha256"]
            or manifest["config_sha256"] != content_hash(manifest["config"])
            or index["status"]
            not in {"preparing", "running", "complete", "stopped", "interrupted", "failed"}
            or index["stop_reason"]
            not in {
                None,
                "infrastructure_error_limit",
                "keyboard_interrupt",
                "system_exit",
                "runner_failure",
            }
        ):
            raise ValueError
        scheduled = [(r["task_id"], r["trial"]) for r in manifest["scheduled"]]
        if [(r["task_id"], r["trial"]) for r in index["rows"]] != scheduled or len(
            set(scheduled)
        ) != len(scheduled):
            raise ValueError
        by_pair = {(r["task_id"], r["trial"]): r for r in simulations}
        if len(by_pair) != len(simulations) or not set(by_pair) <= set(scheduled):
            raise ValueError
        seen = set()
        for row in index["rows"]:
            pair = (row["task_id"], row["trial"])
            eid = row["episode_id"]
            if eid is not None:
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", eid) is None or eid in seen:
                    raise ValueError
                seen.add(eid)
            if (
                row["state"] not in ROW_STATES
                or type(row["graded"]) is not bool
                or type(row["runtime_initialized"]) is not bool
                or row["last_lifecycle_event"] not in LIFECYCLE_EVENTS | {None}
                or row["termination_reason"] not in TERMINATION_REASONS | {None}
                or set(row["counters"]) != COUNTERS
                or any(type(v) is not int or v < 0 for v in row["counters"].values())
            ):
                raise ValueError
            elapsed = row["elapsed_to_last_event_seconds"]
            if elapsed is not None and (
                type(elapsed) not in {int, float} or not math.isfinite(elapsed) or elapsed < 0
            ):
                raise ValueError
            raw = by_pair.get(pair)
            if row["state"] == "recorded":
                if raw is None or any(
                    row[key] != raw[key] for key in ("seed", "termination_reason")
                ):
                    raise ValueError
                reward = (raw.get("reward_info") or {}).get("reward")
                if (
                    row["simulation_id"] != raw["id"]
                    or row["simulation_sha256"] != content_hash(raw)
                    or row["graded"] != _graded(reward)
                ):
                    raise ValueError
            elif (
                raw is not None
                or row["graded"]
                or row["termination_reason"] is not None
                or row["simulation_id"] is not None
                or row["simulation_sha256"] is not None
            ):
                raise ValueError
            if row["state"] == "not_run" and (
                eid is not None
                or row["seed"] is not None
                or row["started_at"] is not None
                or elapsed is not None
            ):
                raise ValueError
        return index
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("EPISODE_INDEX_BINDING_INVALID") from None


def load_episode_index(
    run_dir: Path,
    manifest: dict[str, Any],
    simulations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    binding = manifest.get("episode_index")
    if binding is None:
        return None
    try:
        if not isinstance(binding, dict) or binding.get("path") != INDEX_PATH:
            raise ValueError
        raw = (run_dir / INDEX_PATH).read_bytes()
        if hashlib.sha256(raw).hexdigest() != binding.get("sha256"):
            raise ValueError
        return validate_episode_index(json.loads(raw), manifest, simulations)
    except (OSError, TypeError, ValueError):
        raise ValueError("EPISODE_INDEX_BINDING_INVALID") from None


def run_controlled(config: Any, *, output_dir: Path, index: EpisodeIndex, error_limit: int) -> Any:
    """Use pinned per-task execution while retaining interrupt/failure checkpoints."""
    from loguru import logger
    from tau2.data_model.simulation import Results, SimulationRun, TextRunConfig
    from tau2.evaluator.evaluator import EvaluationType
    from tau2.registry import registry
    from tau2.runner.batch import run_single_task
    from tau2.runner.checkpoint import create_checkpoint_fns
    from tau2.runner.helpers import get_info, get_tasks
    from tau2.runner.progress import run_with_retry

    if (
        not isinstance(config, TextRunConfig)
        or config.max_concurrency != 1
        or config.max_retries != 0
        or config.auto_resume
        or config.auto_review
        or type(error_limit) is not int
        or error_limit <= 0
    ):
        raise ValueError("CONTROLLED_RUN_REQUIRES_FROZEN_SERIAL_TEXT_SETTINGS")
    config.validate()
    logger.remove()
    logger.add(lambda message: print(message), level=config.log_level)
    tasks = get_tasks(
        config.task_set_name or config.domain,
        config.task_split_name,
        config.task_ids,
        config.num_tasks,
    )
    task_filter = registry.get_agent_task_filter(config.effective_agent)
    if task_filter is not None:
        tasks = [task for task in tasks if task_filter(task)]
    expected = list(dict.fromkeys(r["task_id"] for r in index.data["rows"]))
    if not tasks or [task.id for task in tasks] != expected:
        raise ValueError("CONTROLLED_RUN_TASK_SELECTION_MISMATCH")
    # Match the pinned runner's global seed derivation, including its resulting
    # RNG state. These are simulation schedule seeds, not universal model seeds.
    random.seed(config.seed)
    seeds = [random.randint(0, 1000000) for _ in range(config.num_trials)]
    policy_override = None
    if config.domain == "banking_knowledge":
        from tau2.domains.banking_knowledge.environment import get_knowledge_base
        from tau2.domains.banking_knowledge.retrieval import get_info_policy_override
        from tau2.knowledge.embeddings_cache import (
            get_unique_embedder_configs_for_retrieval_configs,
            warm_kb_cache,
        )

        kwargs = config.retrieval_config_kwargs or {}
        warm_kb_cache(
            get_unique_embedder_configs_for_retrieval_configs([config.retrieval_config], kwargs)
        )
        policy_override = get_info_policy_override(
            config.retrieval_config, get_knowledge_base(), **kwargs
        )
    results = Results(
        info=get_info(
            config,
            user_persona_config=None,
            user_voice_settings=None,
            policy_override=policy_override,
        ),
        tasks=tasks,
        simulations=[],
    )
    save_path = output_dir / "results.json"
    if save_path.exists():
        raise ValueError("CONTROLLED_RUN_RESULTS_ALREADY_EXIST")
    results.save(save_path, format="json")
    save_fn, _ = create_checkpoint_fns(save_path, threading.Lock())
    errors = 0
    try:
        for trial, seed in enumerate(seeds):
            for task in tasks:
                index.begin(task.id, trial, seed)

                def execute(task: Any = task, seed: int = seed) -> Any:
                    return run_single_task(
                        config,
                        task,
                        seed=seed,
                        evaluation_type=EvaluationType.ALL,
                        save_dir=output_dir,
                        user_voice_settings=None,
                        user_persona_config=None,
                        verbose_logs=config.verbose_logs,
                        audio_debug=False,
                        audio_taps=False,
                        auto_review=False,
                        review_mode=config.review_mode,
                        review_model=config.review_model,
                        hallucination_feedback=None,
                    )

                result = run_with_retry(
                    execute,
                    task=task,
                    trial=trial,
                    seed=seed,
                    max_retries=0,
                    console_display=False,
                    save_fn=save_fn,
                )
                results.simulations.append(result)
                # This metadata write is outside run_with_retry: failures here
                # must not be converted into a second upstream outcome.
                index.finish(result)
                errors += result.termination_reason == "infrastructure_error"
                if errors >= error_limit and any(
                    r["state"] == "not_run" for r in index.data["rows"]
                ):
                    index.finalize("stopped", "infrastructure_error_limit")
                    return results
    except (KeyboardInterrupt, SystemExit) as exc:
        # A signal can land after the upstream atomic checkpoint replacement but
        # before its return or our in-memory append. Recover only those already
        # saved results; never fabricate an outcome or re-run its evaluator.
        checkpoint = json.loads(save_path.read_text())
        results.simulations = [
            SimulationRun.model_validate(row) for row in checkpoint["simulations"]
        ]
        if index.active is not None:
            saved_active = [
                row
                for row in results.simulations
                if (row.task_id, row.trial, row.seed)
                == (index.active["task_id"], index.active["trial"], index.active["seed"])
            ]
            if len(saved_active) > 1:
                raise RuntimeError("CONTROLLED_INTERRUPT_CHECKPOINT_INVALID") from None
            if saved_active:
                index.finish(saved_active[0])
        index.finalize(
            "interrupted",
            "keyboard_interrupt" if isinstance(exc, KeyboardInterrupt) else "system_exit",
        )
        # The pinned per-task orchestrator cleans up in finally. No batch worker
        # executor is involved, so its forced os._exit interrupt path is avoided.
        return results
    index.finalize("complete")
    return results
