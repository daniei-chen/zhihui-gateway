# Integration History And Current Three-Platform Release

## Current Decision — 2026-10-30

The selected release contains WorkBuddy, Qoder and Trae only. Codex / ChatGPT
will be evaluated as a separate site and backend later, not shipped as a fourth
provider. The original production login is retained byte-for-byte, including its
three-platform text. Homepage selectors, routing examples and README use the
same three-platform scope. Earlier four-gateway notes below are historical
design and verification context, not the current release contract.

The task workspace and coastal cycling scenes now use original local HTML,
CSS and SVG artwork. Reference-site illustration source is retained privately
as audit evidence, not included in the public source or selected asset closure.
The attributed Thinker model retains its separate CC BY-SA license.

Candidate review: 2026-10-30, Asia/Shanghai. This document is not a deployment receipt.

## Delivered In Source

- The selected homepage uses the blue-white Z identity. Login retains the exact
  serving original layout, styles, SVG and behavior; only two platform text
  strings name Codex, WorkBuddy, Qoder and Trae and say four gateways. The new
  three-dimensional login concept from the supplied export is not adopted.
- Homepage assets have content-derived URLs because production caches
  `/home-assets/` as immutable. Different bytes must never reuse the same URL.
- Essential hero text and the console entry stay painted from the first frame.
  Decorative motion can pause without hiding navigation or content.
- The Qoder account page separates confirmed positive rewards, zero rewards,
  already-claimed evidence and unknown outcomes. It does not increment balances.
- Qoder consumption uses the requested period and region. Its engine does not
  expose a complete income ledger, so income and net remain null, not zero.
- Manual, per-account, batch, plan and automatic Qoder/Trae dispatches share a
  private OS lock and durable attempt fence. Lost or pending results do not
  authorize another dispatch, including after midnight or Manager restart.
- A scheduler restart can reconcile its exact attempt when a full terminal
  response was already persisted and the dispatch lock is released. Matching
  timestamps, old account rows or another attempt are not sufficient. A failed
  account row can follow a sent claim, so it does not authorize automatic replay.
- A completed whole-pool manual check satisfies that day's automatic run; a
  single-account check does not. Qoder history reads only Qoder's local activity
  and account pool, not an all-platform statistics aggregate.
- Trae task submission is pending, not completion. An earlier row in today's
  results table is not evidence that the newly submitted job has finished.
- Realm help now belongs to the selected platform. Qoder and Trae no longer
  inherit WorkBuddy domain or task-policy explanations.
- Manager automatic dispatch observes Qoder2's authenticated scheduler status.
  When the engine owns automatic check-in, Manager does not dispatch or mark
  a reward complete. Disabled, malformed or unreachable engine status does not
  authorize takeover: the current protocol exposes no engine idle/lease proof.
  The account history labels engine ownership or unverified ownership separately
  from the result of an actual check-in. No provider schedule is changed.

These changes preserve the existing account-intake role boundary, independent
daily key counters, quota settlement and long random API keys. They do not
grant old keys access to a new provider.

## What Qoder Evidence Actually Establishes

Read-only engine activity shows positive rewards earlier in the period and
zero added credits on October 27, 28 and 29. A historical last-credit delta of
100 is not the latest reward. The Manager previously conflated a completed
check with a new reward, and its income chart fabricated zero income from a
consumption-only endpoint.

These observations explain the misleading interface, not the provider's full
campaign eligibility or reward-issuance policy. Local activity is bounded,
rotated evidence, not a complete provider billing or reward ledger. No extra
provider check-in, OAuth exchange or SMS was triggered to investigate this.

## What Still Prevents A Final Release

