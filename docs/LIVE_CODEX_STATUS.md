# Live Codex execution status

Updated **2026-10-24**. **No genuine Docker/OpenAI run was verified in this environment.** Docker, the organization API key and proxy capability were absent. Installed Codex CLI was `0.163.0`; `codex exec --help` and current official documentation were reviewed. No local ChatGPT login state or personal credentials were used.

## Completed implementation

The existing Python/SQLite/Git architecture and simulated demo were preserved. The live path remains the documented official `codex exec --json` integration inside the existing non-root Docker sandbox and narrow organization API proxy.

- A freeform objective is passed intact to Codex stdin, except required secret filtering. The simulation fixture is never substituted for a failed live task. Handoffs cannot cross from organization API execution to simulation.
- stdout JSONL and stderr are captured separately, with bounded output, command/exit metadata, and redaction. A final event without a newline is processed. Reasoning items are discarded before persistence; visible agent/tool items are retained. Native thread IDs are persisted as soon as they arrive.
- Additive migration **3** stores task phase/progress and `execution_runs`: assignment/worker/mode, start/end, exit code, duration, native thread, error category, retry guidance and progress. Events remain append-only and correlated by execution run. `GET /api/tasks/:id/result` requires project access.
- `start`/`resume` retain the durable event callback interface. The live adapter additionally exposes `stream_events`, `get_result` and `resume_or_start_from_handoff`; Git checkpoints remain scheduler-owned. These are Relay's interfaces, not invented SDK methods.
- The pinned image includes an official `--output-schema` schema for visible final progress: summary, completed work, pending steps, decisions, tests, errors and limitations. The next context uses that actual output, current Git branch/checkpoint, changed files and referenced event artifacts.
- `pause_after_turn: true` requests a useful partial first phase and a backend-enforced safe boundary. After the first completed turn, Relay checkpoints, releases the lease and remains `handoff_pending` until the user selects a worker. It does not fake a usage limit. If the model finishes everything anyway, the live smoke refuses to claim proof of unfinished-work continuation.
- Mid-run Switch requests cancellation, verifies container/process shutdown, checkpoints preserved bytes and rehydrates a fresh worker against the same project/conversation. A stop can leave partially edited code; diffs and tests must be inspected. A second writer cannot start before verified cleanup. Unverified cleanup retains the lease.
- Wall-clock deadlines also bound a blocked stdin pipe. Failed or interrupted jobs preserve files/history. Transient runtime errors remain pending for explicit review/retry; no entire coding turn or non-idempotent tool is automatically replayed. Only official bounded request/stream retries are configured.
- Explicit provider 429/rate-limit/billing categories and available retry information are recorded. All workers sharing that organization scope become quota-blocked. This takes precedence over a pending switch; another logical worker is not a quota workaround. Owner opt-out/offline affects that worker's eligibility separately.
- The existing UI now shows LIVE CODEX / SIMULATED / HUMAN / RELAY origins, stderr/exit/timing metadata, a live phase-boundary checkbox, same-mode worker targets, full-task Git diff and the unchanged conversation ID. No dashboard redesign was introduced.

## Verification evidence

Baseline: **76 tests passed** in 66.974 seconds; `python3 scripts/demo.py` passed with five actual validator tests. During implementation, a full run found one changed-file context regression in the simulation; it was corrected and the original 11 acceptance tests passed again. The new live transport suite passed all ten scenarios before the final full run.

`tests/test_live_codex.py` exercises the **real OrganizationApiCodexAdapter** and scheduler with a faithful, explicitly mocked JSONL process replacing only Docker launch/health/cleanup. The fixture performs actual temporary-repository writes and runs two real unittest checks. It tests:

1. Unseen/freeform prompt passage, phase A, source/test changes on B, same conversation, structured progress, distinct native IDs, stderr, timings and restart replay.
2. A running process interrupted before B starts; a PID assertion verifies the previous process has stopped.
3. A real-format 429 observed just before a switch; no peer execution on shared exhausted capacity.
4. Authorization, revoked grants, cross-project isolation and no live→simulation fallback.
5. Nonzero exit despite a completion event, partial-work timeout recovery, and runtime startup that never consumes stdin.

