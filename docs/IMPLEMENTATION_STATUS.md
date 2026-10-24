> Local-native dashboard milestone (2026-10-24): `--auth local-owner --backend native` pins one password-authenticated Relay owner to the current OS user, rejects demo/shared/proxied execution, and reuses native profiles/worktree/event/checkpoint infrastructure. A real Chrome A → B coding/handoff test passed with five generated tests and reload/restart persistence. See [local-native setup and proof](LOCAL_NATIVE_DASHBOARD.md). The prior shared/native proofs and unrelated work remain intact.

> Shared-server milestone (2026-10-24): invite-only human authentication, project roles, authorized SSE/history/downloads, real pause/resume, three-human/three-worker fallback, HTTPS service configuration and trusted repository import are implemented. See [multi-computer setup](MULTI_COMPUTER.md) and [verification status](MULTI_COMPUTER_STATUS.md). Real Chrome sessions passed on one host with **simulated inference**; physical multi-host/live organization execution remains **NOT VERIFIED**. Earlier demo-only descriptions below describe prior milestones.

> Native backend update (2026-10-24): macOS App Server execution, three isolated ChatGPT profiles, native preflight and A → B → C smoke orchestration are now implemented alongside the preserved Docker/API backend. See [deployment setup](../deploy/README.md) and [current verification status](LIVE_CODEX_STATUS.md). **Real native A → B → C execution passed** with three distinct authenticated profiles, two checkpointed handoffs, seven generated tests and restart persistence. Earlier Docker-only statements below describe the previous milestone.

# Implementation status

Updated 2026-10-24. This repository is a single-organization, local hackathon application. “Implemented” means code and deterministic tests exist; it does not imply a hosted production deployment or a verified paid API run.

See [LIVE_CODEX_STATUS.md](LIVE_CODEX_STATUS.md) for the subsequent live-runner implementation, baseline/final evidence and opt-in genuine smoke procedure. The latest iteration adds schema migration 3, durable execution runs, actual live progress context, bounded stdout/stderr capture, precise provider-limit handling and a live two-phase boundary. Genuine Docker/OpenAI verification remains unavailable.

## Existing-code audit

Before this change the app had a standard-library Python HTTP server, SQLite, Git repositories, seeded local identities, two hard-coded simulated workers, a browser dashboard, and 15 acceptance/startup tests. There was no SDK integration, production authentication, external task queue, repository upload, or provider quota measurement. Worker ownership was a column without explicit project grants, capacity sources, enrollment or an execution adapter. All execution was the fixed validator fixture. Original files and their useful tests were retained.

The implementation order was additive schema migration → explicit worker permissions → persistent checkpoint/context/recovery changes → adapter-driven simulation and directory/settings UI → optional container-only organization API adapter → regression tests, demo and documentation.

## Feature inventory

| Feature | Status | Evidence / boundary |
| --- | --- | --- |
| Users / organization / project memberships | Implemented | `organizations`, `organization_memberships`, existing users/projects and `project_memberships` view. One seeded demo organization; not arbitrary tenant onboarding |
| Real human identity verification | Blocked / future | `LocalDemoIdentityProvider` exposes seeded identity selection. `IdentityProvider` is a clean integration boundary, not an OIDC implementation. Other auth modes fail closed |
| Separately owned workers | Implemented | User/org ownership, explicit project grants, owner/admin configuration, opt-in, mode, timestamps, max parallel tasks (one supported), and credential references |
| Worker privacy and project permissions | Implemented | Unshared workers hidden, private projects inaccessible, credential references excluded from all HTTP responses. Same-org membership alone never grants a worker |
| Enrollment/settings/revocation | Implemented | HTTP routes and dashboard; no fields accept execution secrets. Revoking an active worker requests a safe stop/handoff |
| Independent personal subscription allowances | Blocked / unsupported | Disabled personal adapter. Current official documentation did not explicitly establish Relay's remotely hosted cross-user delegation use case. No personal auth is collected |
| Independent simulated capacity | Simulated | A owned by Alex, B by Sam; separate simulated quota scopes. Demo limit marks only A quota-blocked. No exact remaining-token claims |
| Organization API capacity | Implemented model; live observation unverified | Same organization quota scope across API workers. Provider errors/usage retain provenance; unknown totals/reset times remain null. No quota reset by worker switch |
| Live organization API coding | Implemented adapter; live verification blocked | Official `codex exec --json` inside constrained Docker, narrow inference proxy. Docker and authorized API credential unavailable here; fixture tests are not paid integration evidence |
| Full visible transcript / tools | Implemented | Append-only ordered SQLite events; messages/tool_events views reuse original events. Pagination, attribution, correlation, size caps and protected large artifacts. No reasoning item retention |
| Context summaries and HANDOFF.md | Implemented | Versioned deterministic context, source event range, objective/instructions/decisions/files/pending/tests/errors/Git hash and recent turns. Full history remains separate; on-demand event files in the read-only metadata mount |
| Server-side code and checkpoints | Implemented | Git repositories, durable checkpoint records, file/diff inspection. No file download/re-upload required. Storage is local to the server, not a deployed cloud volume |
| Scheduler / handoff | Implemented | Rechecks grants/opt-in/mode/capacity, one project lease and one active task per conversation, generation-based duplicate switch protection, idempotency keys. Pending queue when nobody qualifies |
| Async execution / cancellation | Implemented and tested with controlled adapters | Simulation stops at fixed safe boundaries. Live tasks use background execution and bounded cancellation; replacement waits for confirmed cleanup |
| Interrupted process recovery | Implemented | Uncertain tools are not replayed. Task-labeled API containers are cleaned up before writer release. Unverified cleanup retains pausing state and lease for operator recovery |
| Partial checkpoint failure | Implemented | Error and recoverable handoff persisted, workspace retained, explicit retry available. Git and SQLite are not one atomic storage transaction |
| Reviewable output | Implemented | Git checkpoint/hash/diff and file viewer; automatic remote PR publishing is not implemented |
| Dashboard | Implemented | Worker directory/settings/enrollment, approval indicators, mode/source/scope, initial/target selectors, reasons, authorizers, handoff timeline, context/files/checkpoints/full history and end-to-end demo button |
| Cloud deployment / enterprise hardening | Blocked / future | Local-only login and loopback server. Container deployment instructions provided; real SSO, TLS, cloud volumes/backups, multi-host scheduling and production quotas still required |