| Boundary | Remaining acceptance |
| --- | --- |
| Codex production identity | The supplied engine/workbench is a mock-transport integration package. Production OAuth mode, authorized scope, client registration and Linux acceptance are not established. Do not silently substitute an API key or reuse desktop credentials. |
| Codex quota and settlement | Trusted account/window observations, Manager RBAC/pool/key bindings, native Responses routing and reliable usage delivery are not connected. Unknown usage must remain unknown. |
| Pending check-in reconciliation | Exact, already-persisted terminal responses can repair scheduler bookkeeping. Lost/pending/failed engine results still need attempt-correlated completion and proof the engine operation has stopped; age, midnight or a random adapter generation cannot unlock them. |
| Competing engine writers | Manager no longer independently schedules Qoder2 claims. Its direct/manual interface can still compete with an engine action; a cross-process lease/attempt protocol is required before claiming global exactly-once execution or safe takeover. |
| Release and rollback | Apply the signed-source gate, persistent compatibility floor and writer closure. Code rollback must preserve current users, revocations, accounts, credits and token rotation. Old code that ignores new roles or quotas is not an acceptable rollback target. |
| Public artwork rights | Task/bicycle illustrations retain their reference provenance; redistribution permission is not established. Replace with original work or clear rights before treating them as publishable original artwork. The Thinker and renderer dependencies keep their separate licenses. |

The recovery above closes a local bookkeeping crash window, not a lost engine
response. Trae's current submission/SSE protocol has no attempt-correlated
completion receipt; it remains fenced. Read-only inspection also confirms the
serving Qoder engine's own scheduler is enabled (09:00/21:00 check-in and 22:00
keepalive). Manager's lock cannot serialize that independent writer. The candidate
therefore defers Qoder2 automation without treating its status as reward evidence.
No schedule was changed and no new check-in was sent during this investigation.

## Codex Authorization Decision

Official documentation distinguishes a same-user/workspace self-hosted VM from
a paid or remotely hosted offering. The documented VM flow completes OAuth
locally with this tool, transfers that selected protected registration over SSH,
preserves the VM's own host ID and lets the VM own later refreshes. It is not
permission to copy desktop credentials silently or turn a user's subscription
into an unrestricted multi-user account pool. Confirm the intended audience and
authorization mode before implementing live Codex transport. No API key is
silently substituted for OAuth, and the supplied engine remains mock-only.

