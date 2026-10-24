# Relay IDE workspace

Relay's existing application now provides a local Monaco editor, project files and Git changes, a readable live agent panel, persistent project conversations, and an explicitly authorized development command console. It retains the native A/B/C App Server adapter, authentication, worker grants, event history, checkpoints, context versions and handoffs. It is not a separate application.

## Start and use it

Use the existing local-owner account and data. Do not bootstrap again or repeat Codex login:

```sh
python3 -m relay.server --auth local-owner --backend native \
  --data ./local-native-data --port 9000 --public-url http://127.0.0.1:9000
```

Stop an older process gracefully before starting this command. Keep the same environment and `RELAY_NATIVE_ROOT` when configured. Schema migration 7 retains existing conversation IDs and history while allowing additional conversations in each project. It adds durable edit reservations and per-user drafts; it does not modify native authentication.

The upstream Windows demo and portable data-directory locking are retained. On platforms without POSIX directory-descriptor protection, the IDE exposes the existing read-only workspace and Git inspection; editor reservations and writes fail closed. Native Codex execution and the privileged command console remain macOS-only. The portable fallback is regression-tested on macOS; this change does not claim a Windows end-to-end verification.

- **Explorer:** open files, navigate folders, filter names, or use **⌘/Ctrl P**. Tabs preserve open files across refreshes. Monaco and its syntax definitions are vendored locally; no CDN, npm process or external editor service is needed at runtime.
- **Edit:** click **Acquire edit lock**, then edit and **Save / ⌘/Ctrl S**. The browser synchronizes a draft separately from saving the actual project file. “Draft retained” means SQLite holds the draft. “Saved” means the authoritative file was atomically replaced. Refresh restores drafts and the lock.
- **Source Control:** inspect actual status/diff/log/branches, stage files, commit, and create/switch branches. A diff opens as a read-only, syntax-highlighted unified diff in Monaco. Existing Git checkpoint records remain available in Agents → Context & checkpoints.
- **Release:** **Checkpoint & release** saves open dirty buffers and creates an explicit Git checkpoint before allowing an agent to write. Close the command console first. Locks do not silently expire and expose unsaved work to an agent. The same authenticated user can explicitly **Recover my lock** after a new login or lost browser session; another editor cannot recover it automatically.
- **Agents:** inspect native profiles, current assigned projects, owner controls, grants and actual availability. Choose a worker, run a task, pause/checkpoint, resume, or explicitly hand off. Unknown remaining provider quota is never presented as a measured balance.
- **Conversations:** create/select persistent conversations. Each has its own messages, tool history, tasks and handoffs; all conversations in a project share its authoritative Git workspace. One unfinished task is permitted per project, including paused tasks. Finish or cancel it before starting another conversation's task.

