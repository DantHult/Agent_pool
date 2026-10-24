# Optional organization API sandbox

The credential-free simulation needs none of this setup. This optional runner uses the documented official `codex exec --json` interface, pinned to the installed CLI version checked during implementation. It has **not been live-tested here**: Docker and an authorized organization API credential were unavailable. Adapter protocol, streaming completion checks, cancellation, redaction, network guards, and proxy forwarding are covered by deterministic tests.

This setup is for one trusted organization per proxy stack. The server's organization credential reference is fixed to `env:RELAY_ORG_OPENAI_API_KEY`. Do not use local `~/.codex` state, employee subscription credentials, browser cookies, or personal tokens.

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
python3 -m relay.server
```

The `.env.example` file is not auto-loaded by Relay. Docker Compose may read your local `.env`; use environment injection or an external secret manager for deployed setups. Never commit a populated `.env`.

5. As the organization administrator, enroll an **organization-owned / Organization API** worker, opt it in, and grant it a project. The API worker is eligible only when ownership, grants, availability, and runtime prerequisites pass. Select it explicitly to run a real task. The default two-worker demonstration stays simulated.
6. A task's persisted events distinguish provider responses and actual tool output from configuration status. Only a zero exit code **and** a documented `turn.completed` event count as successful execution. Inspect the worktree's diff/checkpoint before accepting changes.

## Boundaries

The coding container is non-root, read-only except for the selected worktree and bounded tmpfs directories, with all Linux capabilities dropped, no privilege escalation, one CPU, 768 MB memory, 128 processes, and an independent wall-clock watchdog. Host `.git` and Relay metadata are mounted read-only. No host Docker socket, home directory, personal CLI auth, or server data directory is mounted.

Docker's `internal` network is required and checked before scheduling. Coding containers join only that network. External DNS forwarding is disabled; the Docker-local proxy alias still resolves. The proxy is the only service attached to both the coding network and an egress network. It accepts only authenticated `POST /v1/responses` and `GET /v1/models`, forwards to fixed `https://api.openai.com`, disables provider storage, and does not publish a host port. It does not implement arbitrary URLs, CONNECT, redirects from client input, or general network forwarding. The organization key stays in the separate proxy; the coding process receives only the proxy capability. Docker operators are trusted and can inspect container environments.

The outer Docker container is the execution sandbox. Codex is explicitly given `danger-full-access` **inside that container**, with no interactive approvals, because nested OS sandbox support varies across Docker hosts. This never authorizes host execution. The wrapper refuses a host fallback. Install all needed test dependencies in a reviewed image; coding containers have no package-download network access.

Cancellation removes the task-labeled container and kills its CLI connection. A separate PID1 watchdog stops the command process group after the configured deadline, even if the host loses its Docker connection. If container removal cannot be confirmed, the adapter reports `safe_to_release=False`; Relay retains the writer lease for administrator inspection instead of starting another writer. Recovery identifies only Relay-managed containers with the persisted task ID. Restore Docker, verify/stop the affected container, then restart Relay to recover safely. No uncertain tool is automatically replayed.

The sample application remains loopback-only with local demo login. This sandbox configuration does not make Relay's identity system production-ready. Before remote hosting, implement real SSO/session authentication, TLS, tenant-scoped proxy secrets, protected Docker control access, retention limits, and volume backups. Quota is shared organization API quota; switching logical workers does not create capacity or reset usage limits.

## Live single-worker and A→B proof

Rebuild the execution image after updating Relay: it now contains `/opt/relay/result.schema.json`, used by the pinned CLI's `--output-schema` contract. Run the Relay process as a **non-root OS user** with Docker access; running the server as root is refused for live eligibility so a coding container cannot accidentally run as UID 0.

Configure secrets through your server environment/secret manager, and set `RELAY_CODEX_MODEL` to a model actually available to the organization's API project. Relay does not load personal CLI authentication. The official Codex request and stream retry counts are bounded by `RELAY_CODEX_REQUEST_RETRIES` (default 2) and `RELAY_CODEX_STREAM_RETRIES` (default 1). These retry provider transport requests; Relay never repeats an entire coding turn automatically after partial tool activity.

For one real worker, enroll an organization API worker as Jordan, approve the chosen project and opt in. As a project member, select that worker explicitly and submit any programming objective. The schema-backed final result, actual tool outputs, exit status, timing, native thread ID and changes are saved. If runtime health is unavailable, the task queues; it never silently switches to the validator simulation.

For a reproducible two-worker proof after provisioning:

```sh
python3 scripts/live_smoke.py --run-live --data ./live-proof
# Optional unseen request. The text file is a task objective, not a shell command.
python3 scripts/live_smoke.py --run-live --data ./live-proof-custom --prompt-file ./request.txt
```

The script uses the actual adapter/Docker/proxy/OpenAI route, requires unfinished phase-one work and actual phase-two test execution, then checks restart persistence. A complete passing report is stored at `live-proof/live-smoke-report.json`; task files stay in its server-side workspaces directory. A failure leaves evidence for inspection and never substitutes simulated output. Use a fresh/dedicated directory, not one held by the web server.

In the dashboard, enroll a second approved organization API worker, choose **Live demo: pause after the first phase for handoff**, then select B and Switch Worker at the saved boundary. You may also switch during a live run: Relay cancels/stops it before checkpointing and starting B. This can preserve incomplete edits, which B must inspect. Shared 429/billing exhaustion marks all workers on that API scope blocked; wait or resolve billing rather than switching to evade it. Forwarded provider headers preserve available retry/reset/request information without inventing quota values.

For a cloud-hosted trusted demo, run the same non-root server and persistent data directory on your Docker VM, then connect through an authenticated SSH tunnel (for example `ssh -L 8000:127.0.0.1:8000 your-host`). Workspaces and checkpoints remain on that host. The identity selector remains a local demo control; this is not production multi-user authentication. Do not bind/publish demo login directly to the Internet. No cloud deployment or paid smoke run was performed in the implementation environment; see [LIVE_CODEX_STATUS.md](../docs/LIVE_CODEX_STATUS.md).
