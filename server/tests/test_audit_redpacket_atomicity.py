"""A failed batch revocation must not silently leave callable gift keys."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from server import config, db, keysvc, redpacket


class RedPacketRevocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(config, 'DB_PATH', Path(self.tmp.name) / 'packet.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        previous = db._conn
        db._conn = None
        self.addCleanup(setattr, db, '_conn', previous)
        conn = db.connect()
        self.addCleanup(conn.close)
        self.packet = redpacket.create_packet('audit', 'credit', 3, 3, 'even', 1, 'fixture')

    def test_storage_failure_rolls_back_every_key_and_claim_marker(self):
        second_id = int(self.packet['keys'][1]['id'])
        # Fail a real SQLite DELETE after an earlier key was already processed.
        db.execute(f'CREATE TRIGGER audit_failure BEFORE DELETE ON api_keys '
                   f"WHEN OLD.id={second_id} BEGIN SELECT RAISE(ABORT,'synthetic I/O boundary'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            redpacket.revoke_packet(self.packet['id'])
        for key in self.packet['keys']:
            self.assertIsNotNone(keysvc.resolve(key['key']))
        shares = db.query('SELECT claimed_by_ip,token FROM red_packet_shares WHERE packet_id=?',
                          (self.packet['id'],))
        self.assertTrue(all(row['claimed_by_ip'] is None and row['token'] for row in shares))

    def test_revoke_counts_only_existing_keys_and_remains_idempotent(self):
        keysvc.delete_key(self.packet['keys'][0]['id'])
        self.assertEqual(redpacket.revoke_packet(self.packet['id']), 2)
        self.assertEqual(redpacket.revoke_packet(self.packet['id']), 0)
        for key in self.packet['keys']:
            self.assertIsNone(keysvc.resolve(key['key']))

    def test_nonfinite_amounts_are_rejected_before_allocation(self):
        for amount in (float('nan'), float('inf'), float('-inf'), 10**1000):
            with self.subTest(amount=str(amount)[:30]):
                with self.assertRaises(redpacket.RedPacketError):
                    redpacket.validate(amount, 1, 'credit', 'even', 1)
