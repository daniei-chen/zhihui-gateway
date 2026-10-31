# v0.0.3 edge admission candidate

This is a reviewed merge input, not a command to overwrite the production site.
The current site has independent signed frontend/home/login handling; preserve
their CSP, paths and retained assets. A tested whole-file successor must be
published under the existing publication/backup locks with rollback evidence.

1. Include `gateway-admission.http.conf` in HTTP context exactly once.
2. In the existing four data locations, replace `client_max_body_size 64m` with
   an include of `gateway-admission.location.conf`. Keep their proxy destinations,
   platform headers, read/send timeouts and response streaming settings.
3. Merge `gateway-admission.server.conf` once. Replace the two experimental
   callback locations rather than leaving duplicate declarations. Replace its
   two log destinations with the verified site's audit log only when necessary.
4. Add `error_page 401 = @zh_panel_unauthenticated;` to the protected `/api/`
   location. Keep `auth_request /__zh_session;` and the application's own RBAC.
   Do not open `/api/claim/`: its IP-based repeatable credential retrieval is not
   safe for anonymous use yet.
5. Validate `nginx -t`, isolated real-Nginx route/body/rate/connection tests,
   then the exact assembled production configuration before a controlled reload.
   Recheck old/new serving identities and rollback without restoring business DB.

`proxy_request_buffering off` lets Manager reject invalid headers before Nginx
spools a complete body. Manager independently revalidates the key after reading
the body and enforces aggregate byte reservations/concurrency. These are separate
layers, not a shared authentication grant. HTTP/1.1 upstream and no automatic
proxy retry are deliberate; an effectful request is never replayed to another
upstream after a transport error.

The public edge cap is 64 MiB. A larger legacy upstream or local Manager limit
does not enlarge the public allowance. Per-IP limits are 30 requests/sec plus
60 burst and 64 active connections; shared limits are 120 requests/sec plus 240
burst and 128 connections. They are resource safeguards, not provider/account
allowances, billing limits, fair scheduling or distributed application locks.
Measure legitimate shared-NAT traffic before making these stricter.

Public `/readyz` returns only cached `{"ok":true|false}` and never starts model
generation or probes providers on demand. The full authenticated diagnostics
remain in the control panel. Process health is not proof of paid inference or
provider reward delivery. The historical security-header skips remain separately
reported; edge tests are additional evidence, not replacements disguised as a
smaller test gate.
