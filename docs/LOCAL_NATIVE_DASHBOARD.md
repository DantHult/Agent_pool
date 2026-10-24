# Authenticated local-owner native dashboard

Updated 2026-10-24. This is an incremental extension of Relay, using its existing native App Server adapter, event stream, Git workspaces, writer leases, checkpoints, contexts and handoffs. **A real owner-authenticated Chrome A → B coding/handoff/reload/restart test passed**, with five generated tests. Shared/invite execution remains separate and cannot use personal native profiles.

## Ownership boundary

Use **`--auth local-owner --backend native`** on the Mac that already owns the authenticated profiles. An offline bootstrap pins one real password-authenticated Relay account to the current OS UID in schema migration 6. Startup requires that pin and its one active account. Only that account can log in or resolve a session. It cannot register/invite another person, change the pin over HTTP, or delegate native work to another Relay account.

The listener is fixed to `127.0.0.1`. This mode accepts only a direct `http://127.0.0.1:PORT` or `http://localhost:PORT` origin matching that listener, rejects public/LAN/HTTPS-proxy origins and all `Forwarded`/`X-Forwarded-*` requests, and checks the loopback peer, Host and mutation Origin. It reuses hashed passwords, hashed revocable/expiring server sessions, HttpOnly/SameSite cookies, rate limits, CSRF tokens and project authorization. Each stream rechecks the authenticated owner/session/project. A separate cookie name prevents confusing the local-owner session with demo/shared sessions.

This pin is the trusted local operator's assertion that they control these local subscriptions; it is not a provider-issued cross-person delegation grant or a claim that Relay account email equals every Codex account email. Native profile directories must still be owned by the same OS user with mode 0700. Do not share the Relay password or expose/tunnel/proxy this instance. Loopback checks cannot detect a deliberately hidden tunnel or protect against a malicious administrator/local process. The mode assumes a trusted local machine and operator.

**Demo login can switch identities, so native enrollment/execution is now disabled in both demo and invite/shared dashboards.** `RELAY_ENABLE_NATIVE=1` alone does not bypass that HTTP policy. The original explicit local CLI smoke remains supported under its existing owner/environment rules; it has no shared browser identity endpoint.

## Start without changing Codex login state

Run from the existing repository on the profile-owning Mac. Do not copy `.relay-native`, read/upload auth files, or run Codex login/logout for this feature. Existing profiles remain under the configured `RELAY_NATIVE_ROOT` (default `.relay-native`). Check them **before** starting the dashboard, because a running Relay exclusively locks its active profiles:

```sh
python3 scripts/native_profiles.py check --profile a
python3 scripts/native_profiles.py check --profile b
python3 scripts/native_profiles.py check --profile c
# Or check all profiles (including distinct-account validation):
python3 scripts/native_profiles.py check
```

These commands use the official App Server to inspect existing auth, sandbox/runtime support and thread creation, without starting model turns or changing login state. While the dashboard holds those profile locks, use its profile panel instead of starting a second CLI checker. Missing authentication/CLI/directories are actionable unavailable states; Relay does not automate a login or substitute simulation.

Create a dedicated local-owner Relay database, separate from shared/demo data and prior proof:

```sh
python3 scripts/manage_accounts.py bootstrap --local-owner --data ./local-native-data --email owner@example.com --name 'Local Owner'
# Enter a NEW Relay-only password interactively (12+ characters).
python3 -m relay.server --auth local-owner --backend native --data ./local-native-data --public-url http://127.0.0.1:8000
```

Open http://127.0.0.1:8000 and sign in with that Relay account/password. It never requests a ChatGPT password, device code, cookie or token. The explicit origin overrides any shared deployment `RELAY_PUBLIC_URL` export. An environment file can still configure `RELAY_NATIVE_ROOT`, `RELAY_CODEX_BIN` or optional `RELAY_NATIVE_MODEL`/`RELAY_NATIVE_TIMEOUT_SECONDS`; no API key, Docker or required model setting is needed. `RELAY_NATIVE_OWNER` remains a legacy CLI setting; this dashboard uses the offline database pin rather than a switchable demo ID.

The bootstrap refuses an existing account database rather than repinning another user's account or replacing prior history. Use a fresh dedicated directory. Local data and native proof directories are ignored by Git, and account files/directories retain private permissions. Sessions can be revoked with Sign out, `auth/logout-all`, or the existing offline `manage_accounts.py revoke-sessions` command after stopping the server.