These are transport fixtures, **not real Codex reasoning or paid inference evidence**. Production offers no mock transport switch. Default CI explicitly skips `tests/test_real_codex_smoke.py` unless the operator opts in.

## Genuine opt-in smoke

Provision the isolated proxy/image using [deploy/README.md](../deploy/README.md). Run on a trusted non-root Docker host with an authorized organization API key configured server-side. This can incur organization API charges.

```sh
# Dedicated data directory; do not share it with a running Relay process.
python3 scripts/live_smoke.py --run-live --data ./live-proof

# An unseen programming objective can be supplied as a local text file.
# Include test expectations and meaningful work for two phases.
python3 scripts/live_smoke.py --run-live --data ./live-proof-custom --prompt-file ./request.txt

# Equivalent opt-in unittest (temporary workspace):
RELAY_RUN_LIVE_SMOKE=1 python3 -m unittest discover -s tests -p test_real_codex_smoke.py -v
```

The smoke checks the image's actual CLI version, creates a fresh project and two organization-owned approved workers, submits the objective on A, waits for a real partial phase/checkpoint, explicitly switches to B, and requires real successful command/test output and completed native runs. It reopens the backend and compares transcript, files, runs and contexts. Only then does it write `live_verified: true` in `live-smoke-report.json`. No simulated workers are scheduled. Running without `--run-live` prints SKIP and makes no provider request; missing prerequisites fail closed before creating a workspace.

For the UI, launch Relay with the same configured server environment, enroll/approve two organization API workers as Jordan, and grant Alex project membership. As Alex choose A, enter an arbitrary task, check **Live demo: pause after the first phase for handoff**, and submit. At `handoff_pending`, select B and click **Switch Worker**. For a running-job demo, click Switch while activity is still arriving. Inspect HANDOFF, context, full-task diff, actual outputs and worker origins, then refresh/restart.

## Remaining limits

No Docker image build, cloud deployment, live OpenAI call, or browser visual QA was possible here. The live runner is implemented and transport-tested; genuine integration remains unverified until the opt-in smoke passes on a configured host. Model access and the contents of a reviewed image determine which languages/tests are usable. Coding containers have no general package-download network; install dependencies in the reviewed image. Data-volume filesystem quotas and backups are operator responsibilities.

Relay authentication is still local demo identity selection. It is not safe to publish this login on the Internet. A cloud VM can run the backend/workspace behind an authenticated SSH tunnel for a trusted demo; production sharing still requires real SSO, secure sessions and TLS. Personal ChatGPT/Codex subscription delegation remains unsupported. Nothing transfers inaccessible model reasoning or promises instantaneous initialization on worker B.

## Official contracts reviewed

- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive/): stdin prompts, JSONL events, explicit API automation and structured outputs.
- [Codex configuration reference](https://developers.openai.com/codex/config-reference/): custom Responses provider and bounded request/stream retry settings.
- [API rate limits](https://developers.openai.com/api/docs/guides/rate-limits): shared provider limits; a worker identity does not create new quota.


## Recorded check commands

- `python3 -m unittest discover -s tests -v`: 88 tests, 87 passed and the genuine paid integration test explicitly skipped; 87.294 seconds. Final `python3 -m unittest discover -s tests -q` rerun, including the structured-progress redaction edge case: the same 87 passed / one skipped in 84.027 seconds.
- `python3 scripts/demo.py`: PASS; five actual validator tests, 30 persisted events, A→B and reopen persistence.
- `python3 scripts/check_ui.py`: PASS for HTML IDs and JavaScriptCore syntax.
- `python3 -m compileall -q relay tests scripts deploy`: PASS.
- Ruff configured checks: PASS. Mypy on `relay/workers.py`, `relay/auth.py`, `relay/codex_events.py`: PASS. Development tools ran from a temporary installation; the application runtime remains standard-library Python.
- `python3 scripts/live_smoke.py`: SKIP without opt-in; no provider request.
- `python3 scripts/live_smoke.py --run-live --data /private/tmp/relay-live-preflight-check`: exited 2 at fail-closed preflight because organization API execution was disabled. Independent prerequisite inspection also confirmed Docker, the authorized organization key, and the proxy capability were absent. No workspace, Docker task or paid request was created by this attempt.
- `git diff --check`: PASS.
