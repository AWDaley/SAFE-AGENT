# Packaged synthetic demonstrations

The runtime-profile installation test exposed an existing packaging defect:
`run_m3_demo`, `run_m6_planning_demo` and `run_m6_worker_demo` searched for fixture
files relative to a source checkout rather than the installed package. In
workflow `38927119729` (job `117061396682`, head
`ad7f59b65b639d05a265810d45e562338b878a91`), the clean hashed installation,
installed preflight, no-network preflight check and 51 new unit tests passed;
the full installed suite reported **3 failed, 796 passed, 99 skipped** because
those fixture files were missing. The subsequent PostgreSQL stage was skipped.

The correction bundles byte-identical copies of `demo.json` and
`office_state.json` under `safe_agent.demo/fixtures`. Explicit demonstration
commands resolve that packaged directory, not the working directory. The M0,
M1, M2, M3, M4 and M6 demo fixture lookups now use this location. Existing test
fixtures remain unchanged, with byte-equality tests preventing unnoticed drift.

This does not introduce customer-data fallback: `SyntheticOfficeState.from_fixture`
still fails when the explicitly requested input is absent. These records and
the fixed demonstration keys are synthetic examples, never real credentials.
The historical demo workflows remain demonstrations, not the coordinated
application startup path or a way to adopt real tasks into legacy execution.

After installing the profile from `COORDINATED_RUNTIME_PROFILE.md`, run:

```sh
.venv-coordinated/bin/safe-agent m6-planning-demo
.venv-coordinated/bin/safe-agent m4-demo
```

The first prepares a synthetic proposal without a provider effect. The second
uses disposable synthetic storage to demonstrate uncertain-effect recovery.
Both commands still use their original controlled clocks and original safety
assertions; they are not evidence of real-model usefulness or live-calendar
acceptance. Their temporary workspaces are created only when explicitly invoked.

The dedicated runtime workflow now executes eight installed non-Docker demo
commands from `/tmp`, plus at least 55 non-skipped runtime/package tests, the
complete installed test suite, and the existing 19-case real PostgreSQL/TAL
boundary suite. The separate existing Docker-isolation smoke remains required;
this change does not claim that a Docker-less host can run that optional demo.

No model endpoint, authorization rule, migration, live provider, persistent
customer workspace or production permission is changed by this packaging fix.