## Run, pause and explicitly continue

1. Create a disposable sample project or use a trusted authorized repository already managed by Relay.
2. The **NATIVE LOCAL profiles** panel shows A/B/C as available, busy, paused, unauthenticated, unavailable or quota_blocked, with the pinned owner and a reason. Exact remaining subscription quota is unknown, not invented. Select available A for the project; this enrolls/grants a native worker without invoking a model.
3. Enter an arbitrary coding prompt. Select two planned phases and **Pause after a useful phase** for a repeatable handoff. **Run task** invokes the existing native App Server adapter under the selected isolated `CODEX_HOME`; no fixture task is substituted.
4. Watch persisted **NATIVE LOCAL** agent/tool/test/error events. Task-level errors and phase boundaries remain saved. For an in-flight stop, **Pause / Checkpoint** requests `turn/interrupt` and waits for confirmed completion before publishing/checkpointing; it does not claim to freeze arbitrary tools instantly.
5. At a saved boundary, click **Prepare B for handoff**, then **Request Handoff**. Preparing the successor only enrolls/grants/selects it; it does not execute. The explicit handoff action requires owner/session/CSRF/project/worker eligibility checks again. The conversation ID stays unchanged. B receives a new native thread, committed project state, current Git information, versioned concise context, objective, decisions, pending work/tests/blockers, HANDOFF.md and references to the complete visible transcript.
6. B finishes and runs tests. Inspect Workspace artifacts, full-task diff, HANDOFF, context versions, test output and checkpoints. Refresh/restart with the same local-owner data directory: session (unless expired/revoked), conversation, files, run outcomes and history persist.

**Resume paused worker** starts a fresh thread for the same eligible current profile; native thread transfer between accounts is never asserted. Paused/offline worker availability is owner controlled. If its logical worker was paused by a previous handoff, use its owner settings to make it available after reviewing capacity. A profile rejected by actual provider quota cannot be switched to another account on that task as a limit workaround. No automatic account rotation occurs; retry requires explicit owner action after capacity is legitimately available.

## Isolation and failure behavior

Relay reuses `relay/app_server.py` and `relay/native.py`; it does not start a competing runner. One App Server per profile and its existing process/file locks are reused. Every turn operates in a separate private Git clone with tightly scoped native permissions. Coding tools cannot read profile credentials, other profile workspaces, the canonical project or server auth data. Git/handoff metadata is read-only. Native preflight checks the actual sandbox, Python runtime and thread initialization. The trusted App Server retains its own local authentication; API keys/proxy secrets are not forwarded to it. Auth messages/raw account objects/hidden reasoning are not persisted or transferred.

Canonical project publication and checkpoints remain scheduler-owned, with writer leases and checkpoint conflict checks. A confirmed interrupted turn can publish verified partial work; an unconfirmed interruption, CLI crash or disconnect retains private work without an unverified publish. Native failures checkpoint the canonical state and remain `handoff_pending` with error evidence and `last_error`, rather than silently completing or falling back. Recovery never replays unknown tools. User pause, cancellation, quota and restart behavior are tested separately from successful model execution.

Native execution provides macOS filesystem/network controls and deadlines, not Docker CPU/memory isolation. Dependencies must exist in allowed runtimes/workspace; some developer tools or shell temporary-file mechanisms can be denied. Owner review of model-generated code, tests and diffs is still necessary. Public/shared worker hosting is not enabled by this mode.

## Actual verification

The following command was executed with installed Chrome/optional temporary Playwright and the existing authenticated native profiles:

```sh
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/local_native_dashboard_smoke.py --run-live --data ./native-dashboard-proof
```

**PASS, exit 0:** real owner-authenticated web UI; actual A and B Codex execution on distinct existing ChatGPT accounts; A unfinished code and checkpoint; successor preparation without execution; explicit owner handoff; B completed implementation and five passing Python tests; two distinct native thread IDs; nonempty patches from both; 538 persisted events; browser reload and backend restart preserving events, contexts, files and checkpoint/run records.

