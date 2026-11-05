# Frontend and credential-boundary audit handoff — 2026-11-05

Baseline: `4c6b4cb8cb790698e0753909745dfe8a7c78dd9f` (v0.0.7), worktree `/workspace/scratch/913dc6e1a7c0/zhihui-gateway`, branch `audit/strict-review-20261105`. This agent modified frontend source/tests and the separately authorized WorkBuddy account authorization/probe paths plus their tests. No commit, push, deployment, real credential use, or real provider traffic. Backend ownership remained frozen after the probe regression run; frontend business code was subsequently authorized for the ten P2 fixes below and is now frozen after its final four gates.

## Coverage and limits

Read frontend `AGENTS.md` and installed Next 16.4.3 static-export documentation before changes; applied `vercel:react-best-practices`. Dependencies were copied into the isolated checkout from the earlier checkout without changing the earlier checkout or its user edits.

`frontend-coverage.tsv` now records 714 paths after merging the additional business reads and new tests: **187 manual-full, 12 manual-targeted, 66 static-lint/typecheck/build-only, and 449 inventory-only**. Frontend/site/design scope is 693 paths: 175 full, 3 targeted, 66 gates-only, 449 inventory-only. The separately requested backend coverage adds 12 full and 9 targeted files. `frontend-reviewed-files.txt` lists every manual classification. Full means the complete file was read; targeted is not a line-by-line claim. The inventory-only paths include historical generated homepage/vendor assets and documents as well as unreviewed source. This is explicitly **not** an assertion that every repository file or every frontend line was manually audited.

The six requested credential/backend modules were fully read: `routers/accounts.py` (2,741 lines after changes), `services/tencent.py` (1,717), `services/platformauth.py` (520), `services/credentialrefresh.py` (67), `services/credentiallease.py` (128), `services/renew.py` (378). `services/filelock.py` and `upstreambody.py` were also fully read. Core security/db and gateway components remain other agents' ownership; their references here are targeted dependency review.

Frontend sinks searched and inspected included `dangerouslySetInnerHTML`, DOM HTML assignment, eval/new Function, external navigation, storage, fetches and polling. No unsafe application HTML sink was identified in inspected Next runtime source patterns; this is not proof of absence of every XSS. Cached UI roles are not server authorization. Static export requires backend/proxy response-header policy. Minified vendor bundles, historical homepage exports, all locale strings and full browser journeys were not exhaustively manually exercised. Visual primitives were assigned to the gateway agent and retain a separate ledger. `frontend-business-extra-reviewed.tsv` adds 106 complete business source reads (21,492 lines at read time; 21,993 after fixes), while `frontend-extra-reviewed.tsv` records the separately requested QTG full reads. These separate ledgers must be unioned by path, not summed blindly.

## Confirmed frontend issues fixed

| ID | Severity/context | Original defect | Fix and evidence |
|---|---|---|---|
| FE-01 | P1 browser resource boundary | SSE checked data bytes only after a newline; an unterminated data/comment/id/event/unknown-field line bypassed its limit. Every fragment rescanned the buffered line, producing quadratic CPU work. | `lib/sse.ts`: incremental scan, raw-line UTF-8 byte limit, bounded fragments, validated limits, terminal overflow, initial-only BOM stripping, safe-integer retry. Fragmented Unicode/CRLF/BOM and complete/unterminated boundary tests. |
| FE-02 | P2 stream latency/resources | DONE, protocol error or parser overflow did not stop reading until HTTP EOF. Oversized delta reached the UI before answer truncation. | `lib/chat-stream.ts`: terminal read break, cancel unread response and release reader, no terminal/aborted flush, clip before accumulation/delivery. Fake second read throws; tests prove one read and one cancel/release. |
| FE-03 | P1 UI identity integrity, no server bypass claimed | Old `/api/me` response could overwrite a newer login, clear it with a delayed 401 or resurrect a logged-out cache. Refreshes were not coalesced. | `lib/auth-context.tsx`, `lib/api.ts`: abort/fence obsolete reads and identity commits, shared pending refresh, Strict-effect replay cleanup, aborted 401 cannot redirect globally. Four actual-provider regressions failed before and pass after. |
| FE-04 | P2 realm correctness | Playground realm cancellation left its generation current; old usage/finalizer could alter a replacement stream/session. | `app/(main)/playground/page.tsx`: invalidate before realm/unmount cancellation, clear old in-flight UI ownership, check generation before applying usage. Actual-component race test. |
| FE-05 | P2 task correctness and duplicate work | Failed status read was treated as finished, invoked onFinished and changed active polling 2s→15s. Late response wrote after unmount; same-tick action dispatch could duplicate. | `lib/use-task-run.ts`: preserve last successful running status, active-generation guard, synchronous shared action gate. Three failing-before/passing-after regressions. |
| FE-06 | P2 verification gap | Default npm test omitted existing CJS suites; baseline lint had three errors. | `package.json` includes all `tests/*.test.cjs`; two stale CSS assertions now match the already-existing main/portal selector. Two local `module` names fixed. Conditional dynamic import keeps public demo compile-time isolation. No CSS or permission assertions were removed. |
| FE-07 | P2 account balance attribution | Manual credit refresh and per-account actions could apply old-group results to a new group; old automatic read could overwrite a newer manual refresh; repeated click duplicated provider fan-out. | `app/(main)/accounts/page.tsx`: shared group/admin/unmount generation, bulk-read generation, all credit/metadata/notice commits fenced, synchronous manual gate, action credit evidence invalidates older bulk reads. Five real-component regressions all failed before and pass after. |

