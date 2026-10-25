# Startup, Run File and the development terminal

This extends the existing Relay IDE, native adapter, reservations and restricted command runner. It does not introduce a host shell, new authentication system, provider billing path or new database schema.

## Startup diagnosis

The original frontend awaited overview/provider checks, native profiles, conversation/task metadata, full workspace inspection/diffs and every event-history page before rendering the editor. Every periodic refresh repeated much of that work. Provider readiness ran synchronously under the same lock used by file APIs. Agent text was coalesced, but thousands of raw fragments still had to be downloaded and retained first.

The initial measured baseline on a fixed **3,600-event** dataset made 46–47 API requests and transferred about **1.54 MB of API data** before an editable file was available. It performed 12 native-health calls; full workspace inspection took 150–312 ms. SQL read helpers took about 28–36 ms in total, so database execution was not the dominant delay. The existing `(conversation_id, sequence)` index already supports the new history cursors; no speculative indexes or data migration were added.

The current startup uses lightweight overview/project responses, opens the file workspace independently, and retrieves only the newest 100 events initially. Earlier history uses an authenticated `before` cursor. Original event rows are never deleted or rewritten. The activity view coalesces fragments and keeps at most 180 message/tool nodes in its paged DOM window; raw Debug Events retains its independent complete pagination.

Worker readiness is deferred and cached in memory for up to five seconds, with a shorter busy retry. Uncached probes run outside the editor/database lock. Unknown readiness is displayed as checking. Actual task assignment always revalidates current authorization and runtime health; cached UI readiness cannot authorize execution. Workspace content/diffs/context records load when requested. File-tree markup is reused when its inputs are unchanged. Streamed task events now carry their current generation, preventing Pause/Handoff from sending stale generation values after another user's handoff.

### Reproduce measurements

```sh
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/startup_benchmark.py --label after --samples 3
```

The benchmark uses fresh isolated Chrome contexts with a stable authenticated browser identity, the same persisted `ide-proof/startup-dataset.json`, a 1536×960 viewport, real HTTP/SQLite/Git and mocked native protocol peers. It measures authenticated navigation through opening an editable `hello.py`, not just an empty editor container. Reports include HTML/navigation timings, script CPU, history rendering, layout, API request latency/bytes, SQL-helper timings, workspace inspection, native-health timings, renderer JS heap and Python process peak RSS. The memory figures exclude separate browser/worker processes. No personal browser or native credentials are read or copied. Native-provider cold authentication latency is not represented by the mock protocol fixture.

Initial measurements are in ignored `ide-proof/startup-before.json` and `startup-after.json`; the final paired comparison is recorded separately. Results should be interpreted as local fixture measurements, not a guarantee for every project, provider state or machine.

### Final paired results

Original commit `8e52aad` was profiled from a temporary source checkout; the updated application used the same benchmark harness and identical dataset SHA-256. Both runs used Python 3.14.4, Chrome 132.0.6834.160, macOS on the same host, fresh browser contexts and three samples per version. The test-only Codex lookup mock was corrected identically in both harnesses; no model request was made.

| Median measurement | Before | After |
| --- | ---: | ---: |
| Editable file | 0.597 s | 0.471 s |
| First browser sample | 1.380 s | 0.820 s |
| HTML response | 1.6 ms | 1.5 ms |
| DOM ready | 200.2 ms | 183.7 ms |
| Script CPU | 86.5 ms | 80.2 ms |
| History view rendering | 32.2 ms | 4.2 ms |
| Layout | 90.5 ms | 78.0 ms |
| API requests before interaction | 46 | 11 |
| API response bytes | 1,535,403 | 53,679 |
| Initially loaded events | 3600 | 100 |
| Activity DOM items | 180 | 10 |
| SQL read helpers | 34.2 ms | 7.1 ms |
| Renderer JS heap | 13.8 MiB | 12.1 MiB |
| Python benchmark/server peak RSS | 181.6 MiB | 173.8 MiB |

Median editor interaction improved by **21.1%**. All three updated samples were below two seconds on this local fixture; this is not a claim about cold real-provider authentication or arbitrary projects. Earlier exploratory runs measured 1.156 s → 0.931 s under concurrent testing, illustrating host/cache variance. The paired raw reports and comparison are in ignored `ide-proof/startup-beforefinal.json`, `startup-afterfinal.json` and `startup-comparison.json`.

## Run a file

Open a regular project source file and use **Run File**, **F5**, or Explorer's **Run Current File** context action. Supported types are Python `.py` and Node `.js`, `.mjs`, `.cjs`. Other languages/transpilers use approved project commands such as `npm run build`; JSX/TypeScript are not passed directly to Node as if supported.

Relay saves pending buffers with their existing version checks before execution. A conflict stops the run and preserves the draft. The file's parent directory becomes its project-relative working directory, and arguments are quoted as an argv vector. Execution takes place in the existing sanitized private project snapshot. Actual stdout/stderr, exit status and runtime appear in the terminal. Python output is unbuffered by default so a long-running script does not need explicit `flush=True` to produce visible output.

Interactive scripts can receive text through the stdin form or end-of-input. **Stop**, the terminal Stop control and **Ctrl+C** request actual process termination. Command deadlines and output bounds remain enforced. A cancelled transport is replaced only on the user's next explicit Run, and only if its private source changes can be retained safely. No interrupted command is replayed automatically.