- Report: `native-dashboard-proof/local-native-dashboard-report.json`, `live_verified: true`. SHA-256: `9d9d82ad84b9e94e50d5f847e984ad145cd0d395662087759b7fcbcf8fcc1921`.
- Screenshot: `native-dashboard-proof/native-dashboard-proof.png`.
- Conversation: `164fb0824b6d4b4cbb373a42d4717ad4`.
- A → B handoff checkpoint: `5fe1f62d4f40b2674c51ce11e1306532409dc97b`.
- Final checkpoint: `bdf69fa5f134cd8bc7c23635e5003f35fae62218`.
- A duration 33.704 seconds; B duration 26.969 seconds. Both completed with exit code 0.

The generated sample `word_stats.py` implements Unicode-aware word/unique-word counts and first-longest-word selection, with empty/whitespace/Unicode/tie tests and a README example. It lives in a disposable server project/private task clones, not Relay's source tree. Codex login state was not changed. No credentials or raw identity responses enter the proof report.

To reproduce, install optional Playwright into a test environment (not the application runtime), provide installed Chrome, and choose a fresh proof directory:

```sh
python3 -m pip install --target /private/tmp/relay-browser-tools playwright
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/local_native_dashboard_smoke.py --run-live --data ./native-dashboard-proof/new-run
# --chrome /path/to/chrome and --prompt-file ./objective.txt are supported.
```

This explicitly consumes the existing profiles' Codex allowance. Without `--run-live`, the script skips without logging in or invoking a model. It refuses to overwrite prior evidence. Browser/account test credentials are ephemeral, private and never printed; no personal browser profile is loaded. Failures leave `live_verified: false` and private durable evidence. The prior local native A → B → C proof and shared simulated browser proof are preserved separately.

## Changed files and automated checks

This milestone changes `relay/accounts.py`, `relay/migrations.py`, `relay/server.py`, `relay/store.py`, `relay/native.py`, `scripts/manage_accounts.py`, `scripts/native_profiles.py`, `static/index.html`, `static/app.js`, `static/style.css`, `.gitignore`, `README.md`, `deploy/README.md`, `docs/AUTH_EXECUTION_COMPATIBILITY.md`, and `docs/IMPLEMENTATION_STATUS.md`; adds `scripts/local_native_dashboard_smoke.py`, `tests/test_local_native_dashboard.py`, `tests/browser_local_native.py` and this guide; extends the existing test-only peer `tests/fixtures/native_app_server.py` with a crash scenario. Existing unrelated shared-server work/data/proof is retained.

Automated local-owner tests mock only the CLI protocol process while exercising real HTTP, accounts, CSRF, project authorization, private files/Git, writer leases, pause/cancel, explicit A → B, CLI crash, quota, concurrent-writer denial and restart recovery. They prove no raw credentials/account identity/reasoning reach project APIs/history/downloads/Git. Nonowner HTTP requests and shared/demo/public/proxy/mismatched-OS modes fail closed. These are mocked execution tests, explicitly distinct from the genuine browser/native proof above. Full repository command `python3 -m unittest discover -s tests -q`: **136 tests in 223.117 seconds; 134 passed and two opt-in genuine-provider tests skipped**. This includes the eleven new pinned local-owner HTTP scenarios and all previous native/shared/simulator/API tests. The finalized native/pinned-owner suites then passed all **22 tests in 75.741 seconds**, and a targeted direct-HTTP existing-worker denial test passed again after adding demo/shared execution/handoff assertions.

Ruff configured checks, Mypy on `relay/accounts.py`, `relay/auth.py`, `relay/server.py`, `relay/workers.py`, `relay/native.py`, `relay/app_server.py`, compilation and HTML/JavaScriptCore checks passed. The actual read-only `scripts/native_profiles.py check --profile a`, `b`, and `c` commands all passed with the existing logins. The real native browser result above is separate from these mocked regression checks.


The final browser timing/busy-label/pause regression is reproducible without a real model call:

```sh
PYTHONPATH=/private/tmp/relay-browser-tools python3 tests/browser_local_native.py
```

**Observed PASS** after correcting an initial-load selection timing race. It uses real isolated Chrome with the test-only native peer, verifies initial profile selection is retained after loading, preserves NATIVE LOCAL labeling while busy, and exercises owner-requested Pause/Checkpoint. It is separate from the real native A → B proof above.

For approved Node/npm, private Python environments, dependency preparation across handoffs and Real Codex Test environment diagnosis, see [Native development environments](NATIVE_ENVIRONMENTS.md). That guide distinguishes environment readiness from actual endpoint/build/browser verification.
