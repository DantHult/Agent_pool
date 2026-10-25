# Shared invite-only HTTPS deployment

For multiple human computers, start with [MULTI_COMPUTER.md](../docs/MULTI_COMPUTER.md). It contains exact account bootstrap, HTTPS reverse proxy/service commands, safe LAN testing, roles, invitations, authorized repository import and three-device demo instructions. Use `--auth invite` (the CLI default), persistent storage and the supplied `shared.env.example`, `Caddyfile.shared` and `relay.service`. Shared execution uses the optional organization API backend below. Native consumer profiles are disabled in shared mode and must never be uploaded/copied to a server.

Observed evidence: three member browser sessions plus independent Organizer/Outsider sessions passed a real-Chrome fallback on one Mac with simulated inference, real file/checkpoint/test activity and restart persistence. Real physical multi-host/live organization execution remains **NOT VERIFIED**. See [MULTI_COMPUTER_STATUS.md](../docs/MULTI_COMPUTER_STATUS.md).

The local-owner native dashboard now requires `--auth local-owner --backend native` and an offline-pinned account; it rejects demo/shared/public/proxied use. See [LOCAL_NATIVE_DASHBOARD.md](../docs/LOCAL_NATIVE_DASHBOARD.md). It does not authorize cross-person subscription execution.

# Native Codex on macOS and Windows

Relay can run three local `codex app-server --stdio` processes. Each process has its own private `CODEX_HOME`, independent ChatGPT login and private Git clones. The native backend needs Python, Git, and the official Codex CLI on macOS or Windows. **Docker, `RELAY_ORG_OPENAI_API_KEY`, `RELAY_CODEX_MODEL`, the inference proxy and separate API billing are not required.** The default model comes from Codex; `RELAY_NATIVE_MODEL` is optional. macOS uses Seatbelt permissions. Windows uses the Codex CLI sandbox on that host. Both refuse a model turn until the workspace isolation probe passes. The human project console and preview sandbox remain macOS-only.

This is a local client for the person controlling the signed-in accounts. The configured Relay owner (default `alice`, displayed as Alex) must initiate tasks and explicitly select handoff targets. A different Relay user, including an administrator, cannot spend these subscriptions. This does not enable hosted cross-user subscription pooling or automatic account rotation when usage limits are reached.

## Authenticate three independent profiles

From the repository root, with `codex` on PATH:

```sh
codex --version
python3 scripts/native_profiles.py init
CODEX_HOME="$PWD/.relay-native/profiles/a" codex -c 'cli_auth_credentials_store="file"' login --device-auth
CODEX_HOME="$PWD/.relay-native/profiles/b" codex -c 'cli_auth_credentials_store="file"' login --device-auth
CODEX_HOME="$PWD/.relay-native/profiles/c" codex -c 'cli_auth_credentials_store="file"' login --device-auth
```

Complete each interactive login with a different ChatGPT Codex account you control. Check the browser account for each login; an existing browser session can otherwise sign you into the same account repeatedly. If device authorization is unavailable for your account, omit `--device-auth` and complete the normal browser flow with the same `CODEX_HOME` and file-store setting. Never paste codes, tokens, auth files or cookies into Relay/chat/Git. Never copy `~/.codex/auth.json` or credentials between profiles. The helper prints commands using the configured absolute paths if you set `RELAY_NATIVE_ROOT`.

The explicit `file` credential store keeps each login in its own owner-only profile instead of allowing the OS keychain to share login state. `.relay-native/` is Git-ignored; the helper creates each profile/workspace directory with mode 0700. Relay does not read, export or import the credentials. The CLI manages them. Relay uses `account/read` to check ChatGPT auth and retains only an in-memory account-identity digest to reject duplicate accounts; identity values are never displayed or persisted.

Check readiness without a model turn:

```sh
python3 scripts/live_smoke.py --backend native --check
# Equivalent profile-specific diagnostic:
python3 scripts/native_profiles.py check
```

Preflight starts three local App Servers, performs initialization, checks the named permission profile and Python test runtime, checks each workspace with a real OS sandbox probe, creates an ephemeral thread with the same native parameters used for coding, and verifies distinct ChatGPT logins. It does not call `turn/start` or establish model availability, subscription capacity, or successful coding. Missing login, inaccessible workspace or unsupported sandbox/protocol fails closed. Codex CLI **0.163.0** was used for protocol/schema and sandbox checks; the required named permissions currently use its experimental App Server capability. Upgrade/review the installed CLI if these checks fail; Relay does not fall back to unsandboxed host execution.

