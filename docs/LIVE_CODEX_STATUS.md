# Live Codex execution status

Updated **2026-10-24**. **Real native A → B → C model execution and cross-account continuation passed** after the owner independently authenticated three distinct ChatGPT accounts. The exact requested command exited 0 and produced `live-proof/native-smoke-report.json` with `live_verified: true`. The optional Docker/API backend remains unverified against the real provider.

## Actual installed CLI checks

Installed CLI: **0.163.0**. Reviewed official [App Server](https://learn.chatgpt.com/docs/app-server), [authentication](https://learn.chatgpt.com/docs/auth) and [permissions](https://learn.chatgpt.com/docs/permissions) documentation, and generated the installed CLI's experimental JSON schema in a temporary directory. No authentication files were read or copied.

The requested command was run against the actual CLI:

```sh
python3 scripts/live_smoke.py --backend native --run-live --data ./live-proof
```

After the three interactive logins, it exits **0**. Verified prerequisites and protocol:

- PASS: macOS host and Codex CLI.
- PASS: three independent App Server processes initialize over stdio with isolated `CODEX_HOME` directories.
- PASS: native named permissions and accessible workspaces; actual sandboxed commands can write their workspace but cannot read nonsecret canaries in the profile home or another worker's workspace. The host Python unittest runtime imports successfully inside this sandbox.
- PASS: actual `thread/start` with the private native permission and shell configuration, exercising Codex's sandboxed instruction loader. The CLI executable is canonicalized and allowed as a single runtime read so its helper can run; no auth-directory read exception is granted. The server’s specific Python installation is also readable so framework/Homebrew Python can run tests; home/root runtime prefixes are refused.
- PASS: A, B and C authenticated with ChatGPT and three distinct account identities, checked without displaying or persisting their values. API-key auth is rejected by this backend.

Preflight starts no model turn. It performs no Docker/API backend checks and requires no organization API key, proxy token or configured model. Preflight alone does not prove inference or capacity. The completed real execution below supplies the coding and continuation evidence.

## Successful real execution

The unchanged acceptance command used three actual `codex app-server --stdio` processes and consumed the authenticated profiles' Codex allowance. No Docker, organization API key, proxy token or required model setting was supplied for native execution. No test fixture or mock transport was used.

| Worker / profile | Actual work | Turn duration | Tests at boundary |
| --- | --- | --- | --- |
| A / `a` | Created `text_tools.py`, initial line/longest-line logic, tests and README; reported unfinished work | 38.710 s | 2 passed |
| B / `b` | Read A's committed code and handoff/history, implemented word counting, added empty/blank-input tests | 33.045 s | 5 passed |
| C / `c` | Read B's handoff/history, added Unicode/mixed-line-ending tests, completed README | 38.169 s | 7 passed |

All three turns finished with state `completed` and exit code 0, contributed nonempty project patches, and used different native threads. The conversation stayed `2a325a7a4efb447db6f6cbaff411cc06`; task `6e6199a067e2402aaa08a0477851df59` completed with no pending steps. Two handoffs resumed successfully:

- A → B checkpoint: `57867d89b63c7676acb7fcff83e789ccff26a6a6`.
- B → C checkpoint: `b6ef5191f8acd4039732fcd6390711067f64e7a3`.
- Final acceptance checkpoint: `9229c2e78c052b5e1be02bba457bab4e5d13c0ca`.

The smoke reopened Relay and compared events, execution records, context snapshots and files. Persistence passed and all writer leases were released. The report retains native thread IDs, durations and the checkpointed handoffs. Generated code/tests and full visible events remain in the Git-ignored local `live-proof/` directory; credentials and account identities are absent from this proof.

Post-run repository review corrected one README expected-output comment: the example's longest line is `naïve texte`. The original model execution records and acceptance checkpoint remain intact. The separate documentation review checkpoint is `07706c1e52494191dc25ecf48599c2fa00ac182e`, recorded in the report's `review_checkpoint`; implementation and the seven tests were unchanged.

C corrected an initially incorrect Unicode test fixture and reran all seven tests successfully. Its optional `git status` and a shell heredoc encountered macOS temporary-cache restrictions; these are retained as genuine tool diagnostics. They did not prevent the Python tests, scheduler-owned checkpoints, handoffs or restart verification. Arbitrary developer-tool/temp-file compatibility has not been established by this one smoke run.

## Implemented path

`relay/app_server.py` provides bounded JSONL stdio RPC, initialization, responses/notifications, fail-closed server requests, process-group cleanup and a minimal child environment. `relay/native.py` manages persistent per-profile App Servers and file locks, local ChatGPT auth checks, private clones, new threads/turns, visible streaming events, interruption, errors and validated publication to canonical workspaces. Auth notifications, stderr, raw account objects and reasoning are not persisted. Neither raw auth nor credentials move between workers.

The existing scheduler retains project grants, authorizer checks, events, workspace leases, Git checkpoints, structured progress, context snapshots and handoff history. Schema migration **4** adds native profile identity and configurable two/three-phase boundaries without replacing existing data. Each cross-profile continuation creates a fresh Codex thread with the same Relay conversation, committed project state and visible task context. Only the configured local owner initiates subscription work. Usage exhaustion does not automatically rotate profiles or bypass limits.

The native smoke requires real changes by each worker, completed turns with distinct thread IDs, two checkpointed handoffs, C's successful test command, final progress with no pending steps and restart persistence before marking a report verified. Production has no mock transport setting or simulated fallback. The optional `docker-api` backend remains separately configured.

## Automated versus real evidence

`tests/fixtures/native_app_server.py` is an explicit test-only JSON-RPC peer. It writes real temporary source/tests and produces protocol events, but performs **no Codex inference or ChatGPT authentication**. Native tests cover:

- A → B → C continuation, separate workspaces/homes and new threads, checkpoints/context, original objective, visible history and restart persistence.
- Mocked end-to-end smoke orchestration and proof validation; generated reports stay in test temporary directories and are not real execution evidence.
- `turn/interrupt` preserving partial work before another profile starts.
- Conflict rejection when the canonical checkpoint changes.
- Native readiness independent of Docker, organization API credentials or required API models; API-key profile rejection.
- Owner restrictions, duplicate-profile rejection, no quota-driven account rotation, no forwarded API secrets or persisted reasoning/account identity.
- Refusal of escalation and external token requests, and canonical CLI executable handling for its sandbox helper.

Existing Docker/API transport fixtures, permissions, startup, recovery, proxy and simulation tests remain. Ordinary CI skips both genuine provider integration tests. A passing mocked test is never described as real Codex execution.

## Repeat real verification

See [deployment commands](../deploy/README.md) for three independent interactive logins. Then run:

```sh
python3 scripts/live_smoke.py --backend native --check
python3 scripts/live_smoke.py --backend native --run-live --data ./live-proof
# Optional genuine native unittest, consuming signed-in Codex allowance:
RELAY_RUN_NATIVE_SMOKE=1 python3 -m unittest discover -s tests -p test_real_codex_smoke.py -v
```

A successful real run creates `live-proof/native-smoke-report.json` with `live_verified: true`, execution records and checkpointed handoffs. Failure retains available evidence for inspection and never silently replays a turn. The original paid path remains available with `--backend docker-api` and `RELAY_RUN_LIVE_SMOKE=1` after its separate setup.

Native execution uses macOS permissions and deadlines, not the Docker backend's CPU/memory/container controls. Dependency availability, disk quotas and cleanup/retention are local operator responsibilities. The dashboard remains a loopback local demo with seeded identity selection; browser visual QA and remote deployment have not been verified.

## Recorded automated checks

- `python3 -m unittest discover -s tests -q`: **108 tests, 106 passed, 2 genuine-provider tests skipped**, 121.851 seconds. No model calls in ordinary CI.
- Native integration suite: 11 tests against the test-only JSON-RPC peer, included in the full run.
- `python3 scripts/demo.py`: PASS, simulated A → B with five actual validator tests and restart persistence.
- `python3 scripts/check_ui.py`: PASS, HTML IDs and JavaScriptCore syntax; not browser visual QA.
- `python3 -m compileall -q relay tests scripts deploy`: PASS.
- Ruff configured checks: PASS. Mypy on `relay/workers.py`, `relay/auth.py`, `relay/codex_events.py`, `relay/native.py`, `relay/app_server.py`: PASS. Development tools run from a temporary installation; runtime remains standard-library Python.
- `git diff --check`: PASS.
- `python3 scripts/live_smoke.py --backend native`: explicit SKIP without `--run-live`.
- Before authentication, native preflight exited 2 without a model turn. After the three interactive logins, **the exact native `--run-live` command exited 0**, with three real model turns, two checkpointed handoffs, seven passing generated tests and restart persistence. This evidence is separate from the ordinary mocked test suite.
