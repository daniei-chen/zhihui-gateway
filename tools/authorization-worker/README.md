# Authorization Worker Lab

Experimental finite-action browser controller, not a deployed login service.

The Manager authorization fix is independent of this lab. No production route
imports this directory. It deliberately has no HTTP/CDP endpoint, profile store,
credentials, screenshot output directory or arbitrary script/locator interface.

- `session.cjs`: explicit owner/state binding, official-only navigation, private
  Playwright context, phone/code form filling, user-triggered SMS action, official
  page frames and direct human pointer input, one-use loopback redirect handoff.
- `broker.cjs`: synthetic-only session broker with a pinned literal loopback
  fixture origin, strict finite command schema, random capability, owner/state
  binding, context/session limits, deadlines and cleanup. It cannot open a live
  provider bootstrap. The context factory is trusted in-process test code, never
  a module path, shell command, browser flag or script supplied through requests.
- `stdio.cjs`: import-only private JSON Lines transport. It has no executable
  production launcher, socket listener or HTTP route. Its caller must own both
  pipe ends; owner/state strings are binding capabilities, not proof that a
  caller is an authenticated Manager user.
- The callback is handed in memory to the caller; the Manager must validate the
  callback and PKCE session before consuming it. A redirected page cannot choose
  its own account ID or exchange host.
- Frame bytes are confidential. A future Manager transport must set no-store,
  verify the current session on every request, avoid logging bodies and destroy
  the worker/context on TTL, logout and cancellation.
- The controller destroys its context on close, expiry, startup failure and
  callback handoff completion (including unconfirmed completion). It never
  returns a full provider URL as frame metadata. Frame identity covers reloads,
  viewport changes and pointer cancellation, not only the displayed URL.
- Dynamic verification remains the actual official page. There is no CAPTCHA
  solver, OCR, fake challenge, fingerprint patch or automated acceptance of terms.
- Douyin's live redirect domains and QR element are not verified. The controller
  rejects unreviewed origins and does not advertise a working QR flow. Choosing
  Douyin currently hands the real terms page to the human.

Run the synthetic browser tests with a separately installed Playwright runtime:

```powershell
node --test tools/authorization-worker/broker.test.cjs
$env:PLAYWRIGHT_MODULE = '<absolute path to reviewed playwright module>'
node --test tools/authorization-worker/session.test.cjs tools/authorization-worker/broker.test.cjs tools/authorization-worker/broker.browser.test.cjs
```

Tests must use a fresh browser context and loopback fixture; they do not open the
real provider, send SMS, accept terms or authorize accounts. Linux deployment,
sandbox, package pinning, network allowlist, cgroup/TTL lifecycle, native Manager
UI and real-account acceptance remain required before this can be enabled.
See `docs/SERVER-ASSISTED-AUTH.md` for the intended user experience and gates.

The controller tests exercise 23 synthetic scenarios, including invalid bootstrap,
cross-owner access, consent, form filling, SMS cooldown, direct human pointer
input, stale/expired frames, resize, reload, drag cancellation, wrong/duplicate
trace, iframe/subresource callback rejection, unreviewed navigation, unknown
completion and context destruction. These run separately from the Manager
strict gate; a Manager gate receipt does not attest this experimental worker.
Human input includes a fixed key allowlist and bounded scrolling, not arbitrary
text or shortcuts. Input is rate limited, frames are invalidated by changes,
and invalid/cancelled pointer sequences release an active drag. Concurrent
operations acquire the busy slot before awaiting frame validation.

## Private Fixture Protocol

The broker is a transport laboratory, not the Manager login implementation.
Create `FixtureBroker` in trusted test code with `fixtureOrigin`, `createContext`
and optional `onCallback`; pass it to `serveStdio({input, output, broker})`.
`fixtureOrigin` must be a canonical `http://127.0.0.1:<port>` origin belonging to
a trusted synthetic page. Tests create their own server on a random loopback
port. No real provider or user credentials belong in this runtime.

