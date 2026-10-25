# Native development tools and project dependencies

Relay's macOS native backend uses local Codex App Server processes and the existing ChatGPT-authenticated profiles. Docker, organization API keys and separate API billing are not prerequisites for this path. The worker sandbox continues to deny network access; provisioning tools and dependencies is a separate local-operator action.

## What failed in Real Codex Test

The saved B/C events show distinct environment failures:

- The Relay Python interpreter had no Django installation. Python could start, but importing Django failed.
- The host PATH did not contain a usable Node/npm installation. A sandboxed login shell reported `operation not permitted` while searching inaccessible PATH directories. A direct invocation of the known browser-test Node binary succeeded in a diagnostic sandbox command. The error was therefore not evidence that Node execution was categorically prohibited.
- Worker-side pip downloads failed because the sandbox denies network access. A package-resolution error after failed DNS/network requests does not establish package incompatibility.
- Fresh handoff worktrees contain committed source and Relay context, not ignored `.venv` or `node_modules` directories. Dependencies need to be prepared again for the successor's private worktree.

The new environment support addresses these causes without adding broad home-directory reads or opening worker network access. Earlier blocked tool outputs remain historical evidence; they are not rewritten as successful runs.

## Approve a local Node executable

From `/Users/Skibidi/Agent_pool`, choose a trusted installed Node executable supported by the pinned npm version. The command copies the selected binary into Relay's private tool store; it does not install a system-wide Node distribution.

```sh
cd /Users/Skibidi/Agent_pool
python3 scripts/native_environment.py approve-node --node /absolute/path/to/trusted/node
python3 scripts/native_environment.py check
```

For this investigation, the existing optional Playwright installation supplied the executable below. Use it only if that exact trusted installation is still present; `/private/tmp` is not a permanent tool installation location.

```sh
python3 scripts/native_environment.py approve-node --node /private/tmp/relay-browser-tools/playwright/driver/node
```

Provisioning downloads pinned npm 12.2.1 over certificate-verified HTTPS and checks its pinned SHA-512 integrity before extracting it. Framework Python installations without their optional CA bundle use the macOS system CA file; TLS certificate checks are never disabled. The approved executable and npm files are hashed. Modified tool files fail validation instead of silently becoming approved. The command refuses to replace an existing approved runtime; review any replacement while Relay is stopped.

`RELAY_NATIVE_ROOT`, if set in the server's environment, must have the same value in these operator commands. Its default is `.relay-native` relative to the repository. Tool/dependency stores stay private and are ignored by Git. Do not point this setting at a credential directory or copy profile files into a tool store.

## Prepare committed project dependencies

Review the project's committed `requirements.txt`, `package.json` and any `package-lock.json` first. Preparation uses committed Git content, so uncommitted manifest edits are not approved by this command. For the existing Real Codex Test project:

```sh
python3 scripts/native_environment.py prepare --workspace /Users/Skibidi/Agent_pool/local-native-data/workspaces/e6b48313cb954c8f987b0ca13e6c18c5
```

For another project, supply its own managed workspace path. This operation downloads Python wheels only, resolves npm dependencies and installs npm artifacts into an operator-owned bundle with installation scripts disabled. It does not run project tests or build scripts. Supported input is deliberately limited to registry package/version requirements; local paths, Git/URL dependencies, workspace installers, overrides and bundled dependency installers require separate review. npm lock artifacts must reference the official npm registry and include integrity values.

Preparation is a trusted-operator operation with network access. npm may read transitive dependency metadata while resolving a lock, before Relay validates the completed lockfile. This is not a registry-only network sandbox. Review and trust the manifests and dependency sources before running it. Application credentials and the operator's npm/pip configuration are not forwarded, and lifecycle installation scripts are disabled.

Bundles are identified by committed manifests, Python version and approved toolchain. A changed dependency manifest or toolchain needs a newly prepared bundle. Existing bundles are reused only after their artifact digest passes verification. Corrupted bundles fail closed; retain evidence and prepare a reviewed replacement rather than editing a manifest digest to accept unknown files.

## Continue an existing task

If Relay was already running before this source update, stop it gracefully and restart it with the same data directory, owner mode, port and environment. An already imported Python process cannot load the adapter change by itself. Wait for active work to reach a confirmed pause boundary before stopping it. Keep the existing data directory and native profile directories; do not bootstrap accounts again or repeat Codex login.

Then use the dashboard's existing owner-controlled resume/handoff action. Preparation alone does not resume a task or spend a Codex model turn.

