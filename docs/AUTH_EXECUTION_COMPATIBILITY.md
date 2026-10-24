# Authentication and execution compatibility

Reviewed on **2026-10-24** against the official OpenAI documentation linked below and local `codex-cli 0.163.0` help. This document separates Relay product authorization from provider execution authorization. No personal authentication state was inspected or imported.

| Mode | Provider support / Relay behavior | Funding and quota |
| --- | --- | --- |
| `simulation` | Implemented, runnable without credentials; deterministic predefined code tools and real test subprocesses | Independently owned, independently simulated capacity. No provider inference or personal allowance |
| `organization_api` | Official Codex noninteractive API-key execution is supported. Relay implements a Docker-only adapter and restricted inference proxy; requires explicit operator configuration, organization ownership, grants, opt-in, and API authorization | Organization API funding; logically separate workers share the organization's quota scope. No invented remaining-token counter |
| `personal_supported` | **UNSUPPORTED / disabled** for Relay's remotely hosted cross-user delegated use case. Health reports `unsupported_execution_mode`; execution fails closed | No employee allowance is imported, pooled, transferred, or consumed |
| Relay local demo login | Implemented only to demonstrate user/project/owner permissions. Seed identity selection is not proof of identity | Grants access to Relay demo records only; it does not authorize any OpenAI subscription |
| Production SSO/OIDC | Future integration through Relay's identity boundary | Independent of execution credentials and worker eligibility |

## What the official documentation establishes

The [Codex SDK documentation](https://developers.openai.com/codex/sdk/) supports embedding Codex in applications; the SDK can manage coding threads. The local environment had no installed `openai_codex` package or Node runtime. The implemented adapter therefore uses the documented CLI integration rather than guessing SDK methods.

The [noninteractive Codex documentation](https://developers.openai.com/codex/noninteractive/) documents stdin prompts, JSONL events, explicit API-key automation, and completed/failed turns. It warns against exposing API credentials to repository-controlled code. Relay keeps the organization credential in a separate narrow proxy; generated code receives a scoped inference capability, not the API key. Only visible agent messages and tool events are persisted; reasoning items are discarded.

The [Codex configuration reference](https://developers.openai.com/codex/config-reference/) documents a custom model provider's `base_url`, `wire_api`, `env_key`, and authentication settings. Relay uses these to route Codex to the internal inference proxy with an empty ephemeral `CODEX_HOME`; it never reads an employee's saved CLI authentication. The [Codex authentication documentation](https://developers.openai.com/codex/auth/) distinguishes API and ChatGPT-managed authorization. These are provider mechanisms, not automatic grants for teammates to consume one another's subscriptions.

The [Sign in with ChatGPT overview](https://developers.openai.com/siwc/token-sharing-open-source) describes identity and optional plan usage for eligible open-source/local clients, binding client registrations to a user/workspace, with host identities for that same user/workspace. It directs remotely hosted/paid apps to a separate interest process. We found no explicit documented permission there for Relay's intended cross-user delegated execution across employees' personal allowances. That is a compatibility assessment of the documented use case, not a blanket claim that all ChatGPT integrations are unavailable. Until explicit provider support for this exact delegation is established, enrollment can display the mode and reason but cannot enable it.

The [API rate limit documentation](https://developers.openai.com/api/docs/guides/rate-limits) explains provider limits. Relay records only actual provider usage/error signals it receives, manual availability, or labeled simulation. A separate worker identity cannot reset a shared organization/API-project limit. `reset_at` remains unknown unless a reliable provider signal supplies it; switching does not promise a reset.

## Credential and permission boundaries

A Relay user is a human account. A worker owner is the user or organization authorized to enroll/configure it. A Codex worker is an execution identity/session. A credential reference points to a server-side secret provider. Project membership alone never permits scheduling another person's worker: explicit worker/project grants and owner opt-in are also required.

Organization API enrollment is restricted to administrators and a server-defined credential reference. Secrets are loaded through environment variables; ordinary SQLite worker rows store references only. Public worker directory responses exclude those references; HTTP owner settings also omit the reference; it is only available to authorized backend code. No endpoint accepts personal tokens, cookies, copied CLI auth directories, or a raw API key. Never put credentials in prompts, transcripts, context summaries, artifacts, or Git commits.

For this single-organization MVP, the proxy and server use `RELAY_ORG_OPENAI_API_KEY` and a matching `RELAY_CODEX_PROXY_TOKEN`; neither is collected by the browser. See [sandbox setup](../deploy/README.md). Local demo identity selection remains intentionally permissive and must be replaced before remote deployment.

## Verified versus unavailable here

The deterministic workers, real file/test activity, disabled personal adapter, Docker command confinement, missing-prerequisite checks, cancellation, completion detection, secret redaction, and narrow proxy behavior are covered by tests. **No paid provider request or Docker image build was run** because this environment supplied neither Docker nor an authorized organization API credential. API readiness is configuration/health information, not a claim that a live task succeeded. A real integration run must be performed in the configured sandbox before a live demo is described as verified.

The live continuation implementation and opt-in integration proof are documented in [LIVE_CODEX_STATUS.md](LIVE_CODEX_STATUS.md). Schema 3 stores execution outcomes without changing credential ownership or the unsupported personal-subscription boundary.
