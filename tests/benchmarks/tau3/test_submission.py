"""Private submission packaging with real pinned validators and authored records.

Only source inventory/import setup and the registry's task loader are replaced.
The upstream Results/Submission schemas, task/trial checks, and metrics all run.
No official task contents, models, judges, or retrieval services are used.
"""

import copy
import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from safe_agent.benchmarks.tau3 import TAU_COMMIT, submission
from safe_agent.benchmarks.tau3.config import BenchmarkConfig
from safe_agent.benchmarks.tau3.inventory import content_hash


def _json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(root)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in root.rglob("*")
        if path.is_file()
    }


@dataclass
class _SavedRun:
    run_dir: Path
    metadata_path: Path
    output_dir: Path
    tau_root: Path
    manifest: dict[str, Any]
    results: dict[str, Any]
    metrics: dict[str, Any]
    loader_calls: list[str]

    def save(self) -> None:
        """Rebind authored changes so deeper consistency checks are exercised."""
        _json(self.run_dir / "results.json", self.results)
        _json(self.run_dir / "official_metrics.json", self.metrics)
        self.manifest["saved_artifacts_sha256"] = {
            name: _file_hash(self.run_dir / name)
            for name in ("results.json", "official_metrics.json")
        }
        _json(self.run_dir / "manifest.json", self.manifest)

    def prepare(self) -> dict[str, Any]:
        return submission.prepare_submission(
            self.run_dir, self.metadata_path, self.output_dir, tau_root=self.tau_root
        )

    def reject(self, pattern: str | None = None) -> None:
        before = _snapshot(self.run_dir)
        with pytest.raises((submission.SubmissionError, ValidationError), match=pattern):
            self.prepare()
        assert _snapshot(self.run_dir) == before
        assert not self.output_dir.exists()
        assert not list(self.output_dir.parent.glob(".tau3-submission-*"))