Every new native turn still receives a separate Git clone. The adapter creates a private `.venv`, installs the approved wheels offline and copies approved `node_modules` into that clone. The generated lockfile can become part of the next normal source checkpoint. Mutable virtual environments and dependency trees are never shared between accounts. The successor rehydrates its own environment from the same approved bundle rather than copying another profile's runtime state.

Worker PATH is explicit: the private virtual environment, approved Node/npm, the Python runtime and standard system binary directories. HOME, shell startup configuration and temporary/cache paths are confined to the private worktree. Use `python`/`python3` from PATH to select the prepared virtual environment. Worker downloads remain disabled. Runtime directories `.venv`, `node_modules`, `.native` and `.tmp` are excluded from native patches and checkpoint scanning; tracked source colliding with those reserved paths is rejected.

Existing conversation IDs, events, task state, checkpoints, handoffs, writer leases, worker grants and the local-owner authentication boundary remain managed by Relay. This setup neither reads nor transfers Codex credentials. Each App Server continues to use only its own authenticated `CODEX_HOME`.

## Readiness, previews and evidence

`scripts/native_environment.py check` reports whether an approved Node/npm toolchain is configured. An absent or unfinished approval leaves Python-only tasks available. A modified approved runtime fails validation rather than silently using another PATH entry.

Each native turn emits a persisted `native_environment` event and writes `.native/environment.json` in its private worktree. `ready: false` means no matching approved dependency bundle exists; ask the local operator to prepare it. It is not successful dependency verification. A prepared bundle reports `ready: true` with its bundle identifier; actual imports, tests and builds can still fail and must be checked through real tool outputs.

An operator-only preview may use a private clone and explicitly allocated loopback ports for frontend/backend traffic. This must preserve filesystem isolation and restrict network access to those exact ports; the ordinary worker permission profile must retain its network denial. It does not authorize opening all localhost services or exposing Relay's server/authentication endpoints to project code. Follow the preview runner's own checks and report before claiming successful HTTP or browser verification.

`relay.native_preview.Preview` implements that separate macOS Seatbelt sandbox. It refuses occupied preview ports before executing project code, including an existing Relay listener. It terminates preview process groups on exit and allows restarting a stopped backend after HTTP connections enter `TIME_WAIT`. The smoke command allocates unused ports and changes the proxy target only in its disposable clone. It never opens or modifies the original project's Vite configuration.

Use previews in a fresh, isolated browser context, as the smoke command does. Cookies are scoped to a host rather than a port: an existing browser signed into Relay on `127.0.0.1` could otherwise send its Relay session cookie to a project server on another `127.0.0.1` port. Do not reuse a signed-in browser context for generated project code.

To repeat the actual runtime/browser check with Codex CLI, Google Chrome and Playwright already installed:

```sh
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/native_environment_smoke.py \
  --workspace /Users/Skibidi/Agent_pool/local-native-data/workspaces/e6b48313cb954c8f987b0ca13e6c18c5 \
  --browser
# If Playwright is installed in your active Python environment, omit PYTHONPATH.
# Omit --browser to run actual imports/tests/builds and isolation checks only.
```

This invokes the installed App Server's real command sandbox using a temporary unauthenticated home. It does not read native profile credentials, start a model turn, resume the saved task, or claim a new authenticated account handoff. It publishes a generated lockfile and checkpoint only in a disposable copy, then verifies independent dependencies in B and C private worktrees. Reports remain under ignored `native-environment-proof/`.

The offline regression suite is reproducible with:

```sh
python3 -m unittest tests.test_native_environment tests.test_native -q
```

These fixtures check PATH, reserved paths, artifact integrity, dependency isolation and continuation across fresh clones. They do not establish real Codex inference, Django HTTP success or a Vite/browser result. For Real Codex Test, require actual successful Django tests/endpoint output, a Vite build, HTTP ping, browser console evidence and a backend-stop/restart recovery check before reporting those checks as passed. Keep environment verification separate from earlier authenticated Codex handoff proofs.

## Verified runtime result, 2026-10-24

`native-environment-proof/0977fcc84c2e485e9412e5fd279984b0/report.json` records an actual passing run with Node 24.22.1, npm 12.2.1 and Django 5.2.18. Both fresh B and C environments passed Django's system check, its two endpoint tests, and the Vite production build. Actual sandbox probes denied outside/peer files and access to Relay's loopback service. The direct Django endpoint and Vite proxy returned `{"message":"pong","server":"django"}`. Chrome logged the ping, showed the expected error when Django stopped, and returned to success after restart, with no JavaScript page errors. The console also contained a nonblocking 404 for the sample's missing static resource and the expected HTTP 500/error log during the deliberate outage.

The original source checkpoint remained `87c05901d849149d1e545884eab20cc6ecd4b618`. This proves actual development runtime execution and dependency continuation; `model_execution` and `saved_conversation_resumed` are explicitly false. A new authenticated Codex turn on the saved task remains a separate owner action after reloading the updated server.

