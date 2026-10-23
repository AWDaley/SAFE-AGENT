"""Independent reviewed TAL provenance for benchmark domain admission.

This does not change the office runtime pin. The installed distribution must be
the reviewed VCS commit, with matching import paths and module bytes. There is
no environment switch, editable-install exception, or network fallback. This is
an installation/provenance check, not a defense against a hostile Python host.
"""

from __future__ import annotations

import hashlib
import json
import re
from importlib import import_module
from importlib.metadata import distribution
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from tal.domain_admission import (  # type: ignore[import-untyped, import-not-found]
        DomainActionProposal,
        DomainAdmissionDecision,
        SealedDomainGrant,
        TrustedDomainEvidence,
    )

REVIEWED_TAL_BENCHMARK_COMMIT = "f70b5e70bcdf79ce9fd8f691661c8041807a734a"
REVIEWED_TAL_REPOSITORY = "https://github.com/AWDaley/TervoroAgenticLanguage-TAL-.git"
# Updated with the reviewed upstream module, independently of the commit pin.
REVIEWED_TAL_MODULE_SHA256: dict[str, str] = {
    "__init__": "26a4d88546a14c54c7ca9e57deb74e0051fe72ce4963b408852cb62d7a12d9b9",
    "core": "98cce1adfc6aac76f752d2a9c49c6e7ebbe6bfd843208570a366eca1f0ad06d6",
    "domain_admission": "3cae1d98804d1e2119887f52772319fb543e3aaf4c0ac08ac0c8d66a69ad68dd",
}


class TALDomainRuntimeDenied(RuntimeError):
    pass


class DomainAdmissionEvaluator(Protocol):
    def __call__(
        self,
        grant: SealedDomainGrant,
        proposal: DomainActionProposal,
        evidence: TrustedDomainEvidence,
    ) -> DomainAdmissionDecision: ...


def require_reviewed_domain_runtime() -> None:
    """Require reviewed, non-editable installed code before a benchmark runtime."""
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", REVIEWED_TAL_BENCHMARK_COMMIT):
            raise ValueError("Benchmark domain revision has not been pinned")
        if set(REVIEWED_TAL_MODULE_SHA256) != {"__init__", "core", "domain_admission"}:
            raise ValueError("Incomplete module integrity pin")
        installed = distribution("tervoro-agentic-language")
        direct = json.loads(installed.read_text("direct_url.json") or "null")
        if (
            not isinstance(direct, dict)
            or direct.get("url") != REVIEWED_TAL_REPOSITORY
            or direct.get("dir_info") is not None
            or not isinstance(direct.get("vcs_info"), dict)
            or direct["vcs_info"].get("vcs") != "git"
            or direct["vcs_info"].get("commit_id") != REVIEWED_TAL_BENCHMARK_COMMIT
        ):
            raise ValueError("Unreviewed TAL installation")
        for name, expected_hash in REVIEWED_TAL_MODULE_SHA256.items():
            module = import_module("tal" if name == "__init__" else "tal." + name)
            actual = Path(module.__file__ or "").resolve(strict=True)
            expected = Path(str(installed.locate_file("tal/" + name + ".py"))).resolve(strict=True)
            if (
                actual != expected
                or hashlib.sha256(actual.read_bytes()).hexdigest() != expected_hash
            ):
                raise ValueError("TAL import origin or content mismatch")
    except Exception:
        raise TALDomainRuntimeDenied("TAL_REVIEWED_DOMAIN_RUNTIME_REQUIRED") from None


def load_reviewed_domain_evaluator() -> DomainAdmissionEvaluator:
    require_reviewed_domain_runtime()
    return import_module("tal.domain_admission").admit_domain_action
