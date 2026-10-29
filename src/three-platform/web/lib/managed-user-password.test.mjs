import {test} from 'node:test';
import assert from 'node:assert/strict';
import {
  MANAGED_USER_MIN_PASSWORD_LENGTH,
  ROOT_USER_MIN_PASSWORD_LENGTH,
  isManagedUserPassword,
  canResetUserPassword,
} from './managed-user-password.ts';

test('managed user passwords accept at least eight unmodified Unicode code points', () => {
  assert.equal(MANAGED_USER_MIN_PASSWORD_LENGTH, 8);
  for (const value of ['01234567', '123456789', '1234567890', '12345678901', 'long-letters-password',
    'mixed123', '\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b', '\u{1f600}'.repeat(8), '       x']) {
    assert.equal(isManagedUserPassword(value), true);
  }
  for (const value of ['', '1234567', 'letters', '\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03',
    '\u{1f600}'.repeat(7), '\u{1f600}'.repeat(4), 1234567890, null, undefined]) {
    assert.equal(isManagedUserPassword(value), false, `Accepted ${JSON.stringify(value)}`);
  }
});

test('only the protected admin account retains the existing minimum-length reset rule', () => {
  assert.equal(ROOT_USER_MIN_PASSWORD_LENGTH, 12);
  assert.equal(canResetUserPassword('admin', '0123456789'), false);
  assert.equal(canResetUserPassword('admin', 'Root-Password!'), true);
  for (const username of ['alice', 'other-super-admin']) {
    assert.equal(canResetUserPassword(username, '01234567'), true);
    assert.equal(canResetUserPassword(username, 'Root-Password!'), true);
    assert.equal(canResetUserPassword(username, '12345678901'), true);
    assert.equal(canResetUserPassword(username, '\u{1f600}'.repeat(7)), false);
  }
});
