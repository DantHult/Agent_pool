# Default IDE workflow

Supported IDE features are available after login. There is no read-only mode, edit-mode button, Git enablement setting, or console authorization button. Genuine permission and writer conflicts remain enforced.

## Start

Keep the existing data directory, account, server environment and native logins. Restart an older server gracefully when its work can be stopped, then run:

```sh
python3 -m relay.server --auth local-owner --data ./local-native-data \
  --port 9000 --public-url http://127.0.0.1:9000
```

Local-owner mode defaults to native execution. `--backend native` is still accepted. Demo/shared identities cannot enable native subscriptions; account/OS ownership, loopback restrictions, CSRF and project grants remain required. This change adds no SQLite migration beyond management migration 8 and does not touch native credential files.

## Normal use

1. Open or create a project. Create files/folders directly from Explorer, or open a source file in Monaco and type. Relay acquires its existing human reservation automatically. Save with Cmd/Ctrl+S. Save As, rename/move, confirmed delete, tabs and retained drafts keep their existing version and path checks.
2. Use Source Control immediately. Stage/unstage, commit and branch operations acquire the reservation automatically and save pending editor buffers. Status, diff, log and checkpoint inspection remain scoped read operations. At least two recorded checkpoints are needed for comparison. Branch switches still require unfinished project tasks to be resolved.
3. Open the command panel, enter an approved command and click Run. That explicit action prepares the isolated environment automatically and saves editor drafts. No separate activation is required. A clean command snapshot is refreshed automatically if saved source changed. An existing snapshot containing unpublished source edits is retained and reports a conflict rather than silently discarding them.
4. Select the intended enrolled agent and submit an objective, or use its Assign Task action. Relay saves current drafts, closes unchanged idle console snapshots, checkpoints saved changes when needed, and releases the human reservation before dispatch. Exact agent identity is preserved. Queued tasks still require explicit Start, with the same automatic preparation.
5. Follow actual visible messages, tools and results. Inspect Changes and checkpoint comparison display saved Git diffs. Handoffs remain explicit and checkpoint-based.

Command stdout/stdin, exit status, cancellation, deadline and sandbox restrictions are unchanged. Run itself authorizes only the existing session-bound project sandbox and approved argv command. The console never gains an unrestricted shell, native authentication, arbitrary network or remote Git access. **Apply & checkpoint** remains an explicit decision because it publishes generated source changes from the private command snapshot. Starting an agent refuses running commands or unpublished command changes; use Stop, Apply & checkpoint, or explicitly Close session first.

## Reservation lifecycle and real restrictions

- Opening an editable source file, performing a file mutation, or writing Git acquires a reservation automatically. Reservations remain durable while drafts or active command work exist.
- Starting/resuming/handoffing a task saves current editor buffers and releases clean human work automatically. Saving conflicts stop dispatch; the draft remains intact.
- Switching projects, signing out, or closing the last tab releases a clean reservation. Navigation preserves unsaved drafts and their reservation rather than silently saving/discarding them. Abrupt browser closure does not silently expire a reservation containing work. Refresh/restart restores drafts and reservations.
- After reauthentication, the same browser client can automatically recover its expired/revoked session's reservation. Another client or a still-valid session is never taken over automatically. **Recover previous editor session** appears only for a real same-owner conflict; it is a recovery action, not an editing mode.
- An active agent writer blocks editing. The inline restriction explains the actual state and provides **View active task / pause for editing**. After the agent confirms its pause/checkpoint, editing resumes automatically. Another user's reservation, viewer permissions, protected files and platforms without secure descriptor writes remain blocked with their actual restriction.
- Native profiles are discovered through the existing readiness checks. Unavailable profiles offer **Profile setup / resolve prerequisite**, which displays the actual reason and terminal setup instructions. Login codes and credentials never enter Relay. Existing profile ownership by another service is reported as a conflict, not treated as a missing login.
- Backend capability discovery remains automatic. New controls stay unavailable if an older running Python process lacks their endpoints; restart that process once to load the implementation. There is no user feature toggle that can bypass missing backend support.

Parallel task branches/integration remain unsupported. Project and worker writer protections retain the serialized behavior described in [IDE_MANAGEMENT.md](IDE_MANAGEMENT.md).

## Implementation and verification

The automatic lifecycle is in `relay/ide.py` (`prepare`/`finish`), `relay/console.py` (`ensure`/clean snapshot closure), and their existing HTTP routes in `relay/server.py`. Local-owner CLI startup defaults to native. `static/ide.js`, `static/app.js` and `static/index.html` remove activation controls and manage normal editing, Git, command and task actions. Existing management and filesystem APIs remain in use.

New authenticated POST routes are `/api/ide/prepare`, `/api/ide/finish` and `/api/ide/console/ensure`. They retain session, CSRF, membership, owner and reservation checks. Low-level legacy lock/console-open routes remain for compatibility and existing clients; no UI activation step uses them on a current backend.

```sh
RELAY_RUN_SANDBOX_TESTS=1 python3 -m unittest discover -s tests -q
python3 scripts/check_ui.py
.relay-native/tools/runtime/bin/node tests/activity_projection.js
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/ide_browser_smoke.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/management_browser_smoke.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 tests/browser_local_native.py
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/multi_browser_smoke.py --data ./multi-proof/fresh-default-check
```

Use your installed Playwright environment in place of the optional `/private/tmp` location. New tests cover default capabilities, local-owner native startup without a backend flag, idempotent acquisition, automatic checkpoint/release, draft retention, authenticated recovery, writer conflicts, unauthorized access, actual console closure and current-source snapshot refresh. Browser regressions assert that activation controls are absent, Monaco becomes editable automatically, commands and Git work directly, agent dispatch saves/releases drafts, and navigation/restart retains state.

Browser AI inference uses explicit fixtures; filesystem/Git operations and fixture tests are real. The IDE console walkthrough uses actual installed Codex command execution with an unauthenticated command home and real Python/Node/npm. Those results do not establish a successful subscription model turn.

The attempted genuine chosen-profile test for this delivery is recorded in ignored `ide-proof/default-native-20261024/direct-agent-report.json`: **blocked, `live_verified: false`**, because another running Relay owns native A's profile lock. That service and the existing edit reservation were left running. After finishing that work and gracefully stopping its service, use a fresh directory for the actual test:

```sh
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/agent_management_live.py \
  --run-live --profile a --data ./ide-proof/default-native-new
```

No successful new real native model turn or concurrent real-worker execution is claimed.

Delivery verification, 2026-10-24: **230 tests ran; 228 passed and two opt-in provider tests were skipped**, with actual macOS sandbox tests enabled. Ruff, mypy across nine modules, JavaScript syntax/HTML ID consistency, event projection regressions and `git diff --check` passed. Both IDE/management Chrome walkthroughs passed with zero browser errors; the IDE walkthrough verified actual Python/Node/npm, current-source command refresh, automatic draft saves and save/checkpoint/release before the mocked A → B handoff. The local-owner browser test verified active-agent write exclusion followed by automatic editing after pause. The five-context shared-account regression passed authorization, handoff, fixture-test and restart checks. Evidence is in ignored `ide-proof/browser-report.json`, `ide-proof/management-browser-report.json`, screenshots and `multi-proof/default-workflow-20261024/multi-browser-report.json`. Native subscription inference remains blocked as recorded above.

The subsequent [startup/terminal extension](TERMINAL_AND_STARTUP.md) adds Run File/F5, typed canonical Git, cancellation recovery and bounded recent-history startup. This supersedes earlier console command limitations while retaining the isolated execution and explicit publication boundary.
