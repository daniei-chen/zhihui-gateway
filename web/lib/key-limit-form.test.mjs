import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {parseKeyLimit} from './key-limit-form.ts';
import {accountReloadNotices} from './reload-state.ts';

test('invalid key limits cannot turn into unlimited in the browser', () => {
  for (const value of ['', ' ', 'NaN', 'Infinity', '-1', 'abc', '1e100']) {
    assert.throws(() => parseKeyLimit(value));
    assert.throws(() => parseKeyLimit(value, true));
  }
  for (const value of ['1.5', '9000000000000001']) assert.throws(() => parseKeyLimit(value));
  for (const value of ['0.0000001', '0.1234567']) assert.throws(() => parseKeyLimit(value, true));
  assert.throws(() => parseKeyLimit('1000001', false, true));
  assert.equal(parseKeyLimit('0'), 0);
  assert.equal(parseKeyLimit('10000'), 10000);
  assert.equal(parseKeyLimit('0.25', true), .25);
  assert.equal(parseKeyLimit('0.000001', true), .000001);
});

test('invalid stored policies are visibly blocked and need explicit repair confirmation', () => {
  const source = fs.readFileSync(new URL('../app/(main)/keys/page.tsx', import.meta.url), 'utf8');
  assert.match(source, /request_policy_valid === false/);
  assert.match(source, /!repairConfirmed/);
  assert.match(source, /keys\.policyRepairWarning/);
  assert.match(source, /keys\.badgePolicyInvalid/);
  assert.doesNotMatch(source, /Number\(form\.(?:quota|quotaCredit|maxIps|dailyTokens|dailyCredit)\) \|\| 0/);
});

test('account hot-load uncertainty is not silently displayed as confirmed', () => {
  const row = (phase) => ({upstream_id: null, phase, reason: '', pending_count: 1,
    observed_at: 1, restart_scheduled: false});
  const state = {account_reload_scopes: [row('confirmed'), row('needs_attention'), row('observing')]};
  assert.deepEqual(accountReloadNotices(state).map(item => item.phase), ['needs_attention', 'observing']);
  assert.deepEqual(accountReloadNotices(null), []);
  assert.equal(state.account_reload_scopes.length, 3);
});

test('all locales explain policy repair and non-disruptive account observation', () => {
  for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
    const data = JSON.parse(fs.readFileSync(new URL(`./i18n/locales/${locale}.json`, import.meta.url), 'utf8'));
    for (const key of ['requestPolicyInvalid', 'badgePolicyInvalid', 'policyRepairWarning', 'policyRepairConfirm', 'invalidLimit']) {
      assert.ok(data.keys[key]?.length > 2, `${locale}:keys.${key}`);
    }
    for (const key of ['accountReloadObserving', 'accountReloadAttention', 'accountReloadNoRestart']) {
      assert.ok(data.settings[key]?.length > 2, `${locale}:settings.${key}`);
    }
  }
});
