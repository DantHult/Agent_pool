# Authentication and execution compatibility

Reviewed **2026-10-24** using official OpenAI documentation and installed `codex-cli 0.163.0`, including its generated experimental App Server JSON schema. Relay authorization and Codex authentication are separate checks.

| Mode | Execution authorization | Funding and quota |
| --- | --- | --- |
| `native` | Implemented local macOS App Server client. Each of A/B/C has an isolated `CODEX_HOME` authenticated interactively by its owner. Only the configured local Relay owner can initiate tasks or explicitly continue on another profile. The backend rejects API-key auth. | The authenticated ChatGPT Codex subscriptions; no Docker, organization API credential or separate API billing. Capacity is unknown until the provider reports it. No automatic quota-driven account rotation. |
| `organization_api` | Preserved optional Docker adapter using official `codex exec --json`, organization ownership, project grants, opt-in and explicit server configuration. | Organization API funding; logical workers share organization quota. |
| `simulation` | Credential-free deterministic coding fixture with real Git files and tests. | Clearly labeled independent simulated capacity; no provider inference. |
| `personal_supported` | Disabled legacy mode for hosted cross-user delegation. Local native owner-initiated execution uses the separate `native` mode. | No import/pooling of other users' allowances. |
| Relay local demo login | Local identity selection for testing project permissions; not production identity verification. | Does not authenticate any OpenAI account. |

## Official protocol and authentication

The [official App Server documentation](https://learn.chatgpt.com/docs/app-server) describes stdio initialization (`initialize`, `initialized`), threads (`thread/start`), turns (`turn/start`), streamed item notifications and `turn/interrupt`. Relay implements these interfaces directly rather than inventing SDK methods. Fresh threads receive the objective, committed project state, visible history, tests and pending steps from Relay's handoff infrastructure. Native thread identifiers remain separate from the persistent Relay conversation.

The [Codex authentication documentation](https://learn.chatgpt.com/docs/auth) distinguishes ChatGPT login and API-key authentication. Native mode checks `account/read`, requires `type: chatgpt`, and leaves login/refresh to the official CLI. Each dedicated profile uses the file credential store for isolation. No authentication files are inspected or copied by Relay. Account identity is only hashed in memory for distinct-account checks and is never printed or persisted.

OpenAI's [Sign in with ChatGPT terms](https://openai.com/policies/sign-in-with-chatgpt-terms/) require activity to be for the authenticated user and disallow another user's activity from triggering consumption of that user's account. Relay therefore keeps native subscriptions local to their owner; project grants or administrator privileges do not override this restriction. The legacy remotely hosted cross-user subscription mode remains disabled. This is separate from legitimate owner-initiated local use.

The [Codex permissions documentation](https://learn.chatgpt.com/docs/permissions) and installed CLI schema inform the native permission profile. Native tools operate in private Git clones with minimal runtime access, no network, no other profiles, and read-only Git/handoff metadata. Relay checks the actual OS sandbox before allowing model turns. Unsupported permissions fail closed; Docker/API is never an implicit fallback.

The [noninteractive Codex documentation](https://developers.openai.com/codex/noninteractive/) and [configuration reference](https://developers.openai.com/codex/config-reference/) remain the contract for the optional Docker/API backend and its narrow inference proxy. That backend uses an empty ephemeral Codex home and never uses saved native-profile authentication.

## Credential boundaries

Relay endpoints reject raw keys, tokens, cookies and authentication directories. Native App Server child environments exclude inherited organization/API keys and proxy tokens. The trusted App Server owns its one credential home; coding tools cannot read that home or another worker's files. Authentication notifications, raw account responses, stderr and reasoning are excluded from stored events. Visible output and workspace changes use the shared redaction checks before display or Git checkpoints.

`.relay-native/` and `.env` are ignored by Git. Custom native roots must also remain outside version control. Login codes and credentials stay in the user's terminal/browser. Do not copy auth state between profiles or send it to another worker. See [deployment instructions](../deploy/README.md) for exact commands.

## What has been verified

Automated tests cover native stdio RPC handling against a test-only subprocess, isolated workspaces/environment, three-phase handoff, actual temporary code/tests, cancellation, conflict rejection, owner restrictions and quota behavior. This is mocked inference, explicitly separate from real provider execution. Real installed CLI initialization, permission-profile support and OS sandbox isolation probes passed for all three local profiles. **The exact native live command subsequently passed after independent interactive logins on three distinct ChatGPT accounts.** Three real model turns contributed code/tests/docs, two checkpointed handoffs continued the same conversation, C ran seven passing tests, and restart persistence passed. The Docker/API live route remains unverified. See [LIVE_CODEX_STATUS.md](LIVE_CODEX_STATUS.md).