Each newline-terminated JSON object contains a nonnegative integer `id`, a finite
`command`, an `owner` (1-128 characters) and a `state` capability (32-128
base64url characters). `open` also takes `authUrl`; every later command requires
the unpredictable `handle` returned by `open`. Unknown fields are rejected. IDs
are correlation numbers, not secrets; do not put user data in them.

| Command | Additional fields | Result |
| --- | --- | --- |
| `open` | `authUrl` | New random handle, bounded expiry, fixture status |
| `status` | `handle` | Phase and expiry only; no frame or authorization code |
| `action` | `handle`, `kind`, optional `value` | Finite phone/Douyin selection, phone/code fill or explicit SMS action |
| `capture` | `handle` | JPEG base64 and frame identity/bounds in this response only |
| `pointer` | `handle`, `frameId`, `type`, `x`, `y` | Frame-bound down/move/up |
| `key` | `handle`, `frameId`, `key` | Fixed navigation/accessibility key only |
| `scroll` | `handle`, `frameId`, `x`, `y`, `deltaX`, `deltaY` | Bounded human scroll |
| `cancel` | `handle` | Release pointer and invalidate frame |
| `close` | `handle` | Destroy private context; do not replay a closed session |

`action.kind` accepts only `choose_phone`, `choose_douyin`, `fill_phone`,
`request_sms` and `fill_code`. Only the two fill actions take a `value`. There is
no arbitrary text, locator, navigation, JavaScript, upload, process or filesystem
command. Bootstrap allows exactly the controller's four authorization parameters,
validates them before creating a context and accepts only the pinned fixture
origin. The fixed loopback callback is intercepted in memory, never fetched.

Responses have `{id, ok, data}` or `{id, ok:false, error:{code}}`. Request IDs are
integers so malformed input cannot reflect a secret through an error response.
There is no unsolicited stdout logging. The trusted `onCallback` receives the
callback in memory; it is never emitted on stdout. The transport strips all
unapproved status/frame fields and never writes frame files or replays frames
from `status`. The pipe owner still receives confidential frame/form content and
must not enable payload logging, pipe recording or tracing.

Default bounds are two sessions, one per owner, four active broker calls, one
active operation per session, a 520x760 viewport, 5-minute TTL, 20-second operation
deadline, 24 KiB input lines and 1 MiB JPEG frames. Configuration can lower these
limits; small hard ceilings prevent accidental unbounded test configurations.
JSONL accepts one line at a time and waits for each response to flush (maximum
5 seconds), with no user-space request queue. Oversized input, broken pipes and
EOF close the broker. It bounds individual pipe reads to 48 KiB as well; this is
an intentionally conservative private lab protocol, not a bulk transport.

Capacity is reserved before awaiting context creation. If creation times out,
the slot remains reserved until that late context can be closed. A browser
teardown that does not emit `close` is quarantined instead of falsely freeing its
slot; `close` reports `shutdown_unconfirmed`. Completed/unconfirmed callbacks
retain only a short-lived status entry (10 seconds) after the browser closes.
An unresponsive browser or context factory still requires an external process
supervisor to enforce process termination; a JavaScript deadline is not a kernel
resource limit or a guarantee that a native process has exited.

The additional transport suite covers 20 synthetic unit/fault scenarios and
three private-context browser integrations: capability isolation, finite input,
pre-launch bootstrap rejection, concurrent capacity reservation, deadlines/late
factory cleanup, TTL, disconnect/EOF, failed teardown quarantine, frame limits,
malformed UTF-8, oversized input and backpressure. These 23 tests plus the 23
controller tests pass together; none attests a working live QR/SMS integration.

The Playwright route allowlist is a navigation guard, NOT an operating-system
egress firewall. It does not establish DNS rebinding protection or cover every
browser transport (for example WebSockets/WebRTC). Do not expose this controller
through an API or run it with production credentials until the independent
Linux sandbox and transport gates in the design document are verified.
