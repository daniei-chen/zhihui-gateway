# Request Upload Deadlines

The `v0.0.3+body.20261031.1` candidate addresses local HTTP 408
`body_read_timeout`. It does not change provider timeouts, retry inference,
disable body limits, or modify keys, quotas, recipients or historical records.

## Limits

| Setting | Default | Accepted Range |
| --- | --- | --- |
| `WB_GATEWAY_BODY_READ_TIMEOUT_S` | 180 seconds | 1-600 seconds |
| `WB_GATEWAY_BODY_IDLE_TIMEOUT_S` | 60 seconds | 1-120 seconds |

Both values are validated before startup. Changing them requires a controlled
restart. The absolute deadline starts at body reception; receiving more bytes
does not renew it. The idle deadline renews only for nonempty body data, not
empty ASGI frames. Whichever deadline expires first ends the upload.

The same implementation applies to admission buffering and the JSON readers
for Chat Completions, Responses and Messages, including platform-prefixed
routes. Previously, both admission and the protocol reader had separate
hardcoded 30-second deadlines; changing only the protocol reader was insufficient.

Authentication, byte/count admission, per-request size limits and lifetime
reservations remain in place. Timeout/cancellation releases owned admission
resources, does not select an upstream account and does not replay a model call.

## Compression Is Not An Upload Deadline

Browser/static response gzip operates server-to-client. WorkBuddy request gzip
operates engine-to-provider. Neither compresses the earlier client-to-Manager
upload. An external client's own context compaction can reduce its upload size
but does not guarantee delivery within any particular duration.

The candidate explicitly accepts one gzip member with `Content-Encoding: gzip`.
Wire and decoded data are independently bounded. CRC, truncation, concatenated
members, trailing bytes and aggregate capacity are validated before forwarding.

`manager/client/zhihui_upload.py` implements the client-side policy: skip below
256 KiB, gzip level 1 above it, use gzip only if it saves at least ten percent.
Third-party clients must opt in; server changes cannot compress bytes before a
remote client uploads them. The decoded limit remains 128 MiB.

The reviewed host transaction sets Manager input/forward limits to 128 MiB,
retained input+output budget to 384 MiB, and large concurrency to 2. WorkBuddy
retains a 256 MiB raw-body budget with a 128 MiB per-request limit. These are
byte budgets, not whole-heap guarantees: parsed objects/temporary copies still
need the process memory guard. Qoder/Trae retain their separate provider limits.

## Evidence And Release Boundary

The initial read-only production check found the key-format release healthy,
with no timeout configuration and both 30-second source deadlines intact.
The journal contained local 408s for Chat Completions and Messages. It did not
contain sufficient body timing/size evidence to prove that each case was a
large request, a network stall or event-loop scheduling delay. Exit IP location
alone is not proof of cause.

Synthetic tests cover progress beyond 30 seconds, total/idle expiration, empty
frames, cancellation, schema bounds, and no application/provider dispatch on
timeout. Production activation and a harmless slow-upload check are recorded
separately; a passing local test alone is not a deployment receipt.
