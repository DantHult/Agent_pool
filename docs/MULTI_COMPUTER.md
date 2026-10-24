# Shared Relay: setup, three-device demo and verification

Updated 2026-10-24. One trusted organization per server is supported. Three real, independently signed-in browser sessions on this Mac passed the shared project/handoff/restart test. Their inference was **SIMULATED**, with real Git files and five real Python tests. Physical multi-host access, public HTTPS deployment, and live organization API execution are **NOT VERIFIED** here. The earlier genuine native A → B → C proof is preserved and remains a separate local-owner test.

## What functions now

| Capability | Implemented / observed behavior |
| --- | --- |
| Human accounts | Invite-only email-bound Relay accounts; no seeded production users. scrypt password hashes, opaque hashed sessions, absolute/idle expiration, logout, logout-all, administrator session revocation, persistent rate limits, same-origin/CSRF checks |
| Organization/project roles | Organization admin/member/viewer; project admin/member/viewer; project reads/writes enforced independently of worker/project grants and owner opt-in. Organization administrators can manage/read their organization's projects |
| Multiple human devices | Shared URL, login/accept invitation, project selection, attributed persisted events, authorized downloads/diffs/history, pause/checkpoint, resume and explicit worker handoffs. Five isolated real Chrome contexts passed on one host |
| Live shared workers | Existing organization-authorized Docker/Codex API adapter; freeform objectives, explicit grants and shared provider quota. Mocked HTTP routing tested; actual Docker/API run blocked by missing prerequisites |
| Local native subscriptions | Prior real three-profile native proof retained. Disabled in shared invite-only mode, including worker enrollment and eligibility; no cross-person allowance pooling |
| Remote worker hosts | Deferred. No worker enrollment credential, snapshot submission or remote execution endpoint is exposed. Human accounts are not execution credentials |
| Live subscriptions | Authenticated SSE with project/session revalidation and event-ID replay, plus polling fallback. WebSocket upgrade is rejected; no unauthenticated WebSocket channel exists |

There is network transfer between browsers and the server. Files stay in the server's managed Git workspace; users do not manually move repository files between computers. Raw filesystems, Git metadata, database exports, logs, native profiles and arbitrary paths are not HTTP routes. Selected source downloads use the same authorized, bounded, redacted view as the dashboard.

## Local invite-only setup

Requirements: Python 3.10+ with scrypt support, Git, and a non-root OS user. No application package install is needed. Keep a separate persistent directory from a demo/native proof.

```sh
python3 scripts/manage_accounts.py bootstrap --data ./shared-data --email organizer@example.com --name Organizer
# Enter a NEW Relay-only password interactively, at least 12 characters.
python3 -m relay.server --auth invite --data ./shared-data --public-url http://127.0.0.1:8000 --allow-http-local
```

Open http://127.0.0.1:8000 and sign in as that administrator. The HTTP exception is confined to `localhost`/`127.0.0.1`; it cannot be used for a plaintext LAN/public deployment. The application always binds to loopback. The CLI defaults to invite authentication and refuses to start without a valid public origin. Existing programmatic `make_server()` callers retain their legacy loopback demo default for compatibility; new shared callers must select `auth_mode='invite'`.

Create a project, then **Invite a teammate** with their email and project role. Privately give the one-day link to the intended recipient. Its bearer credential is in a URL fragment, is removed from the address bar by the client, and is stored only as a digest on the server. Relay does not send email. New users choose their own Relay passwords; an existing user accepts another invitation with authenticated `POST /api/invitations/accept` and their email/token. Existing-account invitations grant the project without escalating organization roles. The UI never asks for ChatGPT passwords, device codes, cookies or auth files.

The administrator can use **Project access** to change/remove an existing member's project role. Project viewers can read but cannot submit, pause, cancel, resume, hand off or alter workers, including with replayed request IDs. Removing access closes an existing event subscription on its next authorization check. Session revocation is `POST /api/auth/revoke-sessions` with `user_id`; a user can revoke all their own sessions with `POST /api/auth/logout-all`. These operations require the session CSRF header. To revoke sessions offline, stop the server and use:

```sh
python3 scripts/manage_accounts.py revoke-sessions --data ./shared-data --email person@example.com
```

There is no email password-reset, MFA or OIDC flow in this milestone. Bootstrap is disabled after the first real account exists. Legacy demo accounts lack password records and cannot log into invite mode; old projects/history remain intact. Back up the persistent directory and secure it like source code. Invite mode enforces directory mode 0700 and SQLite modes 0600. Use a restrictive service umask for all new artifacts.

## Import an authorized repository

Stop Relay before using offline tools. A trusted operator can import committed regular files from a repository already present on the server; no repository URL, SSH secret or arbitrary local path is accepted by the browser:

```sh
python3 scripts/import_project.py --data ./shared-data --email organizer@example.com --repo /path/to/reviewed/repository --name 'Authorized repository'
```

