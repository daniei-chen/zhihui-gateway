# Production audit — 2026-11-14

This report records the evidence collected from the domestic production host
`xiaochen` during the 2026-11-14 audit. It is an operational receipt, not a
claim that every possible external provider or disaster-recovery condition has
been proven.

## Current serving state

| Area | Observed state | Evidence |
| --- | --- | --- |
| Gateway | `v1.2.24` serving release, Manager and readiness healthy | `/healthz` and `/api/readyz` returned 200; release pointer was `v1.2.24` before this documentation release |
| Public large body | Fixed | A 4 MiB HTTPS `POST /v1/chat/completions` returned the expected 401 authentication response, not Nginx 413 |
| Nginx 413 | No new 413 after the relay admission fix | The eight 413 access-log entries are from 11:52–11:57, before the v1.2.24 configuration was active; the post-fix probe window added zero 413 entries |
| Qoder | `1.9.9` on `127.0.0.1:8799` | The signed Qoder broker reported `from=1.9.8`, `to=1.9.9`, `ok=true`; the pending journal was cleared |
| Qoder identity | Rootless production data plane | UID/GID `10001:10001`, `CapEff=0`, `NoNewPrivs=1`, seccomp mode 2; state and logs are external and owned by `10001:10001` |
| Trae | Container runs as `10001:10001` | Docker inspection showed `privileged=false`, `user=10001:10001`, loopback publication only |
| Network boundary | Public listeners are limited to SSH/HTTP/HTTPS | App listeners remain loopback-only; UFW default incoming policy is deny |

The Qoder cutover used the broker's signed release verification, an expired
prepared journal snapshot, a fresh administrator review timestamp, atomic
pointer activation, systemd restart, health/version verification, and an
authenticated model-list check. The production route stayed on 8799, so no
gateway route-file edit was needed. A private 8800 canary was stopped and
disabled after the production cutover.

## Data and runtime checks

- `manager.db` passed SQLite `integrity_check` and `quick_check`; schema
  `user_version` is 9 and journal mode is WAL.
- The database contains the expected current tables, including `request_logs`,
  `request_outcomes`, `request_performance`, `usage_events`, and the provider
  dispatch/evidence tables. The earlier probe for a literal `users` table was
  a wrong table-name assumption, not a database corruption signal.
- The active Manager and relay processes run with UID/GID `10001:10001`, zero
  effective capabilities, `NoNewPrivileges=1`, and seccomp mode 2.
- No recent Manager traceback, `PermissionError`, or HTTP 500 was found in the
  reviewed journal window. Historical 503 bursts on 2026-11-13 are recorded as
  upstream/service availability events and are not reclassified as 413.

## Security findings closed in this window

1. Exact relay inference locations now include the shared admission snippet,
   so Nginx's default 1 MiB body limit cannot shadow the gateway's 128 MiB
   edge contract.
2. The Qoder production process no longer runs as root with broad capabilities.
   The root-only broker remains isolated to release verification and atomic
   activation; serving code is handed to UID 10001 before it starts.
3. Qoder account, usage, and log state was snapshotted before the identity
   handoff, then normalized to owner-only directories/files for the serving UID.

## Open items

- The encrypted Qoder/Trae backups are local same-filesystem copies. The host
  explicitly reports `offsite_verified=false`; an offsite provider or mounted
  second filesystem still needs an owner, retention policy, and a restore drill.
- SSH password/root login are disabled and public app ports are not exposed,
  but SSH forwarding and idle-session policy should be tightened only after
  confirming the administrator's tunnel requirements.
- The historical 503 burst and the two historical Nginx 502 entries deserve a
  separate provider-availability review; they are outside the 413 fix and no
  current 500 was observed in this window.
- The Qoder rootless launcher and systemd drop-in are now source-controlled in
  this release. The host installation remains a separate root-owned deployment
  asset and must continue to be hash-checked during future host changes.
