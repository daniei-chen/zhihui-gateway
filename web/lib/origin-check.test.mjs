/**
 * D-41 前端守卫：`lib/origin-check.ts` 的三个纯函数。
 *
 * 跑法：`npm test`（node --test lib/*.test.mjs），或
 * `node --experimental-strip-types lib/origin-check.test.mjs`。
 *
 * 为什么值得单独测：这三条判据错了**界面不会报错**——
 *   · `needsOriginNotice` 反过来（`writes_allowed !== true`）→ 旧后端下所有页面
 *     都弹一条假的「表单会被拒」；
 *   · 只有点分域名该提示，IP / localhost / 反代部署不该提示 —— 判据必须来自
 *     服务端字段，前端 `location` 猜不准；
 *   · `originBlockedReason` 漏了响应头 → 中间层只保留头时用户又看到通用 403。
 */
import {needsOriginNotice, originBlockedReason, originStateKey} from './origin-check.ts';

let failed = 0;
let passed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    passed += 1;
    return;
  }
  failed += 1;
  console.error(`FAIL ${name}: got ${g}, want ${w}`);
}

/* ── needsOriginNotice：只有「服务端说没有锚点」才提示 ── */
check('缺字段（旧后端）不提示', needsOriginNotice(undefined), false);
check('null 不提示', needsOriginNotice(null), false);
check('配置了不提示', needsOriginNotice({state: 'configured', writes_allowed: true}), false);
check('可信反代不提示', needsOriginNotice({state: 'proxy', writes_allowed: true}), false);
check('IP/localhost 直连不提示', needsOriginNotice({state: 'loopback-only', writes_allowed: true}), false);
check('逃生门不提示', needsOriginNotice({state: 'relaxed', writes_allowed: true}), false);
check('域名直连未配置 → 提示', needsOriginNotice({state: 'unconfigured', writes_allowed: false}), true);
check('只给 state=unconfigured 也提示',
  needsOriginNotice({state: 'unconfigured'}), true);

/* ── originStateKey：状态 → i18n 键；未知状态不编造 ── */
check('configured 键', originStateKey('configured'), 'origin.stateConfigured');
check('proxy 键', originStateKey('proxy'), 'origin.stateProxy');
check('loopback 键', originStateKey('loopback-only'), 'origin.stateLoopback');
check('unconfigured 键', originStateKey('unconfigured'), 'origin.stateUnconfigured');
check('relaxed 键', originStateKey('relaxed'), 'origin.stateRelaxed');
check('未知状态给空串', originStateKey('something-new'), '');
check('缺状态给空串', originStateKey(undefined), '');

/* ── originBlockedReason：body 与响应头都认 ── */
check('body detail 命中', originBlockedReason({detail: 'origin_unconfigured'}, ''), 'origin_unconfigured');
check('响应头命中', originBlockedReason(undefined, 'origin_unconfigured'), 'origin_unconfigured');
check('大小写与空白容忍', originBlockedReason({detail: ' Origin_Unconfigured '}, null), 'origin_unconfigured');
check('其他 detail 不算', originBlockedReason({detail: 'Forbidden'}, ''), '');
check('其他原因头不算', originBlockedReason({}, 'csrf_origin_mismatch'), '');
check('非对象体不算', originBlockedReason('origin_unconfigured', ''), '');
check('空输入不算', originBlockedReason(undefined, undefined), '');

console.log(`${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
