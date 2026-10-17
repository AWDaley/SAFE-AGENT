"""Fail-closed provenance check for the reviewed TAL runtime on coordinated writes.

This checks installation metadata and import origins, not hostile-root integrity.
No network, install, refresh, or automatic fallback occurs at dispatch time.
"""
from __future__ import annotations

import json
from importlib import import_module
from importlib.metadata import distribution
from pathlib import Path

from safe_agent.tal.trusted_dispatch import TALOfficeDispatchDenied

REVIEWED_TAL_COMMIT = "e9d09ef47c0e0e18f3955f9c58f7a8941baf6c38"
REVIEWED_TAL_REPOSITORY = "https://github.com/AWDaley/TervoroAgenticLanguage-TAL-.git"


def require_reviewed_tal_runtime() -> None:
    """Require the reviewed VCS installation, not merely a same-named import."""
    try:
        installed = distribution("tervoro-agentic-language")
        direct = json.loads(installed.read_text("direct_url.json") or "null")
        if (not isinstance(direct, dict)
                or direct.get("url") != REVIEWED_TAL_REPOSITORY
                or not isinstance(direct.get("vcs_info"), dict)
                or direct["vcs_info"].get("vcs") != "git"
                or direct["vcs_info"].get("commit_id") != REVIEWED_TAL_COMMIT):
            raise ValueError("unreviewed TAL installation")
        for name in ("core", "office", "runtime", "adapters", "storage"):
            module = import_module("tal." + name)
            actual = Path(module.__file__ or "").resolve(strict=True)
            expected = Path(str(installed.locate_file("tal/" + name + ".py"))).resolve(strict=True)
            if actual != expected:
                raise ValueError("TAL import origin mismatch")
    except Exception:
        raise TALOfficeDispatchDenied("TAL_REVIEWED_RUNTIME_REQUIRED") from None