@pytest.fixture
def saved_run(monkeypatch, tmp_path):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    tau2 = pytest.importorskip("tau2", reason="Pinned benchmark dependency is optional in office CI")
    import litellm
    from tau2.config import DEFAULT_LLM_NL_ASSERTIONS, DEFAULT_LLM_NL_ASSERTIONS_ARGS
    from tau2.data_model.simulation import Results
    from tau2.data_model.tasks import EvaluationCriteria, RewardType, Task, UserScenario
    from tau2.metrics.agent_metrics import compute_metrics
    from tau2.registry import registry

    def no_model(*args, **kwargs):
        pytest.fail("Submission packaging must never call a model, judge, or embedding service")

    for method in ("completion", "acompletion", "embedding", "aembedding"):
        monkeypatch.setattr(litellm, method, no_model)

    tasks = [
        Task(
            id="authored_nl",
            user_scenario=UserScenario(instructions="Ask for the authored status information."),
            evaluation_criteria=EvaluationCriteria(
                nl_assertions=["The customer receives the authored status information."],
                reward_basis=[RewardType.NL_ASSERTION],
            ),
        ),
        Task(
            id="authored_db",
            user_scenario=UserScenario(instructions="Perform the authored database task."),
            evaluation_criteria=EvaluationCriteria(actions=[], reward_basis=[RewardType.DB]),
        ),
    ]
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    tau_root = Path(tau2.__file__).resolve().parents[2]
    config = BenchmarkConfig(
        domain="banking_knowledge",
        stage="final",
        run_id="authored-final",
        output_dir=str(run_dir),
        tau_root=str(tau_root),
        num_trials=4,
        llm_agent="authored-agent-model",
        llm_user="authored-user-model",
        development_task_ids=["authored_nl"],
    )
    inventory = {
        "status": "ready",
        "source_verified": True,
        "source_verification": {"manifest_sha256": "1" * 64},
        "domain": config.domain,
        "domain_task_count": len(tasks),
        "selected_task_count": len(tasks),
        "selected_task_ids": [task.id for task in tasks],
        "task_ids_sha256": content_hash([task.id for task in tasks]),
        "task_content_sha256": content_hash([task.model_dump(mode="json") for task in tasks]),
        "selected_grading_basis_counts": {"NL_ASSERTION": 1, "DB": 1},
        "selected_tasks_with_nl_assertions": 1,
    }
    loader_calls = []

    def authored_loader(split):
        assert split == "base"
        loader_calls.append(split)
        return [task.model_copy(deep=True) for task in tasks]

    def get_loader(domain):
        assert domain == config.domain
        return authored_loader

    def inspect_authored(_config, root):
        assert root == tau_root
        return copy.deepcopy(inventory)

    def import_pinned(root):
        assert root / "src/tau2" == Path(tau2.__file__).resolve().parent

    monkeypatch.setattr(registry, "get_tasks_loader", get_loader)
    monkeypatch.setattr(submission, "inspect_benchmark", inspect_authored)
    monkeypatch.setattr(submission, "prepare_official_import", import_pinned)
    simulations = []
    for task in tasks:
        for trial in range(4):
            reward = float(trial < (3 if task.id == "authored_nl" else 1))
            basis = task.evaluation_criteria.reward_basis[0].value
            reward_info = {
                "reward": reward,
                "reward_basis": [basis],
                "reward_breakdown": {basis: reward},
            }
            if basis == "NL_ASSERTION":
                reward_info["nl_assertions"] = [{
                    "nl_assertion": task.evaluation_criteria.nl_assertions[0],
                    "met": bool(reward),
                    "justification": "Authored recorded judge outcome; no judge was invoked.",
                }]
            else:
                reward_info["db_check"] = {"db_match": bool(reward), "db_reward": reward}
            premature = task.id == "authored_db" and trial == 3
            if premature:
                reward_info = {"reward": 0.0, "reward_basis": None}
            simulations.append({
                "id": f"{task.id}-{trial}",
                "task_id": task.id,
                "trial": trial,
                "seed": config.seed + trial,
                "timestamp": "2026-01-01T00:00:00Z",
                "start_time": "2026-01-01T00:00:00Z",
                "end_time": "2026-01-01T00:00:01Z",
                "duration": 1.0,
                "termination_reason": "agent_error" if premature else "user_stop",
                "agent_cost": 0.0,
                "user_cost": 0.0,
                "reward_info": reward_info,
                "messages": [] if premature else [
                    {"role": "user", "content": "Authored customer request."},
                    {"role": "assistant", "content": "Authored agent response."},
                ],
                "mode": "half_duplex",
            })
    results = Results.model_validate({
        "timestamp": "2026-01-01T00:00:00Z",
        "info": {
            "git_commit": TAU_COMMIT,
            "num_trials": config.num_trials,
            "max_steps": config.max_steps,
            "max_errors": config.max_errors,
            "seed": config.seed,
            "agent_info": {
                "implementation": "safe_agent_" + config.run_id,
                "llm": config.llm_agent,
                "llm_args": config.llm_args_agent,
            },
            "user_info": {
                "implementation": "user_simulator",
                "llm": config.llm_user,
                "llm_args": config.llm_args_user,
            },
            "environment_info": {"domain_name": config.domain, "policy": "Authored policy."},
            "retrieval_config": config.retrieval_config,
            "retrieval_config_kwargs": config.retrieval_config_kwargs,
        },
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "simulations": simulations,
    })
    manifest = {
        "schema_version": 1,
        "kind": "official_tau3_text",
        "tau_commit": TAU_COMMIT,
        "run_id": config.run_id,
        "system": "safe_agent",
        "runtime_mode": "canonical_safe",
        "status": "complete",
        "config": config.model_dump(),
        "config_sha256": content_hash(config.model_dump()),
        "safe_revision": {
            "dirty": False, "commit": "2" * 40, "runtime_sources_sha256": "3" * 64,
        },
        "inventory": copy.deepcopy(inventory),
        "scheduled": [
            {"task_id": task.id, "trial": trial} for task in tasks for trial in range(4)
        ],
        "evaluation_type": "ALL",
        "automatic_review": False,
        "retries": 0,
        "judge_model": DEFAULT_LLM_NL_ASSERTIONS,
        "judge_args": copy.deepcopy(DEFAULT_LLM_NL_ASSERTIONS_ARGS),
        "expected_cost_components": ["agent", "user", "judge"],
        "dependencies": {"tau2": "1.0.1"},
    }
    metadata_path = tmp_path / "submitter.json"
    _json(metadata_path, {
        "model_name": "Authored SAFE fixture",
        "model_organization": "Authored model organization",
        "submitting_organization": "Authored submitting organization",
        "submission_date": "2026-01-02",
        "contact_info": {"name": "Authored Submitter", "email": "authored@example.test"},
        "architecture_notes": "Authored scaffold description for an offline packaging fixture.",
        "development_exposure_notes": "Only authored tasks were used to develop this fixture.",
        "implementation_url": "https://github.com/AWDaley/SAFE-AGENT",
    })
    saved = _SavedRun(
        run_dir, metadata_path, tmp_path / "bundle", tau_root,
        manifest, results.model_dump(mode="json"), compute_metrics(results).model_dump(mode="json"),
        loader_calls,
    )
    saved.save()
    # These private artifacts must stay in the source run, including sidecars.
    for filename in ("episodes.sqlite3", "episodes.sqlite3-wal", "model-private.json"):
        (run_dir / filename).write_text("PRIVATE_DATABASE_CANARY_DO_NOT_COPY")
    return saved