Sources inspected on 2026-10-30:
[ChatGPT plan usage](https://developers.openai.com/siwc/token-sharing-open-source),
[sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), and
[self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms).

## Verification, Not Certification

Latest exact backend/frontend-export diagnostic: `qoder-credit-calendar-r1`
passes 3,778 tests with nine existing skips, zero failures and 2,168 subtests.
All 1,076 frozen manager/web/export files match the candidate; build identity
is `doDE85ror5LWm6i-auKf6`. It exercises private namespaces and 42 loopback
provider-fixture requests, not a production cutover. The exact credit parser
also passes read-only local-engine calendar checks for CN, international, all
regions and selected 1/7/30/90-day windows. Five separate ASGI fixture checks
verify JSON serialization, midnight preservation, unknown income, HTTP 502 on
missing calendars and HTTP 422 on invalid realm. Production authentication and
new browser pixels are not covered by that ASGI fixture.

### Qoder Usage Scope And Calendar

The candidate reads selected daily/model windows directly from the serving
engine's `/usage/daily?days=...&realm=...&group=model`. Its `/usage` endpoint
ignores `range`; it is used only for explicitly scoped lifetime totals, never
as a substitute for a selected one/seven/thirty/ninety-day model table. Model
groups have an explicit total but no trustworthy input/output split, cache
ratio or output speed. Those unavailable metrics stay unknown, not zero.

Hourly charts now read `/usage/timeseries` from the same Qoder engine rather
than the separate QTG forwarding ledger. `QTG_QODER2_TIMEZONE` defaults to
`Asia/Shanghai`, matching the inspected serving engine. Each read verifies its
current calendar date and midnight against the engine's daily evidence before
issuing a bounded custom window. A disagreement fails rather than silently
showing a different day. Minute buckets fold into local hours, including
half-hour DST changes and repeated hours. The next midnight is excluded.
The chart preserves explicit total tokens and uses the engine's current-hour
marker instead of the browser clock.

This is not a promise of instantaneous equality between independent snapshots.
The serving engine counts token-bearing error rows differently in daily and
timeseries endpoints, and its local usage log is not a full provider ledger.
Credit flow now preserves the engine's complete, contiguous day labels rather
than refilling them on the Manager calendar. Today and the last seven reported
days follow the engine's latest date. Missing/gapped/stale calendars fail closed;
invalid amounts and aggregate overflow stay unknown. Neither consumption nor
a completed check-in establishes income or net gain. By-key statistics still
need stable key/realm lineage; their correctness is not established by these
daily/model/hourly/credit checks.

The exact hourly parser was exercised against the serving engine using fifteen
local usage GETs and one local panel login. CN, international and all-region
hourly/calendar contracts pass; observed count/error/token totals agreed for
that snapshot. No provider check-in, inference, balance refresh or business
mutation was sent. The local panel login does update local authentication
state. This check does not install the candidate.

The prior `qoder-scope-r3` frozen input passed 3,764 tests, nine existing skips,
zero failures and 2,065 subtests. Its 1,076 files match the prior build
`xHIn9mVaEE4Kub33bmkmm`; later hourly changes require their own frozen gate.
Current local hourly/config tests pass 107 tests with one Windows `tzset` skip
and 216 subtests; the frontend passes 63 Node tests and builds 26 static
routes. Fresh browser checks are unavailable because local browser use is
disabled in Work settings; no alternate browser path was used to bypass it.

- The frozen `qoder-four-r2` Linux diagnostic passed 3,733 tests with nine
  existing skips and zero failures. It ran in private mount/network/PID
  namespaces with read-only runtime, not in the serving application.
- The final `qoder-four-r3` input, including later realm/history refinements,
  passes 3,734 tests with nine existing skips, zero failures and 1,977 subtests.
  Its 1,074 frozen files match the candidate source and exported web bytes.
- The subsequent `qoder-recovery-r2` input passes 3,749 tests with those same
  nine skips, zero failures and 2,000 subtests. Its 1,074 source/export files
  match the current candidate; this includes recovery, stricter persisted
  evidence and isolated Qoder history reads. No serving release was installed.
- The current `qoder-owner-r2` input passes 3,753 tests with those same nine
  skips, zero failures and 2,012 subtests. All 1,074 source/export files match
  the frozen input. This includes engine-owned automation and fail-closed
  takeover checks; it is an isolated diagnostic, not a production deployment.
- The current frontend Node suite passes 60 tests; TypeScript and 26 static
  routes build. Local CUA checks exercise actual exported pages with synthetic
  data, not live provider authorization or production writes.
- The editable homepage passes all 21 tests, including geometry comparison
  against the supplied uncompressed local reference. Import/materialize
  round trips verify 56 files and refuse overwrite of an existing edit tree.
- Static checks verify hashes, MIME, CSP, local credential isolation and
  visible-first-frame rules. Browser checks cover 320/390/768/1440 widths,
  local console navigation and decorative-renderer failure fallback.

The newer scheduler-ownership notice passes component tests and a production
build, but has no fresh browser/pixel acceptance. Those browser observations
apply to the earlier layout, not an unperformed new real-device test.

The earlier login browser screenshots above describe the now-rejected concept,
not the preserved original. `site/login/preservation.json` records the serving
original SHA256 and its two permitted text differences. Homepage import and
materialization no longer replace this login. Fresh preservation checks must
accompany its release; do not use the concept screenshots as acceptance proof.

See [homepage sources](../site/home-source/INTEGRATION.md),
[release contract](PRODUCT-RELEASE.md), and
[authorization contract](AUTHORIZATION.md). No combination of synthetic tests
proves that every production path is error-free.