## Data compatibility and safety

`relay/migrations.py` applies additive, version-recorded migrations to the original SQLite schema. Existing conversation/event IDs, content, custom worker names and Git workspaces remain. Legacy demo workers receive explicit demo grants; unrelated project membership never becomes a general worker permission. Repeated migration is idempotent. The server data-directory lock prevents two HTTP processes from mutating one database/worktree; never remove a live lock file to bypass it.

Events and audit rows reject update/delete through SQLite triggers. Ordinary database rows never contain API secret values. The only live credential reference is server-assigned `env:RELAY_ORG_OPENAI_API_KEY`; the company key is held by a separate proxy. Exact configured secrets and common key patterns are scrubbed from visible output. This is defensive filtering, not a guarantee that arbitrary secrets in arbitrary source code can be discovered. Use only trusted operators and never paste secrets into prompts.

The coding container mounts only a server-generated project, with `.git` and `.relay` read-only, an internal-only network, memory/CPU/process/deadline limits, and no Docker socket or personal home/auth state. The proxy permits only fixed OpenAI inference endpoints. Package dependencies must be preinstalled into a reviewed image. Production deployments also need filesystem quotas on the data volume, credential rotation, and real authentication. The provided sandbox does not make demo identities production-safe.

## Reproducible verification

Run the complete suite and code checks from the repository root:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/demo.py
python3 scripts/check_ui.py
python3 -m compileall -q relay tests scripts deploy
```

Tests cover original acceptance/startup behavior plus ownership/grants, credential reference privacy, disabled personal mode, capacity provenance, migrations, failures, concurrency, async cancellation, lease retention, HTTP enrollment/settings and the repeatable A→B demo. Live event protocol/proxy tests use deterministic fixtures and never call a paid provider. There is no frontend framework build in this dependency-free app. `requirements-dev.txt` supplies optional lint/type-check tools; see README for commands.

No browser visual QA, Docker image build or paid Codex request was available in this environment. See [authentication/execution compatibility](AUTH_EXECUTION_COMPATIBILITY.md) and [container setup](../deploy/README.md) for the exact support boundary.

Previous ownership-model verification on 2026-10-24: **76 tests passed** in 71.326 seconds; the standalone demo passed with five real validator tests and a durable A→B handoff. Ruff 0.17.2 passed the configured fatal-error/unused-code rules. Mypy 2.4.0 passed the typed worker and identity interfaces (`relay/workers.py`, `relay/auth.py`); this is not a claim of strict whole-repository typing. Python compilation, HTML/JavaScript ID consistency, JavaScriptCore syntax, and `git diff --check` passed. Development tools were installed in a temporary directory only; runtime dependencies remain standard-library Python.

Current verification: **108 tests total, 106 passed, two genuine native/API integration tests skipped by default**. The installed native CLI passes initialization, thread creation, Python runtime and OS sandbox probes. After three distinct profile logins, the real native A → B → C acceptance command passed with seven generated tests and restart persistence. The mock transport exercises the real adapter, actual temporary code changes/test subprocesses and persistence; it is not evidence of genuine model inference. See [LIVE_CODEX_STATUS.md](LIVE_CODEX_STATUS.md) for commands, results and remaining blockers.

The existing application now has the integrated IDE described in [IDE_WORKSPACE.md](IDE_WORKSPACE.md): local Monaco assets, safe editor APIs and durable drafts/reservations, multiple project conversations, coalesced/paired visible events, constrained project Git, and actual local-owner-only sandboxed commands with stdin and explicit conflict-checked publication. The console is restricted argv execution, not a general PTY shell. Browser reports label mocked AI events separately from real command execution.