This creates a new project and Git checkpoint without copying source Git configuration, hooks, native profiles, common auth directories or `.env` files. Symlinks/submodules and excessive snapshots are refused. It imports committed files only, not local uncommitted edits. Review the source for secrets and licensing before importing; filename filtering cannot identify arbitrary embedded secrets. Native/provider credentials must never be committed in the source repository. The sample project can also be used for a task that creates new files.

## HTTPS deployment

Use [deploy/shared.env.example](../deploy/shared.env.example), [deploy/Caddyfile.shared](../deploy/Caddyfile.shared) and [deploy/relay.service](../deploy/relay.service). Install Python/Git, Caddy and the repository at `/opt/relay` on a trusted server. Create a dedicated non-root `relay` user. Operator commands below assume that user and the checkout already exist:

```sh
sudo install -d -m 700 -o relay -g relay /var/lib/relay
sudo install -d -m 750 /etc/relay
sudo install -m 600 -o relay -g relay deploy/shared.env.example /etc/relay/shared.env
# Edit /etc/relay/shared.env locally: real HTTPS origin and persistent data path.
sudo -u relay /usr/bin/python3 /opt/relay/scripts/manage_accounts.py bootstrap --data /var/lib/relay --email organizer@example.com --name Organizer
sudo install -m 644 deploy/relay.service /etc/systemd/system/relay.service
sudo systemctl daemon-reload
sudo systemctl enable --now relay
# In Caddy's persistent service environment set RELAY_DOMAIN=relay.example.com.
# For a foreground validation/run (the hostname is nonsecret):
RELAY_DOMAIN=relay.example.com caddy validate --config deploy/Caddyfile.shared --adapter caddyfile
RELAY_DOMAIN=relay.example.com caddy run --config deploy/Caddyfile.shared --adapter caddyfile
curl --fail https://relay.example.com/healthz
```

Point DNS to the host and allow HTTPS/ACME traffic only as required by Caddy. Keep port 8000 private; Caddy is the only public ingress. The reverse proxy forwards the original allowed host and overwrites `X-Forwarded-Proto`; Relay requires `https` for the public origin and rejects other hosts/origins. Secure cookies are host-only `__Host-relay_session`, HttpOnly, SameSite=Strict, Path=/, with no Domain attribute. Authenticated mutations also require a per-session CSRF token. Tokens are kept in browser memory/cookies, not localStorage. Logout/revocation invalidate server-side digests. Caddy's example has no file-server or access-log directive and never serves native credential directories.

The public certificate/domain and service commands are deployment instructions, **not evidence that public deployment was performed**. Templates were reviewed; Caddy/systemd were not launched here. Do not expose the stdlib HTTP listener directly. Rate limits, bounded request threads/hash work, request-size/read deadlines and stream slots limit basic abuse; upstream denial-of-service protection and a security review remain operator responsibilities.

## Safe LAN testing

For three physical laptops on a trusted LAN, map a hostname such as `relay.local` to the server's LAN address on each device. Run the invite backend with `RELAY_PUBLIC_URL=https://relay.local` and proxy with [Caddyfile.lan](../deploy/Caddyfile.lan):

```sh
python3 -m relay.server --auth invite --data ./shared-data --public-url https://relay.local
RELAY_LAN_HOST=relay.local caddy run --config deploy/Caddyfile.lan --adapter caddyfile
```

Trust Caddy's public local root certificate on each client using the OS/browser trust interface, as described by [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https). Distribute only its public root certificate, never its private CA key. Validate the hostname/certificate and health URL before signing in. No teammate needs SSH access to another person's laptop. This LAN/TLS setup was not executed on three physical machines here; do not bypass certificate verification or use plaintext public/LAN passwords.

## Supported live execution

Provision the existing [Docker/API backend](../deploy/README.md) with organization authorization, an approved image/model, private proxy capability and internal coding network. Keep secrets in the service's private environment/secret store; never enter them into Relay's worker UI. Grant the non-root service user Docker access only on a trusted host: Docker control is privileged. The provided systemd restrictions do not make Docker control unprivileged.

Set the authorized organization environment and verify:

```sh
python3 scripts/live_smoke.py --backend docker-api --env-file /etc/relay/shared.env --check
```

In the shared UI, the organization administrator enrolls **Organization API** workers A/B/C and explicitly approves the project. Members select one of those workers and enter arbitrary coding instructions. No missing runtime or capacity condition silently switches to simulation or a native personal profile. Provider quota totals/reset times remain unknown unless a real provider signal supplies them. A new logical worker does not reset shared API limits.

No organization API credential, Docker host or approved model was available here. Preflight exited 2 and made no provider request. Native profiles were not copied, uploaded, re-used for teammates or converted into server credentials. Remote-worker protocols are deferred rather than asserting an unverified provider authorization. Repository/CLI/provider licensing and applicable service terms still apply; no repository LICENSE file was found and this milestone grants no additional redistribution rights.

## Three-device demonstration