## Real A → B → C acceptance command

After all three profiles pass readiness:

```sh
python3 scripts/live_smoke.py --backend native --run-live --data ./live-proof
# Optional custom objective with enough code/test work across three phases:
python3 scripts/live_smoke.py --backend native --run-live --data ./live-proof-custom --prompt-file ./request.txt
```

This explicitly authorizes consumption of the signed-in accounts' Codex allowance. Use a fresh dedicated data directory, separate from a running web server. The script enrolls A/B/C, requests useful unfinished work from A and B, checkpoints each boundary, and explicitly continues the same Relay conversation on the next profile in a **new native thread**. It requires actual project changes from every worker, two checkpointed handoffs, three completed native turns, successful C test-command output, and persistence after reopening the database. A model that prematurely finishes all work causes the smoke to fail rather than claiming unfinished-work continuation.

Only a fully verified run writes `"live_verified": true` to `live-proof/native-smoke-report.json`. Failed execution keeps events, checkpoints and private work for inspection. No failure substitutes simulation or Docker/API execution. Existing incomplete tasks are not automatically replayed.

For the dashboard:

```sh
python3 scripts/manage_accounts.py bootstrap --local-owner --data ./local-native-data --email owner@example.com --name 'Local Owner'
python3 -m relay.server --auth local-owner --backend native --data ./local-native-data --public-url http://127.0.0.1:8000
```

Sign in with the dedicated offline-pinned Relay owner account. Use the **NATIVE LOCAL profiles** panel to select a profile for the project or prepare a successor, then explicitly request the handoff. Demo/shared identity selection cannot enroll or execute native workers. Choose a native worker explicitly in the composer; automatic demo selection still supports the simulation. Select the pause-after-turn option for a planned handoff, then explicitly choose another native worker and press **Switch Worker**. The UI uses two phases by default; the smoke requests three. For custom configuration, both scripts and server accept `--env-file .env`; only load a local file that you have configured. No `.env` is needed for the native commands above.

## Native execution boundaries and recovery

The trusted App Server process needs its own credentials to contact Codex. Coding tools get a separate private Git clone, minimal runtime reads, no other profile, no credential home, no canonical Relay project path, no MCP servers, and no tool network access. Filesystem rules make `.git` and Relay handoff/history metadata read-only. An isolation probe must prove that tools can write their own workspace and cannot read nonsecret canaries in the profile home or another worker’s workspace. The Codex executable itself is readable so its sandboxed filesystem helper can run; no credential-directory read grant is added. The server’s Python installation is also allowed as a runtime read and tested in preflight, so macOS framework/Homebrew Python can execute tests. Broad home/root runtime prefixes are refused. Server-side approval requests are declined; external auth-token requests are rejected. Native execution does not impose the optional Docker backend's CPU/memory/container limits. Dependencies needed for a task must already be available within the allowed runtime/workspace.

Visible agent messages, command/output events, file changes, errors and native thread IDs feed the existing durable event stream. Reasoning and auth notifications are discarded. The child environment contains its own `CODEX_HOME` and a small OS allowlist; inherited API keys and proxy secrets are excluded. Handoff transfers committed project changes, visible history, objective, tests and pending steps, never authentication state or hidden reasoning.

Interruption uses `turn/interrupt` and waits for `turn/completed`. Only verified completion/interruption allows validated private changes to be applied to the canonical project and checkpointed by the scheduler. If interruption cannot be confirmed or the process disconnects, the private clone is retained without publishing uncertain changes. A process crash cannot leave a native tool writing to the canonical worktree. Restart marks interrupted runs for explicit review; it never replays tools or automatically starts a replacement account. Usage-limit failures block the affected profile and do not trigger account rotation.

The local demo identity selector is not production authentication. Keep this service on loopback. For exact current verification evidence, see [LIVE_CODEX_STATUS.md](../docs/LIVE_CODEX_STATUS.md). **Real native inference and A → B → C continuation passed on 2026-10-24**, using three distinct ChatGPT profiles, two checkpointed handoffs and seven generated tests. Each installation still needs its own interactive logins and passing acceptance command.

The optional organization-funded Docker backend is preserved below.

# Optional Docker / organization API backend

The credential-free simulation needs none of this setup. This optional runner uses the documented official `codex exec --json` interface, pinned to the installed CLI version checked during implementation. It has **not been live-tested here**: Docker and an authorized organization API credential were unavailable. Adapter protocol, streaming completion checks, cancellation, redaction, network guards, and proxy forwarding are covered by deterministic tests.