The right agent panel and left sidebar resize by dragging their separators or using arrow keys when a separator is focused. The bottom panel resizes vertically. **Ctrl `** toggles the command console. Smaller screens use collapsible sidebar/agent overlays.

## Real command console and its restrictions

The bottom panel deliberately provides a **restricted command console**, with genuine subprocess output and stdin. It is not a general shell, xterm emulator or PTY. Resizing changes the viewport, not a remote terminal's rows/columns. Pipes, shell builtins, shell redirection, persistent shell environment changes, full-screen terminal applications and arbitrary executable paths are not supported.

Console access is available only in direct loopback **local-owner/native** mode. It is disabled in demo, invite/shared and proxied modes, independently of the native-enable environment flag. Acquire the project edit lock, save/discard drafts, then click **Authorize project console**. Authorization is bound to that authenticated Relay session, project, user and browser edit reservation; knowing a console ID does not confer access. Logout, expiration, project revocation, command deadlines, explicit stop and server shutdown stop execution. Idle console sessions close after two minutes without authenticated polling. At most two project consoles may be open.

Approved command names are:

```text
python, python3, node, npm, npx, pytest, git
```

Examples that genuinely execute when their dependencies have been prepared:

```sh
python3 -m unittest discover -v
python3 backend/manage.py check
python3 backend/manage.py test ping
npm --prefix frontend run build
node --version
npm --version
git status
git diff
git log
```

Select a subdirectory beside the command input when needed. Up/down arrows recall command history. A running process can receive text through **Send input**, end-of-input, or **Stop**. Commands are limited to 90 seconds and bounded output. Failures report their real exit status; a rejected or failed command is never replaced with simulated output.

The console uses an **unauthenticated** App Server home and a separate private snapshot repository. It copies eligible saved project files, including uncommitted source edits, and reuses approved offline dependency hydration. Protected credential paths, source Git objects/history and native profile authentication are never copied into the command snapshot. This also keeps deleted historical credential blobs out of reach. The OS permission profile allows only that snapshot, minimal system runtimes and the approved toolchain. `.git` and `.relay` remain protected, other projects are inaccessible, network access stays disabled and escalation is declined. There is no host-shell fallback. Console `git log` shows its snapshot history; use Source Control for authoritative project history.

This means `npm install` and Python package downloads cannot fetch new dependencies from inside the console. Use the existing [operator preparation workflow](NATIVE_ENVIRONMENTS.md) for committed manifests. Development servers cannot open arbitrary listening ports; use the existing separately sandboxed operator preview runner for explicit preview ports. Console commands do not gain access to Relay's listener or the host's local services.

Changes made by a command initially exist only in its private snapshot. **Apply & checkpoint** checks the original Git HEAD and a digest of all captured project source and permissions. It refuses to apply if saved project files changed, an editor draft exists, the reservation changed, or execution has not confirmed completion. Successful publication creates a canonical checkpoint and updates a paused task's versioned working context. It closes that console snapshot. Closing a console without applying leaves canonical files untouched; retained private work is not automatically replayed or published after a restart.

Git in the command snapshot is read-only. Use the constrained **Source Control** API for canonical Git writes:

| Operation | Supported arguments |
| --- | --- |
| Status | `status` |
| Diff | `diff`, `diff --staged` |
| History | `log` (last 20 entries) |
| Branches | `branch`, `branch NAME` |
| Stage | `add PATH…`, `add .` (eligible source changes) |
| Commit | `commit -m MESSAGE` |
| Switch | `switch NAME`, `switch -c NAME` |

Writes require an edit reservation and no agent writer lease or pending drafts. Branch switches are also blocked while a project task is unfinished. Git runs through Relay's trusted launcher with hooks, signing, external diff/text conversion and global/system Git configuration disabled. Arbitrary Git flags, reset/clean, remote operations, merge/rebase and forced checkout are unavailable. Read-only project viewers can inspect Git without appending conversation messages.

## Editing and concurrency boundary

File APIs check project membership and write roles, reject absolute/traversal paths and credential/runtime/metadata writes, and traverse with directory descriptors and `O_NOFOLLOW`. Symlinks and hard links cannot turn an editor path into a host-file operation. Saves compare SHA-256 versions and atomically replace files while preserving ordinary executable permissions. A mismatch returns HTTP 409 and leaves the draft intact. There is no automatic merge or silent forced overwrite.

The editor handles UTF-8 text up to 512 KB per file and 30 retained drafts per user/project. Binary files are not editable. Existing Relay snapshot/checkpoint limits still apply. The explorer lists up to 3,000 eligible files. Native authentication, server databases, `.git`, runtime directories and protected secrets cannot be edited through these APIs. `HANDOFF.md` is readable and managed by Relay.

Both human reservations and agent workspace leases are checked under the same Relay process lock. Agent assignment/start/resume/handoff and checkpoints refuse a human reservation. Human saves and Git writes refuse an active agent lease. A worker's private edits become canonical only at Relay's existing verified publication/checkpoint boundary; the editor does not expose an arbitrary live worker filesystem.

## Agent visibility and raw history

The main panel projects actual visible events into user prompts, coherent assistant responses, expandable tool operation cards and compact checkpoints/handoffs/errors. It joins `agent_delta` fragments by task, worker, native session and provider item, replaces a stream with its final response, pairs stdout/results with commands, and avoids duplicating Relay's completion-summary event. Replay/reconnection does not duplicate messages. Cards preserve expansion and scrolling while output updates.

Observable operations show actual commands, output, exit code, elapsed time and reported changed files. Human console and project Git commands carry distinct provenance. The interface does not display or invent private reasoning. Original events are unchanged. The secondary **Debug Events** tab pages through complete raw events, 100 at a time; text/type filters apply to the loaded pages.

## Verification

```sh
RELAY_RUN_SANDBOX_TESTS=1 python3 -m unittest discover -s tests -q
.relay-native/tools/runtime/bin/node tests/activity_projection.js
python3 scripts/check_ui.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/ide_browser_smoke.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 tests/browser_local_native.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/multi_browser_smoke.py --data ./multi-proof/new-ide-check
```

Use an installed Playwright environment in place of the optional `/private/tmp` path. The browser regression launches fresh Chrome contexts and temporary Relay accounts; it never attaches to a personal browser. `ide-proof/browser-report.json` and screenshots distinguish **actual isolated console execution** from **mocked AI protocol events**. The shared-account regression uses explicitly simulated inference with real fixture code/tests. No new paid model execution or physical multi-host deployment is asserted by these tests.

Monaco 0.57.0 is vendored under `static/vendor/monaco` with its license, third-party notices and npm build lock. Rebuild with `python3 scripts/build_editor.py` using the approved Node/npm toolchain. The runtime CSP permits local scripts/workers only; inline styles are permitted for Monaco layout, with no `eval`, CDN or remote worker allowance.

### Verified delivery, 2026-10-24

- Complete release suite: **192 tests, 190 passed, two opt-in provider tests skipped**. Actual macOS sandbox tests were enabled. Ruff, mypy, JavaScript syntax/HTML ID checks and the standalone event-projection regressions passed.
- Chrome IDE walkthrough: actual Monaco save/draft recovery, canonical Git staging/commit, real sandboxed Python/Node/npm, streamed output/stdin, explicit checked publication, keyboard navigation, resizing and mobile navigation passed with **zero page errors**. AI stream and A → B handoff checks used a clearly identified protocol fixture; no new model inference was claimed.
- Existing local-owner browser regression and the five-context shared-account regression passed. The latter retained its three human sessions, two simulated handoffs, five real fixture tests, unauthorized-read denial and refresh/restart assertions.
- The existing visible SQLite history projected **2,113 events into 83 structured items**, including 17 assistant messages and 40 tool cards. Original raw events were not modified.
- The idle service was reloaded at **http://127.0.0.1:9000**, migrating schema 6 → 7. Before/after digests confirmed preserved original conversation IDs, tasks, events, contexts, checkpoints, handoffs, execution runs, assignments, workers, grants, memberships, owner binding and session metadata. All three existing native profiles passed preflight without displaying identities or starting a model turn. Newer user-created checkpoints already present before this upgrade were retained.

Evidence is in ignored `ide-proof/browser-report.json`, `ide-proof/historical-projection-report.json`, `ide-proof/server-upgrade-report.json`, desktop/mobile screenshots, and `multi-proof/ide-redesign-2/multi-browser-report.json`. The canonical project was not edited by the browser tests; they used temporary data, accounts and isolated browser contexts.
