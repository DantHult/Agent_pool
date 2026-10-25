# Relay IDE and agent management

This extends the existing Relay dashboard, scheduler and native App Server adapter. It retains Monaco, cookie sessions/CSRF, owner-pinned native subscriptions, sandbox permissions, immutable event history, Git checkpoints and explicit account handoffs.

For the current activation-free editing/command/task lifecycle, see [DEFAULT_WORKFLOW.md](DEFAULT_WORKFLOW.md).

## Launch

Use the existing account, native logins, data directory and server environment. Once you can stop the existing service gracefully, restart it with:

```sh
python3 -m relay.server --auth local-owner --backend native \
  --data ./local-native-data --port 9000 --public-url http://127.0.0.1:9000
```

Open **http://127.0.0.1:9000** and sign in to Relay. Do not bootstrap an existing data directory or repeat native login. Migration 8 adds archive/removal timestamps and durable requested-worker IDs. Existing conversation IDs, events, checkpoints, handoffs, grants, accounts and sessions remain in place. Existing editor reservations/drafts survive restart.

An older running Python process can serve the new JavaScript before restart. The frontend checks backend capabilities and disables new operations until the service is restarted. Existing editor and handoff controls continue to work. The implementation does not automatically stop your running service or release your edit reservation.

## Files and Git

Explorer and Git operations acquire the necessary reservation automatically; opening an eligible source file enables editing immediately. **New File** creates an actual empty file; **New Folder** creates an actual folder, including empty folders. Folder parents must exist. The file tree and its right-click menu support opening files, creating children, moving/renaming and confirmed deletion. The editor toolbar exposes Save, Save As, Rename / Move and Delete. Save As creates a new copy and retains any original unsaved draft. It never overwrites an existing destination. Tabs show dirty drafts; closing a tab retains its draft. Drafts are synchronized to SQLite and remain when changing conversations or refreshing the browser.

Rename/move and delete first inspect the affected file/tree and send its version. A changed version returns a conflict. Affected drafts must be saved or discarded first, including drafts held by another user. Folder deletion requires confirmation and refuses protected descendants, links and hard-linked files. Atomic no-overwrite rename protects existing destinations, including empty folders. Empty folders persist on disk across browser/server refreshes; Git itself does not track empty directories.

The APIs use the existing project membership, durable human reservation and directory-descriptor protections. They reject traversal, case variants of protected names, symlink escapes, credentials, `.git`, and Relay metadata writes. Structural operations are limited to 3,000 entries/20 MB, with 512 KB per file. File failures are reported as errors. No file API acquires native authentication or host-shell access. Secure writes remain unavailable on platforms without POSIX descriptor protection.

Source Control provides status, working/staged diffs, history, staging/unstaging, commits and branch creation/switching. Checkpoint selectors compare two recorded project commits. **Inspect Changes** reviews the first-to-last checkpoints associated with that task, rather than including unrelated later project commits. It shows saved changed paths and a Git diff. Authorship is attributed to recorded human operations, workers and checkpoints; Relay does not infer line-level authorship.

## Agents and direct assignments

**Agents → Add Agent** enrolls a named worker. Native enrollment offers only existing authenticated, preflight-ready, unenrolled A/B/C profiles. Duplicate enrollment is rejected server-side. If setup is needed, run:

```sh
python3 scripts/native_profiles.py init
```

Run the printed login commands privately in your terminal. The dashboard never accepts, displays or copies ChatGPT credentials. Existing enrolled profiles can be explicitly approved for additional projects. The optional organization API backend retains its separate owner/admin and runtime requirements. Unsupported personal delegation cannot be enabled from the enrollment form.

Agent cards show their profile/provider, configured model when known, backend availability, current task/project/conversation, execution state, last observed event/command, activity timestamp, elapsed run time, reported file changes and checkpoint. Availability includes authorization, opt-in, scheduler occupancy and runtime/sandbox readiness. An authenticated login alone is insufficient. No hidden reasoning is shown. Commands can change files without emitting structured file-change events; a missing reported-file count is not presented as a clean workspace.

Click **Assign Task** or **Queue Task** on the intended agent. Enter instructions and choose a new conversation or an existing idle conversation. Immediate dispatch validates the chosen agent and project writer state. Queueing records the chosen worker without starting execution. **Start chosen agent** rechecks all permissions and starts only that worker. Relay never substitutes a worker or dispatches queued tasks automatically, including after restart. Choose an explicit handoff target to authorize a different worker.

