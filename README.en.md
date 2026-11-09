<div align="center">

# Zhihui Gateway · Manager

**One workspace, three clearly separated paths.**

![Version](https://img.shields.io/badge/version-0.0.9-blue)
![Status](https://img.shields.io/badge/release-signed_%26_controlled-6554c0)
![Stack](https://img.shields.io/badge/FastAPI-Next.js-009688)

**English** · [简体中文](README.md) · [Product overview](../README.md) · [Changelog](CHANGELOG.md)

<img src="docs/images/dashboard.png" alt="Dashboard with isolated synthetic data" width="100%" />

</div>

> **0.0.7 is Zhihui's product version, not an inherited component's v1.x / v2.x version.**
> Installation is controlled by the trusted publisher. Runtime identity and acceptance receipts determine what is actually active. Isolated Linux gates, signatures, paired backups, graceful draining and real acceptance are mandatory; code rollback never restores old business ledgers.

## Components And Boundaries

### Local Candidate: One Default Key And Invitation Registration

These changes remain in the local candidate. They are not enabled in production until the full gates, signatures and an explicit deployment instruction are complete.

- The homepage separates user login and administrator login. The user login page offers invitation registration.
- Each workbench reuses one existing default key and does not offer multiple-key creation. A user without a key may initialize it once; existing plaintext cannot be read again and is not automatically rotated. Current key labels and statistics use the owning username, without rewriting usage history or ledgers.
- Extra keys retained from older versions stay valid to avoid interrupting existing clients. The workbench displays one default and prevents further issuance.
- Registration requires a one-time administrator invitation, a server-signed browser registration credential and persistent IP, network, device and global rate limits. Only the invitation hash is stored; plaintext is returned once. Registration grants no calling credits.
- Device registration binds a browser, not an unforgeable physical device. Clearing cookies or changing browsers still requires another valid invitation and remains subject to rate limits.
- The public reverse proxy must overwrite client-supplied IP identity headers and use the correct trusted proxy ranges. The example configuration requires trusted-publisher review; it does not automatically alter production.

Manager provides accounts, models, keys, logs and the administration UI. The WorkBuddy engine handles the account pool and provider requests. Qoder / Trae retain their separate adapters and authorization boundaries.

This is a maintained branch with local adaptations, not an unmodified-upstream panel. Candidate source, licenses, tests and build identities are retained. Historical installation guides are component references, not substitutes for the controlled release contract.

## v0.0.7 Highlights

| Problem | Release behavior |
|---|---|
| Codex / Claude / DSH channel rejection | 11 versioned rules scoped to WorkBuddy system/developer, including Claude literal-template variants |
| Amplified 11128 retries | Stop after both candidate paths reject; no repeated identical path or account penalty for refusal |
| Concurrent account renewal | Go / Python coalescing, shared kernel locks, identity/generation checks and atomic persistence |
| Implicit high reasoning on small budgets | No default thinking/high injection for budgets ≤1024 without explicit controls; native reasoning is not forcibly disabled |
| Direct deletion of expired logs | Bounded background archives, line/SHA verification and exact-row deletion; unresolved references remain protected |
| Unknown usage mistaken for wallet freezes | Read-only review with bounded evidence; unknown is neither measured zero nor wallet state |
| Catalogue staleness and loading races | Coalescing, realm isolation, invalidation generations and freshness metadata |
| Initial readiness sampling race | Await a real startup sample while preserving fail-closed and admission-drain rules |
| Legacy updater branding and history mixed with current state | Independent Zhihui release center, source and matching receipts; retire web installation and explicitly archive legacy display files |
| Layered retry amplification | A shared maximum of 8 provider-send authorizations across rotations, paths, gzip and Manager redispatch; missing or invalid receipts disable further rejection retries |
| Full credit queries on page load | Local snapshot first; automatically refresh stale entries only, at most 64 per batch with failure backoff; preserve manual refresh and unknown-versus-zero semantics |
| Replacing unchanged heartbeat data | Bounded structural sharing preserves stable JSON and row references without hiding changes or crossing group boundaries |
| Ambiguous 502 diagnostics | Fixed error-family metrics and independent reachability, readiness, authentication, model, quota, channel and stream facts; bare 11128 is not automatically a channel refusal |
| Unclear source and capability drift | Offline review of 39 public sources, explicit adaptation contracts, synthetic protocol fixtures and fragmented SSE fuzzing |

The `.4` cancellation, revoked-key and paired-backup fixes remain intact. Soft credit floors and large-request distribution default to disabled. See the [0.0.7 notes](docs/releases/v0.0.7.md) for scope and limitations.

### Adaptation Is Not Provider Authorization

Rules are idempotent and switchable. They do not alter user, assistant or tool bodies, tool arguments or images, and do not globally replace the number `11128`. Real refusals remain `channel_check_rejected`; future channel checks cannot be guaranteed unchanged.

Responses is an adapter, not a persistent-session, background-task or strict-tool-schema service. Unsupported semantics are explicitly rejected before provider consumption.

### Preserve Accounting Truth

Unknown provider usage and customer wallet holds are separate concepts. Lifetime consumption is not reconstructed from truncated logs, and immutable events are not rewritten. Review has no zero-out button; code rollback never restores an old business database.

## API Overview

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/chat/completions` | Chat-compatible calls using downstream keys |
| POST | `/v1/responses` | Bounded Responses adaptation |
| POST | `/v1/messages` | Messages adaptation |
| GET | `/v1/models` | Models available within the authorized scope |
| GET | `/api/budgets/review` | Administrator read-only paginated review |
| GET | `/api/budgets/review/evidence` | Administrator bounded evidence view |
| GET | `/api/logs/retention/status` | Most recent archive sample |
| GET | `/readyz` | Bounded-age cached readiness without inference probes |

## Documentation

| Document | Contents |
|---|---|
| [Upgrade and rollback contract](docs/V007-OPERATIONS.md) | Defaults, approval gates, backups and failure recovery |
| [Upstream adoption](docs/UPSTREAM-ADOPTION-v007.md) | Pinned sources and explicit adoption decisions |
| [Usage](docs/usage.md) | Everyday operation and client connection |
| [Features](docs/features.md) | Existing capabilities and limitations |
| [Screenshots](docs/screenshots.md) | Screens and isolated examples |
| [Development and tests](docs/development.md) | Component development reference |
| [Security configuration](docs/security-notes.md) | Administration and network boundaries |
| [Management API tokens](docs/api-tokens.md) | Scopes, expiry and session requirements |
| [Account proxy routes](docs/account-proxy-routes.md) | Proxy settings and account scope |
| [Historical security audit](docs/SECURITY-AUDIT.md) | Component history, not this release's acceptance proof |
| [Component signing reference](docs/release-signing.md) | Never bypass the installed controlled trust chain |
| [Deployment reference](deploy/README.md) | Includes native Windows templates; legacy one-click publishing is not valid for the controlled instance |

## Release And Non-Goals

Prepare local artifacts and receipts only, then wait for explicit user approval. Approval still requires isolated Linux full gates, complete signatures, paired backups, controlled drain, version checks and bounded live acceptance. Never force-kill in-flight requests.

No new sales, fee operations, advanced Responses state, media, offsite backups or Rust rewrite. Weak passwords and existing administrator policy are excluded from this task, not declared fixed. Existing component licenses and attribution are preserved.