Browser refresh reconnects to the same authorized command session and reopens its terminal panel. Switching file tabs does not change the running process or its directory. Saved task/checkpoint history remains durable. A backend restart stops execution and never pretends to recover an unknown running process by replaying it.

## Typed terminal and Git

The terminal provides quoted command input, Enter, up/down history, streamed output, stdin, Ctrl+C, directory display, Clear and Copy. Clear affects the visible scrollback, not stored events. It uses text output rather than an unrestricted shell/PTY or full-screen terminal emulator.

Approved sandbox tools are `python`, `python3`, `node`, `npm`, `npx`, `pytest` and inspection-only Git inside the private runner. Typed Git commands are recognized separately and dispatched to the **authoritative project Git API**:

| Typed command | Behavior |
| --- | --- |
| `git status` / `git status --short` | Actual scoped status |
| `git diff` / `git diff --staged` | Actual source/index diff |
| `git add PATH…` / `git add .` | Stage eligible paths; directory expansion excludes protected descendants |
| `git restore --staged PATH…` | Unstage through the existing constrained API |
| `git commit -m "slight improvements"` | Create an actual canonical commit and checkpoint record |
| `git log` | Recorded canonical history |
| `git branch` / `git branch NAME` | List/create a branch |
| `git switch NAME` / `git switch -c NAME` | Switch/create-and-switch, subject to unfinished-task restrictions |

Git paths are literal and relative to the selected project directory. Quotes are parsed with `shlex`; argument vectors are never interpolated into a shell. Pipelines, shell operators, arbitrary executables, destructive Git flags and remote operations are rejected. **`git push` is unavailable**: no network capability or host Git credentials are granted. Use a separately authorized Git client outside Relay for remote pushes. Rejected commands display rejection rather than a stale successful exit code.

Canonical Git writes retain human reservations, project permissions, safe Git configuration, disabled hooks/external diff/signing and agent-writer exclusion. Folder staging cannot accidentally include `.env`, authentication, runtime or Relay metadata paths. Ordinary runtime commands remain private until **Apply & checkpoint** explicitly publishes their changes. The terminal labels canonical Git versus isolated command output so these scopes are visible.

One sandbox command session is supported per project, with at most two open project consoles. Select the project to return to its session. This is not a claim of tested parallel native AI work or arbitrary terminal tabs. The existing 90-second command deadline, 256 KB stream bounds, approved offline dependencies, workspace isolation and disabled network remain. Install/prepare dependencies through the existing [approved environment workflow](NATIVE_ENVIRONMENTS.md).

## Routes, files and checks

New authenticated POST routes are `/api/ide/terminal` (typed dispatch) and `/api/ide/run-file` (validated file execution plan). Runtime plans execute through the existing session-bound `/api/ide/console/ensure` and `/run` transport. New GET options are `overview?light=1`, `projects/ID?light=1`, `workers?cached=1`, `native/profiles?cached=1`, and conversation events with `recent=1` or `before=SEQUENCE`. Existing full/default API responses remain supported.

Implementation changes are in `relay/terminal.py`, `relay/{console,ide,server,store}.py`, and `static/{app.js,ide.js,activity.js,index.html}`. The fixture lookup repair in `tests/test_native.py` and `tests/test_local_native_dashboard.py` ensures that mocking Codex does not also replace real Git discovery. Additional coverage is in `tests/test_terminal.py`, `tests/test_ide.py`, `scripts/terminal_browser_smoke.py` and `scripts/startup_benchmark.py`. Existing browser scripts were updated to wait for intentionally asynchronous history and to match light overview URLs. An unused import was removed from `relay/accounts.py`.

```sh
RELAY_RUN_SANDBOX_TESTS=1 python3 -m unittest discover -s tests -q
python3 scripts/check_ui.py
.relay-native/tools/runtime/bin/node tests/activity_projection.js
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/terminal_browser_smoke.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/ide_browser_smoke.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/management_browser_smoke.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 tests/browser_local_native.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/multi_browser_smoke.py --data ./multi-proof/fresh-terminal-check
```

Use your installed Playwright environment in place of the optional `/private/tmp` path. Terminal proof records actual `hello.py` output, interactive greetings, both cancellations, an intentional exit 7/stderr, Node output, `npm run build`, typed Python and an actual Git commit named **slight improvements**. It also verifies clipboard/clear, rejected push, browser reconnect, history pagination and retained original events. Evidence is in ignored `ide-proof/terminal-browser-report.json` and screenshots. Browser AI handoff checks use explicit protocol fixtures or simulation; no new real model inference is claimed by these command tests.

Restart an older service gracefully when its work can be stopped, preserving its data and environment:

```sh
python3 -m relay.server --auth local-owner --data ./local-native-data \
  --port 9000 --public-url http://127.0.0.1:9000
```

No native login, Docker setting or organization API key is needed for this local command path. CLI/tool availability and approved dependencies remain real prerequisites.

Release verification: **240 tests ran; 238 passed, two opt-in provider tests skipped**, with actual macOS sandbox checks enabled. The targeted clean-snapshot/closed-transport recovery test also passed. Ruff, mypy, JavaScript/HTML consistency, event projection regressions and whitespace checks passed. The new terminal walkthrough, existing IDE walkthrough, management walkthrough, local-owner browser regression and five-context shared-account regression passed. AI protocol/simulation checks are explicitly separate from actual command/Git execution. No existing user service, conversation, authentication profile or checkpoint was replaced by these disposable tests.