This setup is for one trusted organization per proxy stack. The server's organization credential reference is fixed to `env:RELAY_ORG_OPENAI_API_KEY`. Do not use local `~/.codex` state, employee subscription credentials, browser cookies, or personal tokens.

## Local setup and readiness checks

The smoke script does not load `.env` unless `--env-file` is supplied. A bare `--run-live` command cannot see settings that exist only in that file. Existing exported shell variables take precedence. Environment files accept only supported Relay settings, are parsed without executing shell code, and never print their values.

1. Install and launch Docker Desktop using the [official Mac instructions](https://docs.docker.com/desktop/setup/install/mac-install/). Confirm `docker info` succeeds.
2. If `.env` does not exist, copy `.env.example` to `.env` locally and restrict permissions to the owner (`chmod 600 .env`). Set `RELAY_ENABLE_ORG_API=1`, the explicitly authorized `RELAY_ORG_OPENAI_API_KEY`, and `RELAY_CODEX_MODEL` available to that API project. Set a private random `RELAY_CODEX_PROXY_TOKEN`; the proxy and backend must use the same value. Keep the image/network/URL defaults from the example. Never paste a key into chat or commit `.env`.
3. Build the image and provision the proxy using that same file:

```sh
docker build -f deploy/Dockerfile.codex -t relay-codex:local .
docker compose --env-file .env -f deploy/compose.yaml up -d --build
python3 scripts/live_smoke.py --backend docker-api --env-file .env --check
```

`--check` reports prerequisites without printing secrets, invoking Codex or making an OpenAI request. Missing setup exits with status 2. Once it passes, explicitly run:

```sh
python3 scripts/live_smoke.py --backend docker-api --env-file .env --run-live --data ./live-proof
# To use the same configuration in the web app:
python3 -m relay.server --auth demo --env-file .env
```

A passing readiness check establishes local configuration/image/network availability, not successful API authorization, model access or a genuine completed coding task. The opt-in smoke remains the live proof.

## Provision

1. Install and start Docker on the machine hosting Relay. Supply an explicitly authorized organization-owned OpenAI API key and a random proxy capability through server-side environment variables or a secret manager. The proxy and Relay must receive the same values. See `.env.example`; do not put actual secrets in Git or shell arguments.
2. Set `RELAY_CODEX_MODEL` to a Codex model available to the organization's API project. Model availability and billing are controlled by that project; Relay does not claim a default entitlement.
3. Build the pinned execution image and start the isolated proxy:

```sh
docker build -f deploy/Dockerfile.codex -t relay-codex:local .
docker compose -f deploy/compose.yaml up -d --build
```

4. In the Relay server's environment, set the nonsecret switches:

```sh
export RELAY_ENABLE_ORG_API=1
export RELAY_CODEX_IMAGE=relay-codex:local
export RELAY_CODEX_NETWORK=relay-codex-internal
export RELAY_CODEX_PROXY_URL=http://relay-codex-proxy:8080/v1
export RELAY_CODEX_TIMEOUT_SECONDS=180
python3 -m relay.server --auth demo
```

The `.env.example` file is not auto-loaded by Relay. Docker Compose may read your local `.env`; use environment injection or an external secret manager for deployed setups. Never commit a populated `.env`.

5. As the organization administrator, enroll an **organization-owned / Organization API** worker, opt it in, and grant it a project. The API worker is eligible only when ownership, grants, availability, and runtime prerequisites pass. Select it explicitly to run a real task. The default two-worker demonstration stays simulated.
6. A task's persisted events distinguish provider responses and actual tool output from configuration status. Only a zero exit code **and** a documented `turn.completed` event count as successful execution. Inspect the worktree's diff/checkpoint before accepting changes.

## Boundaries

The coding container is non-root, read-only except for the selected worktree and bounded tmpfs directories, with all Linux capabilities dropped, no privilege escalation, one CPU, 768 MB memory, 128 processes, and an independent wall-clock watchdog. Host `.git` and Relay metadata are mounted read-only. No host Docker socket, home directory, personal CLI auth, or server data directory is mounted.

Docker's `internal` network is required and checked before scheduling. Coding containers join only that network. External DNS forwarding is disabled; the Docker-local proxy alias still resolves. The proxy is the only service attached to both the coding network and an egress network. It accepts only authenticated `POST /v1/responses` and `GET /v1/models`, forwards to fixed `https://api.openai.com`, disables provider storage, and does not publish a host port. It does not implement arbitrary URLs, CONNECT, redirects from client input, or general network forwarding. The organization key stays in the separate proxy; the coding process receives only the proxy capability. Docker operators are trusted and can inspect container environments.

The outer Docker container is the execution sandbox. Codex is explicitly given `danger-full-access` **inside that container**, with no interactive approvals, because nested OS sandbox support varies across Docker hosts. This never authorizes host execution. The wrapper refuses a host fallback. Install all needed test dependencies in a reviewed image; coding containers have no package-download network access.

Cancellation removes the task-labeled container and kills its CLI connection. A separate PID1 watchdog stops the command process group after the configured deadline, even if the host loses its Docker connection. If container removal cannot be confirmed, the adapter reports `safe_to_release=False`; Relay retains the writer lease for administrator inspection instead of starting another writer. Recovery identifies only Relay-managed containers with the persisted task ID. Restore Docker, verify/stop the affected container, then restart Relay to recover safely. No uncertain tool is automatically replayed.

The explicit demo identity mode remains loopback-only. For multiple human devices, use the implemented invite-account/session boundary and HTTPS configuration in MULTI_COMPUTER.md. This container configuration alone does not authenticate humans. Deployment still requires protected Docker control, secret handling, retention/backups and an external security review; established SSO/MFA remains future work. Quota is shared organization API quota; switching logical workers does not create capacity or reset usage limits.

## Live single-worker and A→B proof

Rebuild the execution image after updating Relay: it now contains `/opt/relay/result.schema.json`, used by the pinned CLI's `--output-schema` contract. Run the Relay process as a **non-root OS user** with Docker access; running the server as root is refused for live eligibility so a coding container cannot accidentally run as UID 0.

Configure secrets through your server environment/secret manager, and set `RELAY_CODEX_MODEL` to a model actually available to the organization's API project. Relay does not load personal CLI authentication. The official Codex request and stream retry counts are bounded by `RELAY_CODEX_REQUEST_RETRIES` (default 2) and `RELAY_CODEX_STREAM_RETRIES` (default 1). These retry provider transport requests; Relay never repeats an entire coding turn automatically after partial tool activity.

For one real worker, enroll an organization API worker as Jordan, approve the chosen project and opt in. As a project member, select that worker explicitly and submit any programming objective. The schema-backed final result, actual tool outputs, exit status, timing, native thread ID and changes are saved. If runtime health is unavailable, the task queues; it never silently switches to the validator simulation.

For a reproducible two-worker proof after provisioning:

```sh
python3 scripts/live_smoke.py --backend docker-api --run-live --data ./live-proof
# Optional unseen request. The text file is a task objective, not a shell command.
python3 scripts/live_smoke.py --backend docker-api --run-live --data ./live-proof-custom --prompt-file ./request.txt
```

The script uses the actual adapter/Docker/proxy/OpenAI route, requires unfinished phase-one work and actual phase-two test execution, then checks restart persistence. A complete passing report is stored at `live-proof/live-smoke-report.json`; task files stay in its server-side workspaces directory. A failure leaves evidence for inspection and never substitutes simulated output. Use a fresh/dedicated directory, not one held by the web server.

In the dashboard, enroll a second approved organization API worker, choose **Live demo: pause after the first phase for handoff**, then select B and Switch Worker at the saved boundary. You may also switch during a live run: Relay cancels/stops it before checkpointing and starting B. This can preserve incomplete edits, which B must inspect. Shared 429/billing exhaustion marks all workers on that API scope blocked; wait or resolve billing rather than switching to evade it. Forwarded provider headers preserve available retry/reset/request information without inventing quota values.

For a cloud-hosted trusted demo, run the same non-root server and persistent data directory on your Docker VM, then connect through an authenticated SSH tunnel (for example `ssh -L 8000:127.0.0.1:8000 your-host`). Workspaces and checkpoints remain on that host. The identity selector remains a local demo control; this is not production multi-user authentication. Do not bind/publish demo login directly to the Internet. No cloud deployment or paid smoke run was performed in the implementation environment; see [LIVE_CODEX_STATUS.md](../docs/LIVE_CODEX_STATUS.md).

For native macOS tool approval, offline project dependencies and environment recovery without Docker/API credentials, see [Native development environments](../docs/NATIVE_ENVIRONMENTS.md).
