# Current upstream review — 2026-11-14

This is the current-source review for the v1.2.25 workstream. The older
`UPSTREAM-REVIEW-0.0.4.md` remains a historical decision record; its versions
are not current production provenance.

## Fixed observations

| Component | Latest public observation | Fixed identity | Production decision |
| --- | --- | --- | --- |
| WorkBuddy Manager | `ithtelab/workbuddy-manager` v2.11.14 | commit `8b5a4df412b86d29b92c4740d0c9adc1f6750b99` | The gateway is a customized product line; the public upstream is reviewed, not silently substituted into production. |
| WorkBuddy engine source | `daniei-chen/zhihui-gateway` `upstream-src` asset | asset SHA256 `2f9f619e29094b963161e5e42c82ae20660b7f68125026d0e4960c4986c6c614`, 7,435,373 bytes; snapshot id `6118aa0a8e861046f9e9dc76ba09b0bfefe73cbedcf5f7ebbedf4609b9a10663` | Bundled source is sanitized, patched, and locked by `dev/upstream-source.lock`; no floating download is accepted by the release workflow. |
| Qoder Hub | v1.9.9 | commit `d0f4837c8f38c090f6d76baadc3da2c09f5c66c0` | v1.9.9 is the reviewed and activated production release; the serving process is rootless on `127.0.0.1:8799`, while the root-owned broker retains signed activation and rollback control. |
| Trae WorkAssistant | v3.9.3 | commit `a50d65f7a2f0d3dd3b0b8ae4179199276bd93466` | The newest public release is recorded for review. No automatic desktop/client upgrade is inferred from the server adapter. |

The three public repositories were queried on 2026-11-14. A tag or release
being newer does not prove that the local binary, container, or provider route
has been replaced. The production server must keep a separate runtime receipt.

The v1.2.25 host review first exercised the Qoder v1.9.9 candidate on a
private 8800 listener with a copied state set. It then snapshotted the
production state, normalized the serving directories to UID/GID `10001:10001`,
activated the signed broker journal, and restarted the production service on
the existing 8799 route. The serving process returned version 1.9.9 and ran as
UID/GID `10001:10001`, with `CapEff=0`, `NoNewPrivs=1`, and seccomp mode 2.
An authenticated `/v1/models` request returned 13 models, and Manager health
and readiness remained healthy after the cutover. The private canary was
stopped after this production acceptance.

The current host receipt and remaining backup/network items are recorded in
`PRODUCTION-AUDIT-2026-11-14.md`.

## Release input rule

The WorkBuddy source archive is fetched from the repository's `upstream-src`
Release so the package remains self-contained. The release workflow now checks
both the archive byte length and SHA256 against `dev/upstream-source.lock`
before unpacking it. A changed archive is a release-blocking event that needs a
new review, patch application check, upstream tests, and an intentional lock
update.

## Decisions

- Adopt only compatibility changes that preserve authorization, billing,
  bounded retry, and provider-side side-effect boundaries.
- Keep Qoder and Trae version observations separate from the Manager release;
  do not claim an engine upgrade from a UI or adapter change.
- Preserve the rule that missing provider evidence remains unknown or human
  review. A newer upstream rejection message is not proof of zero billing.
- Keep sanitized source, licenses, patch hashes, and release receipts
  inspectable without shipping credentials or runtime state.