The idle local-owner server was then gracefully reloaded at `http://127.0.0.1:9000` with the same data directory. `native-environment-proof/server-reload-report.json` confirms unchanged conversations/messages/events, task state/generations, contexts, checkpoints, handoffs, assignments, execution runs, workers, grants, memberships, owner binding and session metadata. All three existing profiles passed actual App Server initialization, sandbox/runtime/thread probes and ChatGPT authentication; distinct-account validation passed without displaying identities. The saved web task remains at its existing `handoff_pending` boundary; no model turn was replayed automatically.

## App review tools for agents

A native agent's shell has no network, so it cannot start a server and open it. Each native task is therefore offered Relay tools (Codex dynamic tools, answered by Relay through `item/tool/call`):

| Tool | What it does |
| --- | --- |
| `run_app` | Starts a command from the agent's private worktree under a separate Seatbelt profile that may listen only on its assigned `127.0.0.1` port(s). `PORT`/`HOST` are set; a hard-coded port can be requested. |
| `http_request` | Sends an HTTP request to the app and returns status, headers and body. |
| `view_page` | Renders a page in headless Chrome over a private DevTools pipe and returns visible text, console messages, uncaught errors, failed requests and a screenshot. |
| `app_logs`, `stop_app` | Shows the app's output; stops its whole process group. |

The app sandbox reuses the preview policy: reads only system paths, the Python runtime, the approved toolchain and the worktree (never `.git`, `.relay` or `.env` files); network only on the assigned ports. Ports already in use, including Relay's, are refused before any project code runs. Seatbelt's port rule cannot distinguish loopback from LAN peers, so while an app runs Relay also holds the all-interfaces addresses (`0.0.0.0`/`::`) on its ports: a server bound to all interfaces fails with "address in use" (the tool explains how to bind `127.0.0.1`), and nothing is reachable through the machine's network address. Servers bound to `127.0.0.1` with address reuse (the default for Python's HTTP servers, Node, Django and Flask) work normally. Chrome uses a fresh temporary profile and a proxy that only the app's ports bypass, so page scripts cannot reach other local services, and reach the Internet only as the project's internet access allows (below). The app is stopped when the task ends. Codex's own network settings were not loosened: its permission profiles offer either no network or full network, with no loopback-only option. Tool calls appear in the task's activity as `tool_invocation`/`tool_output` events on the `app_review` channel. Requires macOS; `view_page` additionally needs Google Chrome (or `RELAY_REVIEW_BROWSER`).

## Internet access for agents (per project)

Agents' shells remain offline: Codex permission profiles offer either no network or full network, with no allowlist that it enforces. Instead Relay carries agents' internet traffic itself, and a project administrator chooses in **Projects → Internet** what it may reach:

- **Any public website or API** (the default, with no setup): every public host, so keyless public APIs just work. No API keys are added in this mode, because any host could receive them.
- **Only listed domains**: exact hosts such as `api.example.com`, or `*.example.com` for subdomains, plus optional API keys (below).
- **No internet.**

Relay enforces the choice itself:

- `run_app` apps and the new `run_command` tool get `HTTP(S)_PROXY` pointing at a per-session Relay proxy (`relay/internet.py`); their Seatbelt profile allows outbound connections only to that proxy port. `NODE_USE_ENV_PROXY` makes Node's `fetch` use it; `SSL_CERT_FILE` points at the macOS CA bundle.
- `view_page` Chrome uses the same proxy, so pages load what the project allows and nothing else.
- `fetch_url` lets the agent call an API directly from Relay.

In every mode the proxy accepts only ports 80 and 443, resolves each name itself, refuses any name or address that is private, loopback, link-local or carrier-grade NAT (so no name can point back at this machine or the LAN), and connects to the address it checked. Requests are summarised in `app_logs` and tool output. A changed setting applies to running agents at their next request. An agent's code can send what it reads in its workspace to any host the project allows; choose listed domains or no internet for projects whose code must not leave this machine.

API keys never travel through Relay's HTTP API or database, and are used only with listed domains. Put each value in Relay's environment file as `RELAY_SECRET_<NAME>=…` (start Relay with `--env-file`), then list `<NAME>` in the project's settings. Apps receive it as the environment variable `<NAME>`; `fetch_url` replaces `${NAME}` in the URL, headers or body on Relay's side. All `RELAY_SECRET_*` values are redacted from tool output, events and logs. Redaction cannot catch a value an app deliberately transforms, and a key could be sent to any of the project's listed domains, so list only domains you trust with the project's keys.