def test_saved_results_round_trip_uses_upstream_checks_and_private_permissions(saved_run):
    from tau2.data_model.simulation import Results
    from tau2.metrics.agent_metrics import compute_metrics
    from tau2.scripts.leaderboard.submission import Submission
    from tau2.scripts.leaderboard.verify_trajectories_public import check_num_trials, check_tasks

    before = _snapshot(saved_run.run_dir)
    metadata_before = saved_run.metadata_path.read_bytes()
    validation = saved_run.prepare()
    assert saved_run.loader_calls == ["base"]
    assert validation["status"] == "validated_saved_records"
    assert validation["fresh_model_calls"] == 0 and not validation["fresh_judging"]
    assert not validation["published"] and not validation["independent_run_authenticity_verified"]
    payload = Submission.load(saved_run.output_dir / "submission.json")
    trajectory_path = saved_run.output_dir / "trajectories" / payload.trajectory_files["banking_knowledge"]
    results = Results.load(trajectory_path)
    assert check_tasks(results)[0] and check_num_trials(results)[0]
    assert compute_metrics(results).model_dump(mode="json") == saved_run.metrics
    scores = payload.results.banking_knowledge
    assert (scores.pass_1, scores.pass_2, scores.pass_3, scores.pass_4) == (50, 25, 12.5, 0)
    assert scores.retrieval_config == "bm25" and scores.cost is None
    assert payload.submission_type == "custom" and payload.modality == "text"
    assert payload.methodology.verification.modified_prompts
    assert not payload.methodology.verification.omitted_questions
    assert "authored_nl" in payload.methodology.notes
    assert len(results.simulations) == 8
    assert results.simulations[-1].termination_reason.value == "agent_error"
    assert results.simulations[-1].reward_info.reward == 0
    assert results.simulations[-1].reward_info.reward_basis is None
    assert results.simulations[-1].messages == []
    assert trajectory_path.read_bytes() == (saved_run.run_dir / "results.json").read_bytes()
    assert _snapshot(saved_run.run_dir) == before
    assert saved_run.metadata_path.read_bytes() == metadata_before
    expected_files = {
        "submission.json", "official_metrics.json", "disclosures.json", "validation.json",
        "trajectories/banking_knowledge.json",
    }
    assert set(_snapshot(saved_run.output_dir)) == expected_files
    for relative, (raw, mode) in _snapshot(saved_run.output_dir).items():
        assert mode == 0o600, relative
        assert b"PRIVATE_DATABASE_CANARY_DO_NOT_COPY" not in raw
    for path in (saved_run.output_dir, saved_run.output_dir / "trajectories"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for name, digest in validation["files_sha256"].items():
        assert _file_hash(saved_run.output_dir / name) == digest
    disclosures = json.loads((saved_run.output_dir / "disclosures.json").read_text())
    assert "tau_root" not in disclosures["config"] and "output_dir" not in disclosures["config"]
    assert disclosures["input_sha256"]["results.json"] == _file_hash(saved_run.run_dir / "results.json")


@pytest.mark.parametrize("defect", ["missing", "duplicate_pair", "unexpected_pair", "infrastructure", "ungraded"])
def test_incomplete_or_invalid_matrix_is_rejected(saved_run, defect):
    rows = saved_run.results["simulations"]
    if defect == "missing":
        rows.pop()
    elif defect == "duplicate_pair":
        rows.append(copy.deepcopy(rows[0]))
    elif defect == "unexpected_pair":
        rows[0]["trial"] = 99
    elif defect == "infrastructure":
        rows[0]["termination_reason"] = "infrastructure_error"
    else:
        rows[0]["reward_info"] = None
    saved_run.save()
    saved_run.reject("COMPLETE_GRADED_TRAJECTORY_MATRIX_REQUIRED")


@pytest.mark.parametrize(
    "defect", ["duplicate_task", "duplicate_simulation_id", "changed_task", "missing_task"]
)
def test_duplicate_identities_or_changed_public_tasks_are_rejected(saved_run, defect):
    if defect == "duplicate_task":
        saved_run.results["tasks"].append(copy.deepcopy(saved_run.results["tasks"][0]))
    elif defect == "duplicate_simulation_id":
        saved_run.results["simulations"][1]["id"] = saved_run.results["simulations"][0]["id"]
    elif defect == "changed_task":
        saved_run.results["tasks"][0]["user_scenario"]["instructions"] = "Changed authored task."
    else:
        saved_run.results["tasks"].pop()
    saved_run.save()
    saved_run.reject()


@pytest.mark.parametrize("defect", ["filtered", "dirty_source", "incomplete_run", "changed_inventory", "changed_judge"])
def test_nonfinal_or_changed_run_evidence_is_rejected(saved_run, defect):
    manifest = saved_run.manifest
    if defect == "filtered":
        manifest["config"]["task_ids"] = ["authored_nl"]
        manifest["config_sha256"] = content_hash(manifest["config"])
    elif defect == "dirty_source":
        manifest["safe_revision"]["dirty"] = True
    elif defect == "incomplete_run":
        manifest["status"] = "running"
    elif defect == "changed_inventory":
        manifest["inventory"]["task_content_sha256"] = "9" * 64
    else:
        manifest["judge_model"] = "different-judge"
    saved_run.save()
    saved_run.reject()


def test_changed_saved_metrics_are_rejected_even_with_rebound_file_hash(saved_run):
    saved_run.metrics["pass_hat_ks"]["1"] = 0.99
    saved_run.save()
    saved_run.reject("SAVED_OFFICIAL_METRICS_MISMATCH")


@pytest.mark.parametrize("defect", ["missing_assertion", "changed_label", "changed_verdict", "changed_breakdown"])
def test_recorded_judge_outputs_must_match_task_and_reward(saved_run, defect):
    reward = saved_run.results["simulations"][0]["reward_info"]
    if defect == "missing_assertion":
        reward["nl_assertions"] = []
    elif defect == "changed_label":
        reward["nl_assertions"][0]["nl_assertion"] = "A different assertion was judged."
    elif defect == "changed_verdict":
        reward["nl_assertions"][0]["met"] = False
    else:
        reward["reward_breakdown"]["NL_ASSERTION"] = 0.0
    saved_run.save()
    saved_run.reject()


@pytest.mark.parametrize("name", ["results.json", "official_metrics.json"])
def test_post_run_file_edits_break_completed_manifest_binding(saved_run, name):
    path = saved_run.run_dir / name
    # A whitespace edit preserves schema/metrics but changes the completed bytes.
    path.write_bytes(path.read_bytes() + b"\n")
    saved_run.reject()


def test_output_cannot_be_inside_source_run_or_replace_existing_directory(saved_run):
    saved_run.output_dir = saved_run.run_dir / "bundle"
    saved_run.reject("OUTPUT_MUST_BE_OUTSIDE_RUN")
    saved_run.output_dir = saved_run.run_dir.parent / "existing"
    saved_run.output_dir.mkdir()
    with pytest.raises(submission.SubmissionError, match="SUBMISSION_OUTPUT_MUST_NOT_EXIST"):
        saved_run.prepare()
    assert saved_run.output_dir.is_dir()


def test_output_appearing_during_validation_is_not_replaced(saved_run, monkeypatch):
    original = submission.tempfile.mkdtemp
    created_inode = []

    def concurrent_output(*args, **kwargs):
        saved_run.output_dir.mkdir(mode=0o750)
        created_inode.append(saved_run.output_dir.stat().st_ino)
        return original(*args, **kwargs)

    monkeypatch.setattr(submission.tempfile, "mkdtemp", concurrent_output)
    with pytest.raises((submission.SubmissionError, FileExistsError)):
        saved_run.prepare()
    assert saved_run.output_dir.stat().st_ino == created_inode[0]
    assert stat.S_IMODE(saved_run.output_dir.stat().st_mode) == 0o750
    assert not list(saved_run.output_dir.iterdir())
    assert not list(saved_run.output_dir.parent.glob(".tau3-submission-*"))


def test_input_symlink_is_rejected_without_following_private_target(saved_run):
    results_path = saved_run.run_dir / "results.json"
    target = saved_run.run_dir.parent / "private-target.json"
    results_path.rename(target)
    results_path.symlink_to(target)
    before = target.read_bytes()
    saved_run.reject("REQUIRED_REGULAR_FILE_MISSING")
    assert target.read_bytes() == before
