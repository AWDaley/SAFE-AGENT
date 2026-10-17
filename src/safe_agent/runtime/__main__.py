"""Run read-only dependency diagnostics, never an agent or migration."""
from safe_agent.runtime.preflight import main

if __name__ == "__main__":
    raise SystemExit(main())
