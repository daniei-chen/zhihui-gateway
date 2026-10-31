"""Canonical sk-hex credentials; no external calls or live data."""
import hashlib
import re
import secrets
import unittest
from unittest.mock import patch

from server import db, keysvc, notify, steward_config
from server.tests.authorization_test_support import isolated_data


class KeyFormatTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_data())
        db.connect()

    def test_new_credentials_exact_shape_entropy_and_no_plaintext_storage(self):
        with patch.object(keysvc.secrets, 'token_hex', wraps=secrets.token_hex) as random:
            keys = [keysvc.create_key('synthetic-format') for _ in range(32)]
        self.assertEqual(random.call_count, 32)
        random.assert_called_with(16)
        self.assertEqual(len({item['key'] for item in keys}), 32)
        for item in keys:
            self.assertRegex(item['key'], r'^sk-[0-9a-f]{32}$')
            row = db.query_one('SELECT * FROM api_keys WHERE id=?', (item['id'],))
            self.assertEqual(row['key_hash'], hashlib.sha256(item['key'].encode()).hexdigest())
            self.assertNotIn(item['key'], str(dict(row)))
            self.assertEqual(keysvc.resolve(item['key'])['id'], item['id'])

    def test_in_place_rotation_keeps_id_policies_counters_and_revokes_prior_token(self):
        created = keysvc.create_key('synthetic', realm='cn', models=['fixture'], platforms=['qoder'],
                                    quota=1000, quota_credit=10, daily_token_quota=500,
                                    daily_credit_quota=5, assigned_to='fixture-user')
        db.execute('UPDATE api_keys SET used_tokens=123,used_credit=2.5,used_credit_units=2500000 WHERE id=?',
                   (created['id'],))
        before = dict(db.query_one('SELECT * FROM api_keys WHERE id=?', (created['id'],)))
        replacement = 'sk-' + secrets.token_hex(16)
        db.execute('UPDATE api_keys SET key_hash=?,prefix=? WHERE id=?',
                   (keysvc._hash(replacement), replacement[:12], created['id']))
        after = keysvc.resolve(replacement)
        self.assertEqual(after['id'], created['id'])
        self.assertIsNone(keysvc.resolve(created['key']))
        for name in ('quota', 'used_tokens', 'quota_credit', 'used_credit', 'realm', 'assigned_to',
                     'daily_token_quota', 'daily_credit_quota', 'created_at'):
            self.assertEqual(after[name], before[name])
        self.assertIsNotNone(keysvc.validate(after, '127.0.0.1', 'fixture', platform='workbuddy'))
        self.assertIsNone(keysvc.validate(after, '127.0.0.1', 'fixture', platform='qoder'))
        self.assertEqual(db.query_one('SELECT COUNT(*) n FROM legacy_qtg_keys')['n'], 0)

    def test_old_wbk_keys_remain_supported_until_explicit_rotation(self):
        created = keysvc.create_key('synthetic-legacy')
        legacy = 'wbk_' + secrets.token_urlsafe(32)
        db.execute('UPDATE api_keys SET key_hash=?,prefix=? WHERE id=?',
                   (keysvc._hash(legacy), legacy[:12], created['id']))
        self.assertEqual(keysvc.resolve(legacy)['id'], created['id'])
        self.assertIsNone(keysvc.resolve(created['key']))

    def test_no_arbitrary_prefix_case_padding_or_partial_match(self):
        created = keysvc.create_key('synthetic-shape')
        token = created['key']
        for bad in (token.upper(), token + '0', token[:-1], ' ' + token, token + '\n',
                    token.replace('sk-', 'sk-qtg-'), 'sk-' + secrets.token_hex(16), None, 123):
            with self.subTest(shape=type(bad).__name__):
                self.assertIsNone(keysvc.resolve(bad))

    def test_steward_reads_new_or_legacy_keys_and_rejects_noncanonical_shape(self):
        for token in ('sk-' + secrets.token_hex(16), 'wbk_' + secrets.token_urlsafe(32)):
            with patch.object(steward_config, 'secret_file', return_value=token + '\n'):
                self.assertEqual(steward_config.model_key(), token)
            self.assertNotIn(token, notify.redact('api_key=' + token))
        for token in ('sk-not-hex', 'sk-' + 'g' * 32, 'wbt_' + '0' * 43):
            with patch.object(steward_config, 'secret_file', return_value=token), self.assertRaises(ValueError):
                steward_config.model_key()


if __name__ == '__main__':
    unittest.main()