UI regressions execute the actual transpiled providers/components with a hook harness and deferred promises; they are not claimed as Playwright/browser tests.

## Additional authorized business fixes — final checkpoint

All ten F-BIZ P2 findings from the full-read supplement are fixed locally, without backend API contract changes or dependency upgrades. `frontend-business-extra-notes.md` contains the original evidence, current fix table and validation boundaries. The changes require successfully loaded model/proxy/upstream state before writes; serialize whole-config mutations; fence key export/import results by dialog/target generation; erase password content on every successful confirmation close; expose retryable plan failures; correct prefix-aware packet links and claimed-key rendering; normalize empty durations; and implement bounded, cancelable, hidden-tab-aware PgSync polling with unknown-status write gates.

The final tests comprise 19 actual-source component cases in `audit-business-state.test.cjs` and 14 in `audit-plan-claim.test.cjs`. Independent cross-review found three narrow new interleavings before freeze; these were repaired and regressions added: Proxy old callback vs just-started read, PgSync hidden first status vs unknown idle, and portal deletion confirmation vs writable state. Cross-browser/admin lost updates still require server revision/CAS; front-end single-flight is not that guarantee. Already-issued mutations are not represented as canceled server operations.

Current source hashes and delegated ownership are in `frontend-p2-fixed-files.tsv`. All four final frontend gates passed after the last business change, and source is frozen. No production browser latency or provider throughput claim is added.

## Confirmed credential/backend issues fixed

### BE-01 — P1 account authorization could write credentials into the wrong account pool

`auth_start` previously checked only group existence. A forwarding-only group has no `auth_dir`; `_save_and_finish` passed None to `tencent.write_auth_file`, which silently selected `config.AUTH_DIR`. Deleting/editing a group or changing its directory/endpoint during provider awaits could also invalidate the original authorization destination.

`accounts.py` now captures `(Path.resolve(auth_dir), normalized base_url, container)` before the provider call. Missing directories fail closed before issuing a grant. The binding is stored alongside the existing user authorization generation and realm/group identity, rechecked after provider awaits and on every authorized HTTP step, and rechecked immediately before final persistence under the existing authorization guard and DB writer transaction. The final writer uses the captured resolved directory with no default fallback. Group deletion, directory/instance changes and symlink retargeting reject the grant; rename-only edits remain valid. Default `None` and explicit `0` refer to the same group. Qoder/Trae retain their existing engine-generation/owner binding rather than adding an inconsistent filesystem binding.

New `test_audit_account_scope.py`: 8 methods cover forwarding-only group, directory removal, provider-pending directory/endpoint/deletion, start-time changes, symlink retargeting, final save without a supplied group snapshot, default aliases and rename compatibility. All use temporary SQLite/data/auth directories and synthetic credentials. Baseline produced 8 failed subcases plus one default-alias error; unchanged-group positive behavior passed. Current 8/8 pass. The related 9-module suite is **213/213 pass**. Existing platform_auth/user_roles mocks were updated to include the now-required actual group name/auth_dir; assertions were unchanged.

