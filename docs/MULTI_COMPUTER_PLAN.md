# Multi-computer milestone plan

Inspected 2026-10-24 before implementation. Preserve the existing Python/SQLite/Git application, migrations, native profiles and all prior proof. `live-proof/native-smoke-report.json` is present and records three completed native runs, two handoffs, seven passing generated tests and restart persistence. That proof is **one machine using the local owner's three profiles**, not three humans or a shared-server subscription authorization.

## Architecture and compatibility

Keep one trusted organization per Relay deployment and its server-managed Git workspaces. Add invite-only human accounts with scrypt password hashes, hashed opaque revocable sessions, expiration/idle limits, exact-origin checks, CSRF tokens and persistent rate limits. No OIDC provider exists in the repository; do not invent an unverified OAuth integration. The web UI collects only a new Relay account password, never model credentials. Public deployment requires HTTPS through a loopback reverse proxy. CLI defaults to invite authentication; the old loopback-only demo is explicit and its legacy programmatic test helper stays compatible.

Project admin/member/viewer roles and explicit worker/project grants remain distinct. All reads, artifact/download routes and persisted event subscriptions check project access on the server; all writes also check role. Shared live execution uses the existing organization API sandbox/proxy. Native consumer profiles stay local and are disabled in shared-server mode. Do not copy their files, implement cross-person subscription pooling, or claim remote-worker authorization. Remote worker enrollment/snapshot transport is deferred; no remote worker endpoint will accept credentials.

Use authenticated Server-Sent Events plus the existing polling/history API, rather than adding a WebSocket dependency. Recheck membership and session validity while streaming and restore state by stable event/conversation IDs. Preserve scheduler writer leases, generation checks, checkpoint/context records and idempotency. Add genuine pause-without-reassignment and explicit resume operations. The dependable fallback is clearly labeled three-worker simulated inference with real fixture files/tests.

## Implementation phases

1. Additive auth/invitation/rate-limit migration, no seeded production humans; bootstrap CLI and role checks. Keep old demo/data/tests.
2. Secure HTTP auth/session/CSRF boundary, invitations/member controls, authenticated streaming, protected source/patch downloads and minimal health endpoint. Block shared native execution and raw filesystem/log/credential URLs.
3. Extend existing dashboard with login/accept-invite/logout, invitations and membership controls, role-aware actions, pause/resume and worker observation timestamps; keep the current layout.
4. Add HTTPS reverse proxy and non-root service examples, persistent data configuration, safe loopback/LAN instructions and a three-device checklist.
5. Automated security/auth/HTTP/scheduler/restart tests; real three separate browser profiles on this host if available. Preserve the native live proof. A simulated multi-browser run and mocked live adapter tests are separate from genuine model/multi-host evidence.

## Blockers and honest acceptance

This environment has one macOS host, the three local native profiles and Chrome. It has no supplied organization API key/Docker host, three physical machines, public hostname, TLS certificate or authorized remote-worker provider flow. Implement all non-live functionality without waiting for these. Mark real **multi-host organization API** execution and public deployment **NOT VERIFIED** unless those actual prerequisites become available. The prior native proof cannot stand in for that verification. Require explicit operator provisioning for the preserved live organization backend; never substitute simulation silently.

Security references reviewed: [OWASP password storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html), [OWASP sessions](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html), and [Caddy HTTPS reverse proxy](https://caddyserver.com/docs/quick-starts/reverse-proxy). These guide implementation; they are not a security audit or a claim of production certification.