The project task list shows pending and historical tasks across conversations. History opens the correct conversation. Running tasks can pause/checkpoint or cancel; safely paused tasks can resume on the same eligible worker. Cancellation waits for the existing adapter's stop/publication confirmation. Cancelling a queued task does not checkpoint or touch another writer's files. Errors remain in task/event history. Agent **View activity** opens its recorded task/conversation; the right panel retains readable streamed messages and expandable tool outputs.

Before immediate task dispatch, the UI saves current drafts and releases its human reservation automatically. Running commands and unpublished command changes remain explicit conflicts.

Owner settings change the display name, project grants, opt-in and availability. **Remove from project** revokes that project's grant and stops affected execution safely. **Remove enrollment** requires confirmation and refuses unfinished assigned/queued work. Cancel and wait for a checkpoint, or hand off the task first. Removal is a tombstone: historical worker IDs, messages, attribution and checkpoints remain. External accounts and native authentication files are untouched. The same profile can be explicitly enrolled again under a new worker ID.

## Conversations and concurrency

Conversations can be created, renamed, opened, archived with confirmation, shown again and restored. Archive is the supported removal operation; histories are not destructively deleted. Unfinished tasks prevent archive. Participants come from actual assignment history. Archived conversations are readable; restore them before starting a task. Project/task/worker/conversation IDs remain separate.

**Same-project parallel execution and branch integration are unsupported.** This implementation takes the brief's serialized fallback. Existing project writer leases and human reservations remain enforced. Paused unfinished work still reserves project task ownership. Multiple tasks can queue in separate conversations, but only one task may write a project's canonical workspace. The scheduler retains separate-project execution capability and one task per worker; concurrent real native processes across projects were not verified in this delivery. No claim of tested parallel native execution is made.

Task branches, merge conflict approval and branch integration are not implemented. No integration action is offered. Arbitrary merge/rebase, destructive Git commands, remote operations, unrestricted shell/PTY and network package installation remain unavailable. The existing restricted console and operator-approved offline dependency workflow are unchanged.

## Backend operations

All routes use the existing authenticated API, Origin/Host validation, CSRF for POST and server-side project/worker permissions.

| Method | Route | Behavior |
| --- | --- | --- |
| GET | `/api/ide/entry?project_id=…&path=…` | Versioned file/tree inspection for structural operations |
| POST | `/api/ide/files` | `mkdir`, exclusive `rename`, confirmed/versioned `delete` |
| POST | `/api/ide/conversations/update` | Rename, archive or restore a scoped conversation |
| POST | `/api/workers/remove` | Confirmed enrollment tombstone; refuses unfinished work |
| POST | `/api/tasks/assign` | Exact worker, instructions, optional conversation, explicit `queue` |
| POST | `/api/tasks/start` | Explicit dispatch of persisted chosen-worker intent |
| GET | `/api/tasks?project_id=…` | Scoped task queue/history across conversations |
| GET | `/api/ide/review?project_id=…&task_id=…` | Task checkpoint review |
| GET | `/api/ide/review?project_id=…&before=…&after=…` | Compare two recorded project checkpoints |
| POST | `/api/ide/git` | Existing constrained operations plus `unstage PATH…` / `unstage .` |
| GET | `/api/workers`, `/api/overview` | Existing responses now include observations and capability discovery |

## UI control audit