The DB critical section intentionally spans the final small atomic credential write to prevent concurrent persisted group configuration changes redirecting it. It includes filesystem fsync, so unusually slow storage can briefly occupy the DB writer. No cross-process database or file-lock guarantees beyond the project's existing implementations are invented.

### BE-02 — P2 probe could report an invalid account healthy

`tencent.probe_account` previously considered any nonempty HTTP-200 byte chunk healthy, including `data: {"error":{"code":12153}}`, a heartbeat-only stream, HTML, or incomplete JSON. This is demonstrated by real HTTPX MockTransport, without network traffic.

`_probe_response` reuses `observability.StreamAudit` with a 64 KiB event/line budget and adds a 256 KiB cumulative body budget (including HTTP error bodies). Only valid string content/reasoning or a valid stop/length choice establishes success; role-only, DONE-only, heartbeats, non-SSE JSON success, malformed JSON and invalid choice field types do not. Every parsed error in a coalesced chunk takes precedence over its earlier content. Nested/flat business errors preserve their code/message and the existing credit-expiry explanation. HTTP-200 JSON errors also preserve their provider code. Channel errors retain the alternate-path behavior.

The entire probe, including alternate path, uses `asyncio.timeout(config.TENCENT_TIMEOUT)` so heartbeats cannot extend it indefinitely. The request asks for identity encoding and rejects unexpected compression before body decoding; unlike the full gateway, this deliberately small diagnostic does not accept a provider that ignores identity negotiation. Responses close on success, error, timeout and overflow. Latency uses monotonic time. Successful first evidence does not promise that a provider would never fail later in the unconsumed stream.

New `test_audit_probe_evidence.py`: **15 methods pass**, including HTTP-200 nested/flat errors, heartbeat/DONE/role-only, valid response/finish, byte-fragmented UTF-8/CRLF, invalid types/non-SSE garbage, coalesced error, JSON error, size/event bounds, total timeout, compression refusal before read and SSE channel fallback. The first 14-test pre-fix run yielded 21 failed subcases; the compression test was added with the fix. Related probe/request/header/proxy/realm suites total **126/126 pass**. The old channel-fallback fake response gained `aiter_bytes` for error bodies because production no longer uses unbounded `aread`; all assertions remain unchanged.

## Verification

| Gate | Baseline | Final |
|---|---|---|
| Frontend npm test | 128 pass | **226 pass, 0 fail, 0 skip** |
| Frontend typecheck | pass | pass |
| Frontend lint | 3 errors / 75 warnings, exit 1 | **0 errors / 79 warnings, exit 0** |
| Frontend build:export | pass, Next 16.4.3, 30 pages | pass, 30 pages |
| Frontend production npm audit | 0 known advisories | same dependency tree, 0 production advisories |
| Frontend all-dependency npm audit | not initially completed | 8 moderate dependency-chain entries from one dev advisory, below |
| Account binding related Python tests | baseline defect tests fail | **213/213 pass** |
| Probe related Python tests | baseline defect tests fail | **126/126 pass** |
| site/home-source lockfile audit, including dev | not initially completed | **0 known advisories** |
| site/home-source npm run check | unavailable prerequisites | FAIL: missing `public/scripts/upgrade.js` |
| site/home-source npm run build | unavailable prerequisites | FAIL: missing entire `public` source tree |
| git diff --check, owned scope | — | pass |

The first checkpoint added 27 regression cases and included 38 previously excluded existing cases: 128+38+27=193. The later authorized business fixes add 33 real-component cases, yielding the current 226; latest frontend gate logs are `frontend-final-p2-*`. Thirteen auth/UI/credits regression cases were proven failing on original code; 14 new stream-boundary cases also run in the gate. Backend suites overlap (e.g. account_proxy/realm), so their 213 and 126 totals must not be summed into unique-test claims. One attempted rerun used the repository root rather than manager as Python import root and failed module imports; the corrected manager-cwd run completed all 213 successfully. Preserve the separate logs rather than hiding the command mistake.