1. Organizer signs in, creates/imports one project, provisions three supported Organization API workers, opts them in, and grants the project. Invite Alex, Sam and Jordan as organization members with project-member access; create a fourth organization account without that project grant.
2. On laptop 1, Alex accepts the invitation and chooses worker A. Enter a substantive natural-language objective with meaningful work across three phases; select **Three phases** and **Pause after a useful phase**. Require actual live file changes and authentic tool events.
3. On laptop 2, Sam signs in and selects the same project. Confirm the same conversation ID, persisted events and changed files without manually transferring a repository. Inspect the full history/diff.
4. Use **Pause / Checkpoint** during a run, or wait for its safe phase boundary. Confirm the stopped writer, saved commit and context vN/HANDOFF.md. Select B and **Request Handoff**. Its new native thread receives objective, decisions, completed/pending work, tests, blockers, current Git state and visible historical references. No hidden reasoning/native session is transferred.
5. On laptop 3, Jordan confirms the same history, checkpoints and B's changes. Pause/checkpoint as necessary and explicitly hand off to C. C must finish the remaining work and run real tests. Review actual tool exits and errors, not just agent claims.
6. Refresh all three browsers, restart Relay using the same data directory, and confirm account sessions (unless expired/revoked), conversation ID, attribution, checkpoints, context, events and test logs survive. An uncertain interrupted execution becomes pending for explicit review; it is never blindly replayed.
7. Sign in as the fourth account. Direct requests to the project, event stream/history, artifact, task-result and source-download URLs must be denied. Test viewer roles separately for read-only access. Session or project revocation must stop an already-open stream.

A handoff is both requested and authorized by the named Relay actor under the pre-existing worker owner/project grant. Assignment/generation/leases and append-only events/audit records retain these decisions. A pause saves state without automatically assigning an alternate. Resume returns the task to its current eligible worker; Request Handoff selects a different eligible worker. Owner offline/opt-out and provider exhaustion can leave work pending. The server never resumes or rotates an unauthorized subscription.

## Reliable fallback and repeatable proof

No model calls or provider credentials are needed for the fallback. As Organizer, enroll three **organization-owned Simulation** workers A/B/C and grant the project. Alex submits the password-validator fixture prompt with **Three phases** and pause enabled. Sam pauses/checkpoints A, selects B and requests handoff; B writes trusted tests and remains at a safe boundary. Jordan pauses/checkpoints B and requests C; C executes the five real fixture tests. All inference/capacity is visibly simulated. This fallback demonstrates multi-user transport, real files/checkpoints/tests and authorization, not real model reasoning.

For the original single-user local demo:

```sh
python3 -m relay.server --auth demo --data ./demo-session --port 8001
python3 scripts/demo.py
```

For automatic real-browser fallback proof, install optional Playwright outside the production environment and use installed Chrome:

```sh
python3 -m pip install --target /private/tmp/relay-browser-tools playwright
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/multi_browser_smoke.py --data ./multi-proof/new-run
# Linux/other Chrome locations: add --chrome /path/to/chrome-or-chromium
```

Use a fresh proof subdirectory; the script refuses to overwrite prior proof. **Observed pass:** `multi-proof/verified/multi-browser-report.json` plus `three-browser-proof.png`, five isolated Chrome contexts on this host, three authorized humans, fourth-account denial, A/B/C inference explicitly simulated, five passing fixture tests, page reload and backend restart persistence. This is not a physical multi-host or live organization API result. The genuine local native proof remains `live-proof/native-smoke-report.json` with its three real model turns and seven tests.

Automated tests cover login/logout/expiry/invites/rates/CSRF/TLS cookie/proxy guards, role and IDOR denial (including streams/downloads/artifacts), active-stream revocation, multi-session reconnect/restart, safe pause/resume/handoffs, credential exclusion, arbitrary objective routing through a **mocked** organization adapter, and repository import safeguards. No automatic test in ordinary CI consumes a personal subscription or organization billing.

## Changes and remaining work

Exact milestone files: `relay/accounts.py`, `relay/migrations.py`, `relay/server.py`, `relay/store.py`, `relay/workers.py`, `relay/config.py`; `static/index.html`, `static/app.js`, `static/style.css`; `scripts/manage_accounts.py`, `scripts/import_project.py`, `scripts/multi_browser_smoke.py`; `tests/test_shared_server.py`, `tests/test_repository_import.py`, `tests/test_live_codex.py`; `.env.example`, `.gitignore`; `deploy/shared.env.example`, `deploy/Caddyfile.shared`, `deploy/Caddyfile.lan`, `deploy/relay.service`, `deploy/README.md`; `README.md`, `docs/MULTI_COMPUTER_PLAN.md`, `docs/MULTI_COMPUTER.md`, `docs/MULTI_COMPUTER_STATUS.md`, `docs/AUTH_EXECUTION_COMPATIBILITY.md`, `docs/IMPLEMENTATION_STATUS.md`. Existing native adapter/proof files are preserved, not rebuilt.

Remaining priorities: (1) provision an authorized organization API backend and run the live checklist; (2) execute the checklist over real TLS on three physical hosts and retain independent proof; (3) obtain deployment security review, add established OIDC/MFA/account recovery and operational backups/retention; (4) only then consider a provider-authorized remote-worker protocol with short-lived revocable worker credentials and audited snapshots/patches. No real multi-host/live-shared success is claimed by this milestone.