| Controls | Verified operation and coverage |
| --- | --- |
| New Project, project selection | Actual project creation and authorized workspace loading; management Chrome walkthrough |
| Explorer toolbar/filter/tree/context menu | Create/read/update/exclusive rename/delete, nested/empty folders, filter, refresh and modification indicators; explorer unit/HTTP tests and Chrome walkthrough |
| Editor tabs/close, Save, Save As, Discard, keyboard save/quick-open | Monaco models, draft retention, exclusive copy, version checks; editor tests and both IDE browser walkthroughs |
| Automatic reservation/recovery | Durable session-bound reservation, automatic save/checkpoint/release and agent-write exclusion; editor, console and browser regressions |
| Add Agent and enrollment form | Real enrollment, authenticated profile discovery, readiness and uniqueness checks; native management HTTP tests and management browser walkthrough |
| Agent Assign/Queue, assignment form, View activity | Explicit worker identity, persistent task/conversation and actual dispatch/state; management tests and Chrome walkthrough |
| Agent name, availability, opt-in, grants, project removal, enrollment removal | Owner/admin mutation, safe stop, confirmation and historical retention; permissions/native tests and management browser walkthrough |
| Task Start/History/Inspect Changes/Pause/Resume/Cancel | Chosen-agent dispatch, scoped history/diff, confirmed adapter stop and persisted state; management/native scheduler tests and browser regressions |
| Handoff target/reason/Request Handoff | Existing generation-checked explicit handoff and checkpoint/context transfer; native adapter, local-owner and shared browser tests |
| Continue, Simulate Limit, Run Demo | Enabled only for supported simulation states; existing scheduler, permissions and multi-browser tests |
| New/open/rename/archive/restore/show archived conversation | Scoped persistent histories and participant attribution, draft preservation, unfinished-task protection; management tests and Chrome walkthrough |
| Status/diff/staged diff/log/branches, stage/unstage, commit, branch create/switch | Actual constrained Git operations with writer/conflict checks; editor/management tests and Chrome walkthrough |
| Compare checkpoints, Inspect Changes | Scoped recorded commits and saved change sets; management tests and browser walkthrough |
| Console run/cwd/stdin/end input/stop/apply/close/history | Actual isolated approved commands, explicit checked publication and session cleanup; real sandbox tests and IDE Chrome regression |
| Debug paging/type/text filters, artifacts, file links, expandable operations, Jump to latest | Stored events/artifacts and keyed structured projection; event projection tests and IDE browser regression |
| Activity tabs, settings, panel collapse, separators, mobile navigation | Existing pane/layout handlers, pointer/keyboard resize and overlays; IDE browser regression |
| Sign-in/out, invitation/access forms, identity selector | Existing authentication/RBAC; account, local-owner and five-context shared browser regressions |
| Unsupported platform writes, delegation, task-branch integration, unready controls | Disabled with explanations; portable integration/native permission tests; older-backend browser check |

The audit combines behavioral backend tests, static HTML/handler consistency checks and browser workflows; it does not claim every permutation of every form input was browser-tested.

## Verification and real execution boundary

```sh
RELAY_RUN_SANDBOX_TESTS=1 python3 -m unittest discover -s tests -q
python3 scripts/check_ui.py
.relay-native/tools/runtime/bin/node tests/activity_projection.js
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/management_browser_smoke.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/ide_browser_smoke.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 tests/browser_local_native.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/multi_browser_smoke.py --data ./multi-proof/new-management-check
```

Use your installed Playwright environment instead of the optional `/private/tmp` path when appropriate. Browser tests use disposable accounts/projects and mocked native inference, or explicitly simulated shared workers. Git/filesystem operations and fixture Python tests are actual operations. The IDE console regression executes actual approved Python, Node and npm in the unauthenticated Codex command sandbox. None of these is evidence of a new successful subscription model turn.

For a genuine chosen-profile task through the **Add Agent → Assign Task** interface, first finish active work and gracefully stop any other Relay holding that profile. Then use a fresh proof directory:

```sh
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/agent_management_live.py \
  --run-live --profile a --data ./ide-proof/direct-native-new
```

This creates a separate temporary owner account and coding project, reuses the existing native login without copying it, requires actual native completion and unittest output, verifies the selected worker and checkpoint diff, and checks browser refresh. It does not stop other services or fall back to mocks. The original service can then be launched again with its original data/environment.

The attempted native-A proof on 2026-10-24 was **blocked**: another running Relay owns its profile lock. The existing service also has a durable human edit reservation. It was left running, with authentication and project state untouched. `ide-proof/direct-native-20261024/direct-agent-report.json` records `live_verified: false`. No new actual native AI task or parallel real-worker execution is claimed.

Implementation files are `relay/file_operations.py`, `relay/management.py`, `relay/ide.py`, `relay/store.py`, `relay/migrations.py`, `relay/server.py`, and `static/{app.js,ide.js,index.html,style.css}`. New coverage and reproducible checks are `tests/test_management.py`, `scripts/management_browser_smoke.py` and `scripts/agent_management_live.py`.

Delivery checks on 2026-10-24: the complete suite ran **217 tests: 215 passed, two opt-in provider tests skipped**, with actual macOS sandbox checks enabled. The 16 management unit tests were also rerun after the final migration/draft/status refinements. Ruff, mypy (nine modules), JavaScript syntax/HTML ID consistency, event projection tests and `git diff --check` passed. The management walkthrough, existing IDE/real command-console walkthrough, local-owner browser regression and five-context shared-account regression passed; the management and IDE walkthroughs reported zero browser errors. Evidence is retained in ignored `ide-proof/management-browser-report.json`, `ide-proof/browser-report.json`, screenshots and `multi-proof/management-20261024/multi-browser-report.json`. The new native-A attempt remains explicitly blocked as described above.