Final whole-export stats after the business fixes: 258 files, 5,595,234 bytes; 94 JS files, 3,379,458 raw bytes, 1,068,261 sum of independently gzip-compressed JS bytes; 4 CSS files, 143,290 bytes. Next reports 30/30 generated static pages and lists 28 app routes. The earlier export statistics remain in their checkpoint JSON. This is whole export, not per-route first load. Public demo synthetic identity marker is absent from production JS. Export counts can change with cleanup of stale generated chunks; no byte-saving claim is made from differing intermediate outputs. No real browser LCP/INP/CLS, route waterfall, model TTFT, token/sec, WAN p95/p99 or maximum production capacity was measured. The faster second build is cache/environment affected and is not application-speed evidence.

## SSE microbenchmark

Same Node v24.22.1 setup; valid newline-terminated single data lines; 256-character fragments; 5 measurements per size. JSON and script retained.

| Valid data line | Baseline median ms | Fixed median ms |
|---:|---:|---:|
| 64 KiB | 27.239 | 1.328 |
| 128 KiB | 106.229 | 0.912 |
| 256 KiB | 465.920 | 2.268 |
| 512 KiB | 1667.111 | 4.525 |

512 KiB is approximately 368× faster in this local parser microbenchmark, consistent with eliminating repeated scanning. This does not imply 368× model throughput or end-to-end speed; short timings are noisy and other agents shared the host.

## Remaining findings and practical limits

1. **Dev-only minimatch advisory remains.** `@typescript-eslint/typescript-estree` 8.72.1 requires minimatch `^10.2.2`, resolves 10.2.6. Official GHSA-g3hv-75wg-m249 / CVE-2026-118764 affects >=7.0.0,<11.0.1; the identified patch is 11.0.1. npm suggests eslint-config-next 15.5.29, a downgrade against Next 16.4.3. No compatible 10.x patch was identified; no forced cross-major override/downgrade applied. Actual installed caller `dist/useProgramFromProjectService.js:244` passes `{dot:true}`, without `optimizationLevel>=2`; project config has no allowDefaultProject/untrusted runtime glob API. Official trigger is attacker-supplied patterns at optimizationLevel>=2. Current demonstrated exposure is developer/CI tooling, not a reachable production request. Primary sources inspected: https://github.com/advisories/GHSA-g3hv-75wg-m249 and https://github.com/isaacs/minimatch/releases/tag/v11.0.1 .
2. **Credential permissions require deployment policy.** `tencent._atomic_write_json` line 363 creates new credential files with 0644; verified in a temporary directory. Cross-UID read is possible if parent directories are traversable. Source intentionally supports root Manager→uid10001 upstream, so blindly switching to 0600 would break the deployment. A shared group/ACL plus directory traversal policy is needed; no unsupported cross-UID change made.
3. **Realm log pagination/counts remain incorrect at scale.** `accounts.task_logs` lines 1784–1831 applies realm filtering after DB pagination and recomputes totals/stats from only 2,000 candidates. A page may be short/empty while matching records exist later, and totals truncate. `checkin_logs` lines 1598–1640 similarly counts a filtered candidate window. `_realm_uid_filter` reads only the default auth directory, so grouped accounts absent there are lost from realm-filtered historical views. Source-confirmed and separately recorded, not fixed this round. The safe follow-up is persist/resolve realm in the query before pagination, with an explicit historical-unknown policy.
4. **Generic reads discard stale results but often do not cancel their requests.** useAsyncAll coalesces background refresh and fences state, but fetchers have no AbortSignal; many list APIs do not expose cancellation. Old group/filter reads still consume network/server capacity and arbitrary uncanceled old 401s can still reach the global interceptor. Correct propagation should be tested as a coherent transport change, not mechanical signature edits.
5. **Polling/large-tenant behavior needs representative browser measurements.** Heartbeat suspends hidden tabs and per-second clocks share subscribers. Portal QR polling is expiry-bounded but not visibility-paused. Dashboard group aggregation can fan out by group count without a client cap. Accounts already page display at 20 rows, but load/filter the full account array; keys and large settings sections still render substantial content. No speculative gain is assigned to virtualization.
6. **Frontend decomposition and existing warnings.** Accounts is 1,759 lines, keys 1,419, settings layout 2,356. Fetch, mutation and form/render state share these modules, increasing the likelihood of ownership races. 79 lint warnings remain: the prior 75 plus four `react-hooks/set-state-in-effect` warnings for the new identity/tab-bound state reset paths in Keys, Security, Settings and PgSync. Those resets clear old credentials/config state before new reads; eliminating the warnings cleanly requires keyed child scopes or a coordinated state redesign. No suppression or timer/microtask workaround was introduced.
7. **Single-worker/background-job architecture is an explicit constraint.** WorkBuddy authorization caches and renewal scheduler ownership are process-local. main starts background jobs only with BACKGROUND_JOBS; transition deployment already disables that flag. Multiple primary workers all enabled could perform sequential duplicate forced renewal despite per-file leases (which protect simultaneous credential writes). Do not present the existing transition job flag as absent.
8. **Homepage source is incomplete in this checkout.** Independent package-lock audit (dev included) returns 0. check and build were run, both blocked by absent public resources. npm ci cannot reconstruct those assets; no assets were fabricated or tracked generated directory modified. Historical exports/vendor assets remain inventoried rather than exhaustively reconstructed/browser-tested. Site login HTML preservation contract was respected.

