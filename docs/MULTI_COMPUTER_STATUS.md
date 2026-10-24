# Multi-computer verification status

Updated 2026-10-24. **Shared human access is implemented and verified in real isolated Chrome sessions on one host, with explicitly simulated inference. Physical multi-host access, public HTTPS deployment and genuine shared organization API execution are NOT VERIFIED.** This is the remaining gap in the requested live multi-computer definition of done; the earlier local native success does not close that gap.

## Inspected and preserved baseline

The native proof was present before this milestone and remains present:

- `live-proof/native-smoke-report.json`: `live_verified: true`, three actual local native executions, two handoffs, seven generated tests, same conversation and restart persistence.
- SHA-256 after the shared milestone: `1d824e1ee8cda4a4d4eb715d8f90b5f42af4a1706402eb3d34cbdfbe69a9d199`.
- No native auth profile was read, copied, uploaded or tracked for this milestone. The real native test was not rerun or used as shared subscription authorization. Existing native automated tests remain in the regression suite.
- Pre-change full baseline: 108 tests in 115.461 seconds, 106 passed and the two opt-in genuine-provider tests skipped.

## Real browser fallback evidence

Command actually run using temporarily installed Playwright and the already-installed Chrome:

```sh
PYTHONPATH=/private/tmp/relay-browser-tools python3 scripts/multi_browser_smoke.py --data ./multi-proof/verified
```

Result: **PASS**, exit 0.

- Five isolated Chrome contexts: Organizer, three independently invited/member-authenticated people (Alex, Sam, Jordan), and an organization account without project access.
- Each member accepted an email-bound invitation and used its own Relay password/session. No seeded Alex/Sam/Jordan identity endpoint was used.
- Alex submitted the clearly labeled simulated validator task on A. Sam observed the same files/conversation, paused/checkpointed A and requested B. Jordan observed B's tests/context, paused/checkpointed B and requested C. C ran the five real trusted fixture tests.
- Three execution runs, two successful worker-to-worker handoffs plus their explicit pause records, durable commits and versioned contexts. No concurrent writer lease remained.
- All three member browsers reloaded and retained the same conversation/test history, then survived backend restart with their sessions, attribution, execution records and checkpoint IDs.
- The fourth account was denied direct project, source-download, event/history, event-stream, artifact and task-result URLs.

Proof: `multi-proof/verified/multi-browser-report.json` and `three-browser-proof.png`. Report SHA-256: `baa8dad971252d227d3f506e9162390061ee0499b8ae0966943cb535f0e59ec9`.

The report explicitly records `mode: simulated-inference`, `real_multi_host_verified: false` and `live_organization_api_verified: false`. This is real browser/account/HTTP/Git/test activity on one Mac, not real model inference or physical separate computers. The screenshot was visually inspected. Failed earlier attempts and their local logs/data were retained; the final script refuses to overwrite an existing proof directory.

## Automated evidence

An intermediate complete suite passed 124 tests in 185.653 seconds: 122 passed, two genuine-provider tests skipped. Fifteen shared-server HTTP scenarios subsequently passed again after tightening viewer worker-grant restrictions. The final full suite, including the additional three-phase organization-adapter test and tightened viewer grants, passed as recorded below.

Coverage includes:

- Real scrypt verification; invite email binding/expiry/reuse/revocation; one-time bootstrap; login/logout, absolute/idle expiration, logout-all and administrator session revocation; session persistence on restart.
- Host/origin/CSRF and login-spoofing defenses, rate limits, secure cookie attributes and HTTPS reverse-proxy enforcement. Header/cookie tests do not count as a deployed real TLS test.
- Admin/member/viewer project roles, direct-ID access denial, unauthorized downloads/artifacts/task results, unsupported WebSocket upgrades, and revocation during an already-open authenticated SSE subscription.
- Arbitrary natural-language instructions routed intact to the authorized organization adapter (mocked execution), including a three-phase handoff instruction for B; old local/native/simulation paths preserved.
- Leases, generation/idempotency checks, safe pause without reassignment, explicit resume, A/B/C attribution, checkpoints, HANDOFF/context and full visible-event persistence across restart.
- Provider/worker credential exclusion and rejection, no raw Relay password/session/invitation values in project responses or SQLite dumps. The authentication/session endpoints intentionally deliver their browser cookies/CSRF and admin-only invitation links; those values never enter conversation/history or exports.
- Trusted committed-repository import excluding source Git/auth state, and rejection of symlinks/submodules/oversized snapshots.

Other actual checks:

- `python3 scripts/demo.py`: PASS; original owned-worker simulation, five real tests and restart persistence.
- `python3 scripts/check_ui.py`: PASS; HTML IDs and JavaScriptCore syntax.
- `python3 -m compileall -q relay tests scripts deploy`: PASS.
- Ruff configured checks: PASS.
- Mypy on `relay/accounts.py`, `relay/auth.py`, `relay/server.py`, `relay/workers.py`, `relay/native.py`, `relay/app_server.py`: PASS.
- Working-tree and index whitespace checks: PASS.

## Live/deployment blockers and remaining priorities

Actual `python3 scripts/live_smoke.py --backend docker-api --check` exited 2. Organization enable/key, approved model, proxy capability, coding network and Docker CLI were unavailable in the process environment. No provider request was made. Personal native profiles were not used to work around that lack of shared authorization.

Caddy/systemd templates are supplied but were not launched; no public DNS/certificate/server or three physical machines were available. The loopback browser proof cannot verify cross-host TLS, firewall rules or real organization-model access. Use [MULTI_COMPUTER.md](MULTI_COMPUTER.md) for exact setup and the three-device live checklist. Priorities are authorized organization provisioning, genuine three-machine/TLS proof, security/identity operations review (OIDC/MFA/recovery/retention), and only then a provider-authorized remote worker protocol.

One concurrent Git commit captured early **throwaway test databases** before their ignore rule was in place. They contain test account hashes/session digests, not copied Codex credentials or plaintext test passwords. These generated paths were removed from the current index with `git rm --cached` and are now ignored; every local proof file was retained. Existing Git history was not rewritten, so that earlier commit still contains the test data. Native profile and real secret directories are not tracked. Do not publish authentication databases as application source.

The code is a hardened invite-only hackathon server for one trusted organization, not an independently audited identity platform. No repository LICENSE file was found; deploying this code or redistributing provider components still requires the appropriate rights/terms. No shared consumer-subscription pooling or remote-worker authorization is claimed.

## Final regression run

Actual command: `python3 -m unittest discover -s tests -q`.

**PASS: 125 tests in 165.892 seconds; 123 passed, two genuine-provider tests skipped.** This includes all original local/native tests, fifteen shared-server HTTP tests, the trusted repository import test, and the new three-phase organization transport fixture test. No genuine model calls are made by this ordinary suite.

Final source compilation, configured Ruff checks, six-file Mypy check and HTML/JavaScriptCore checks passed. Working-tree and staged-index whitespace checks passed. These are independent of the successful real-Chrome simulated-inference proof and the preserved earlier real native proof.
