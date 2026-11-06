from unittest.mock import patch
import unittest

from server import config, main, operational


class TransitionOwnershipTest(unittest.TestCase):
    def test_shared_worker_never_claims_primary_serving_role(self):
        from server import instancelease
        with patch.object(config, 'SHARED_ADMISSION', True), patch.object(config, 'BACKGROUND_JOBS', False):
            self.assertEqual(instancelease.serving_role(), 'candidate')
        with patch.object(config, 'SHARED_ADMISSION', True), patch.object(config, 'BACKGROUND_JOBS', True):
            self.assertEqual(instancelease.serving_role(), 'serving')
    def test_transition_does_not_recover_budgets_replay_or_migrate_users(self):
        source = {'role_policy_version': 2, 'users': [
            {'username': 'admin', 'role': 'admin', 'sv': 1}]}
        with patch.object(config, 'BACKGROUND_JOBS', False), \
             patch('server.security.load_users', return_value=source), \
             patch('server.security.migrate_user_roles') as users, \
             patch('server.ledgerreplay.recover') as receipts, \
             patch('server.budget.recover') as budgets:
            main._recover_primary_state()
        users.assert_not_called()
        receipts.assert_not_called()
        budgets.assert_not_called()

    def test_transition_rejects_user_role_migration_instead_of_writing(self):
        with patch.object(config, 'BACKGROUND_JOBS', False), \
             patch('server.security.load_users', return_value={'users': [{'username': 'admin'}]}), \
             patch('server.security.save_users') as save:
            with self.assertRaisesRegex(RuntimeError, 'primary_role_migration'):
                main._recover_primary_state()
        save.assert_not_called()

    def test_primary_keeps_existing_recovery_responsibilities(self):
        with patch.object(config, 'BACKGROUND_JOBS', True), \
             patch('server.security.migrate_user_roles') as users, \
             patch('server.ledgerreplay.recover') as receipts, \
             patch('server.budget.recover') as budgets:
            main._recover_primary_state()
        users.assert_called_once()
        receipts.assert_called_once()
        budgets.assert_called_once()

    def test_transition_monitor_cannot_notify_or_run_shared_maintenance(self):
        with patch.object(config, 'BACKGROUND_JOBS', False), \
             patch.object(operational, 'readiness', return_value={'ok': True}), \
             patch.object(operational, 'transition') as notify, \
             patch.object(operational, 'replay_ledger_receipts') as replay, \
             patch.object(operational, 'reconcile_zero_reservation_unknowns') as reconcile:
            result = operational._monitor_tick(100, 0, 0, 0)
        self.assertEqual(result[-1], {'ok': True})
        notify.assert_not_called()
        replay.assert_not_called()
        reconcile.assert_not_called()