Positive backend boundaries reviewed: platformauth binds owner authorization generation, platform/realm and engine generation, validates callback hosts/paths/state/PKCE and retains uncertain completions; credentialrefresh uses per-loop/path/UID single-flight plus file lease and token-generation compare-and-swap; credentiallease refuses symlink/nonregular/multiple-link lock files and has a finite acquisition deadline. These are observed controls, not a blanket guarantee that the modules are vulnerability-free.

## Evidence files

All paths below are under `/workspace/scratch/913dc6e1a7c0/`:

- `frontend-baseline-{test,typecheck,lint,build}.log`, `frontend-final-{test,typecheck,lint,build}.log`
- `frontend-auth-{before,after}.log`, `frontend-ui-race-{before,after}.log`, `frontend-credits-{before,after}.log`
- `frontend-stream-fix-tests.log`, `frontend-expanded-tests.log`
- `frontend-sse-{before,after}.json`, `frontend-sse-benchmark.mjs`
- `frontend-dependency-audit.json`, `frontend-dependency-audit-all.json`, `frontend-final-export-stats.json`
- `account-scope-{before,after,regression,final,complete,complete-corrected}.log`
- `probe-evidence-{before,after,final}.log`
- `credential-residual-proof.json` (pre-probe-fix false-health demonstration and unchanged 0644 mode)
- `site-dependency-audit.json`, `site-dependency-audit.stderr`, `site-check.log`, `site-build.log`
- `frontend-reviewed-files.txt`, `frontend-coverage.tsv`, `frontend-coverage-counts.json`, `frontend-tracked-files.txt`

- Latest additional evidence: `frontend-final-p2-{tests,typecheck,lint,build,components}.log`, `frontend-final-p2-lint.json`, `frontend-final-p2-export-stats.json`, `frontend-p2-fixed-files.tsv`, `frontend-business-extra-{reviewed.tsv,notes.md}`, `frontend-plan-claim-notes.md`.

After frontend freeze, the parent full Manager run exposed a stale Python source-shape contract for Security save. `manager/server/tests/test_async_state.py` was reproduced at 16 pass / 1 fail, then its fixed 900-character slice and old `if(await reload())` regex were updated to require the actual stronger `await reload() && current()` clearance. No business source changed. The isolated manager-assets single-file rerun passed 17/17 plus 11 subtests (run `20261105-211752-412f80c2`, view unchanged, no non-loopback/provider requests). Logs: `frontend-async-state-before.log`, `frontend-async-state-after.log`. This additional test file is targeted coverage, not a full-file manual-read claim.

A second source-contract check accurately reproduced three PgSync test failures caused by the old setInterval/pollStatus/empty-deps shapes. Only `PgSyncPanelPollingTest` in `manager/server/tests/test_pgsync_review.py` changed: it now requires the stronger completion-timer, in-flight, AbortSignal, identity/generation and cleanup contract while retaining dirty-form protection; four mutations are rejected. Final isolated combination with test_async_state passed 31/31 plus 15 subtests, 0 skip (run `20261105-212347-8aaec8cf`). Business source/out stayed frozen. Logs: `frontend-pgsync-contract-before.log`, `frontend-contracts-after.log`; coverage is targeted.
