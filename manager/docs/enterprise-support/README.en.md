# Enterprise Account Support for WorkBuddy Manager

> **Retired historical reference.** `apply.sh` rejects every target, including sourced calls, with status 2 before reading credentials, copying files, or operating containers. The manual install, rebuild, rollback, and upgrade commands below are historical records and must not be used on current managed instances. Follow the [current product release contract](../../../docs/PRODUCT-RELEASE.md) and [current signing requirements](../../../docs/release-signing.md).

Patches that add **Tencent CodeBuddy / WorkBuddy enterprise account** support to
[ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) and
its upstream gateway `workbuddy2api`.

[中文文档](README.md)

> **The problem**: enterprise accounts keep their quota outside the personal
> "resource package" system. The personal endpoint `get-user-resource` returns an
> empty `Accounts` array for them, so the panel shows **0 credits** — and because
> credits drive the weighted-random account picker, an enterprise account with
> `credits=0` is almost never selected.

## What this fixes

| Before | After |
|---|---|
| Panel shows `0` credits | Shows real remaining quota (`limitNum - credit`) |
| Enterprise account barely selected | Normal selection weight |
| Check-in fails daily with `400 code=10001` | Panel hides the button; scheduler skips it |

## Directory layout

```
├── README.md / README.en.md                 Docs (zh / en)
├── apply.sh                                 Retired script (execution rejected)
├── workbuddy2api-enterprise-support.patch   Upstream Go patch (6 files / 307 lines)
├── manager-tencent.py.patch                 Manager credit-query patch
├── manager-wb2api.py.patch                  Manager check-in gating patch
└── verified/                                Historical upstream reference only
    └── upstream-enterprise_usage.go
```

## One-shot installation is retired

This directory no longer provides an installation entry point. The historical script cannot preserve the full container environment, image identity, and mount attributes when rebuilding a container. Its two Manager drop-in files are also absent from this public tree. Do not restore those old files or bypass the rejection guard. Use the managed release process linked above; the old implementation remains only for code review.

## The key gotcha

`billingJSON → doJSON` **already unwraps the envelope** and returns `env.Data`.
Parsing it again with a `{code, msg, data}` struct makes `LimitNum` / `Credit`
silently fall to zero — the quota reads `0` with **no error at all**.

Parse the data body directly:

```go
var resp enterpriseUsageData   // business fields only, no code/msg
json.Unmarshal(data, &resp)
```

## Quota semantics (opposite of personal)

```
personal:   remain = CapacityRemain
enterprise: remain = limitNum - credit     // credit is *used this cycle*
            limitNum == -1 → unlimited
```

## New endpoint (upstream)

```bash
POST /v1/balance/refresh
Authorization: Bearer <api_key>
```

Refreshes all account balances without checking in — useful because upstream only
refreshes credits at the scheduled check-in hours (09:00 / 21:00).

```json
{
  "total": 3,
  "succeeded": 3,
  "accounts": [
    {"uid": "...", "nickname": "1501483050822569239", "credits": 1658, "expiring": 0},
    {"uid": "...", "nickname": "LzZ", "credits": 899, "expiring": 0}
  ]
}
```

## Verified (2026-10-18)

| Account | enterpriseId | panel credits | checkin_supported |
|---|---|---|---|
| enterprise test | `g12ad6q2qwao` | **1658** ✓ | **False** ✓ |
| personal A | — | 899 | True |
| personal B | — | 600 | True |

Cross-check: Tencent endpoint returns `credit=342, limitNum=2000` → `2000 − 342 = 1658` ✓

Full test suite: **18 packages pass**.

## Upgrading

Upstream patches are **overwritten on upgrade**. Re-apply with:

```bash
git cherry-pick a80939d c4a3a48 3f91f42 b2b3a49
```

Manager patches are volume-mounted and survive image upgrades, but must be
regenerated if the target files (`services/tencent.py`, `services/wb2api.py`) change.

## Rollback

**Upstream**: `git checkout 33d1414 -- .` or restore `build-wb2api.bak-*`

**Manager**: rebuild the container without the two `-v .../patches/...` mounts
(the image files themselves were never modified).

## Notes

- Enterprise accounts cannot check in — Tencent returns `400 code=10001`.
  This is a server-side restriction, not a bug we can work around.
- Enterprise quota resets on the cycle end date (`cycleEndTime`); unused quota is forfeited.

## License

MIT. Upstream workbuddy2api and workbuddy-manager are MIT too.
