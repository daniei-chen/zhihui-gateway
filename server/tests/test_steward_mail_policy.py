"""Thirty-minute notification windows and inline branding; synthetic SMTP only."""
from email import policy as email_policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import unittest

from server import db, steward_config, steward_mail, steward_rules, steward_snapshot, steward_store
from server.tests.authorization_test_support import isolated_data
from server.tests.test_steward import FakeSMTP
from server.tests import test_steward_watchdog as watchdog_fixture


class MailWindowTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_data())
        db.connect()
        steward_store.ensure_schema()

    def signal(self, active=True, now=100, severity='warning', rule='disk', recipients=('fixture@example.com',)):
        return steward_store.observe(rule, active, severity, 'fixture', 'safe evidence',
                                     recipients=recipients, now=now)

    def send_fault(self, now=100, status='accepted'):
        self.signal(now=now)
        item = steward_store.claim('first', now=now)
        steward_store.finish(item, {'status': status}, now=now)
        return item

    def test_recurrence_escalation_recovery_and_exact_boundary(self):
        self.send_fault()
        self.signal(now=110, severity='critical')
        self.assertIsNone(steward_store.claim('mail', now=110))
        self.signal(False, now=120)
        recovery = steward_store.claim('mail', now=120)
        self.assertEqual(recovery['event'], 'recovered')
        steward_store.finish(recovery, {'status': 'accepted'}, now=120)
        self.signal(now=130)
        self.assertIsNone(steward_store.claim('mail', now=130))
        self.assertEqual(steward_store.deliveries()[0]['error_code'], 'alert_cooldown_30m')
        self.assertIsNone(steward_store.claim('mail', now=1899))
        self.signal(now=1900, severity='critical')
        self.assertEqual(steward_store.claim('mail', now=1900)['event'], 'escalated')

    def test_restart_unknown_and_clock_rollback_keep_window(self):
        self.send_fault(now=1000, status='unknown')
        self.signal(False, now=1010)
        recovery = steward_store.claim('mail', now=1010)
        steward_store.finish(recovery, {'status': 'accepted'}, now=1010)
        steward_store.ensure_schema()
        steward_store.recover_interrupted()
        self.signal(now=1020)
        self.assertIsNone(steward_store.claim('restart', now=900))
        self.assertIsNone(steward_store.claim('restart', now=1020))
        self.assertIsNone(steward_store.claim('restart', now=9000))
        self.assertEqual(steward_store.deliveries()[-1]['status'], 'unknown')

    def test_rule_and_recipient_are_separate_and_safe_retry_is_not_duplicate(self):
        item = self.send_fault(status='retry')
        self.assertIsNone(steward_store.claim('early', now=159))
        retry = steward_store.claim('retry', now=160)
        self.assertEqual(retry['id'], item['id'])
        steward_store.finish(retry, {'status': 'accepted'}, now=160)
        self.signal(now=161, recipients=('other@example.com',))
        self.signal(now=161, rule='memory')
        recipients = {steward_store.claim('mail', now=161)['recipient'] for _ in range(2)}
        self.assertEqual(recipients, {'fixture@example.com', 'other@example.com'})

    def test_recurrence_cancels_stale_recovery_before_delivery(self):
        self.send_fault()
        self.signal(False, now=110)
        self.signal(now=120)
        self.assertIsNone(steward_store.claim('mail', now=120))
        recovery = next(row for row in steward_store.deliveries() if row['event'] == 'recovered')
        self.assertEqual((recovery['status'], recovery['error_code']), ('cancelled', 'incident_recurred'))

    def test_running_recovery_analysis_cannot_complete_after_recurrence(self):
        selected = steward_config.save({'enabled': True, 'ai_analysis': True, 'recipients': ['fixture@example.com']})
        self.send_fault()
        steward_store.observe('disk', False, 'warning', 'fixture', 'safe', recipients=selected.recipients,
                              analyze=True, now=110)
        item = steward_store.claim_analysis('model')
        self.signal(now=120)
        self.assertFalse(steward_store.analysis_is_current(item))
        steward_store.finish_analysis(item, 'old all-clear', revision=selected.revision, recipients=selected.recipients)
        self.assertEqual(steward_store.analyses()[0]['status'], 'cancelled')

    def test_pending_upgrade_followup_is_cancelled_and_old_attempt_seeds_window(self):
        self.send_fault()
        # Simulate old-schema attempts with no new window table rows.
        db.execute('DELETE FROM steward_mail_windows')
        old = steward_store.deliveries()[0]
        db.execute('INSERT INTO steward_deliveries(incident_id,event,recipient,payload,next_at,created_at) '
                   'VALUES(?,?,?,?,?,?)', (old['incident_id'], 'analysis-firing', 'fixture@example.com', '{}', 100, 100))
        steward_store.ensure_schema()
        self.assertEqual(steward_store.deliveries()[0]['error_code'], 'analysis_panel_only')
        self.signal(now=200, severity='critical')
        self.assertIsNone(steward_store.claim('new', now=200))
        self.assertEqual(steward_store.deliveries()[0]['error_code'], 'alert_cooldown_30m')

    def test_interrupted_sending_fences_recurrence_without_resending_unknown(self):
        self.signal()
        original = steward_store.claim('crashed', now=100)
        steward_store.recover_interrupted()
        self.signal(now=110, severity='critical')
        self.assertIsNone(steward_store.claim('replacement', now=110))
        self.assertEqual(db.query_one('SELECT status FROM steward_deliveries WHERE id=?', (original['id'],))['status'], 'unknown')

    def test_analysis_enriches_only_unsent_rule_and_keeps_result_in_panel(self):
        selected = steward_config.save({'enabled': True, 'ai_analysis': True, 'recipients': ['fixture@example.com']})
        steward_store.observe('disk', True, 'warning', 'fixture', 'safe', recipients=selected.recipients, analyze=True)
        analysis = steward_store.claim_analysis('model')
        steward_store.finish_analysis(analysis, '建议检查 password=private-value', revision=selected.revision,
                                      recipients=selected.recipients)
        self.assertEqual(len(steward_store.deliveries()), 1)
        item = steward_store.claim('mail')
        payload = steward_store.delivery_payload(item, selected)
        self.assertIn('建议检查', payload['analysis'])
        self.assertNotIn('private-value', payload['analysis'])
        self.assertNotIn('private-value', steward_store.analyses()[0]['answer'])
        changed = steward_config.save({**selected.model_dump(), 'ai_analysis': False})
        self.assertNotIn('analysis', steward_store.delivery_payload(item, changed))
        self.assertEqual(steward_store.analyses()[0]['answer'], '')

    def test_analysis_columns_migrate_additively_without_losing_old_record(self):
        db.execute('DROP TABLE steward_analysis')
        db.execute("CREATE TABLE steward_analysis(id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL,event TEXT NOT NULL,"
                   "payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at INTEGER NOT NULL,"
                   "finished_at INTEGER,owner TEXT NOT NULL DEFAULT '',error_code TEXT NOT NULL DEFAULT '',"
                   "UNIQUE(incident_id,event))")
        db.execute("INSERT INTO steward_analysis(id,incident_id,event,payload,created_at) VALUES(1,7,'firing','{}',100)")
        steward_store.ensure_schema()
        steward_store.ensure_schema()
        row = db.query_one('SELECT * FROM steward_analysis WHERE id=1')
        self.assertEqual((row['incident_id'], row['answer'], row['policy_revision']), (7, '', None))

    def test_claimed_recovery_and_fault_are_rechecked_before_dispatch(self):
        self.send_fault()
        self.signal(False, now=110)
        recovery = steward_store.claim('mail', now=110)
        self.signal(now=120)
        self.assertFalse(steward_store.analysis_delivery_is_current(recovery, steward_config.Policy()))
        # A newly-added recipient may have a queued fault, but not after recovery.
        self.signal(now=130, recipients=('other@example.com',))
        fault = steward_store.claim('mail', now=130)
        self.signal(False, now=140)
        self.assertFalse(steward_store.analysis_delivery_is_current(fault, steward_config.Policy()))

    def test_turning_recovery_off_cancels_previously_claimed_all_clear(self):
        self.send_fault()
        self.signal(False, now=110)
        recovery=steward_store.claim('mail',now=110)
        self.assertFalse(steward_store.analysis_delivery_is_current(recovery,
            steward_config.Policy(send_recovery=False)))


class RecipientPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_data())
        db.connect()
        steward_store.ensure_schema()

    def test_append_preserves_all_addresses_and_thresholds_atomic_revision(self):
        first = steward_config.save({'enabled': True, 'recipients': ['first@example.com', 'second@example.com'],
                                     'disk_warning_percent': 85})
        second = steward_config.change_recipient(first.revision, 'Third@Example.COM')
        self.assertEqual(second.recipients, ['first@example.com', 'second@example.com', 'Third@example.com'])
        self.assertEqual(second.disk_warning_percent, 85)
        self.assertEqual(second.revision, first.revision + 1)
        self.assertEqual(steward_config.policy(), second)
        with self.assertRaises(ValueError):
            steward_config.change_recipient(first.revision, 'stale@example.com')
        with self.assertRaises(ValueError):
            steward_config.change_recipient(second.revision, 'THIRD@example.com')
        self.assertEqual(steward_config.policy(), second)

    def test_remove_only_selected_and_refuses_empty_active_policy(self):
        first = steward_config.save({'enabled': True, 'recipients': ['first@example.com', 'second@example.com']})
        second = steward_config.change_recipient(first.revision, 'FIRST@example.com', remove=True)
        self.assertEqual(second.recipients, ['second@example.com'])
        with self.assertRaises(ValueError):
            steward_config.change_recipient(second.revision, 'second@example.com', remove=True)
        self.assertEqual(steward_config.policy(), second)

    def test_corrupt_policy_cannot_reset_recipients_to_empty_defaults(self):
        db.execute('INSERT INTO settings VALUES(?,?)', (steward_config.SETTING, '{broken'))
        with self.assertRaises(ValueError):
            steward_config.policy()
        with self.assertRaises(ValueError):
            steward_config.save({})
        self.assertEqual(db.query_one('SELECT value FROM settings WHERE key=?',
                                      (steward_config.SETTING,))['value'], '{broken')

    def test_classified_quality_signals_exclude_admin_and_unknown_is_not_recovery(self):
        from unittest.mock import patch
        from server import budget, observability
        from server.tests.test_steward import snapshot
        budget.initialize()
        self.assertTrue(observability.ensure_outcome_schema())
        current = snapshot()
        current['traffic'].update(channel_rejections=5, upstream_auth_failures=3, pool_unavailable=3)
        active = {s.rule for s in steward_rules.evaluate(current, steward_config.Policy()) if s.active}
        self.assertEqual(active, {'channel_verification', 'upstream_authorization', 'pool_capacity'})
        current['traffic'].pop('channel_rejections')
        signals = {s.rule: s for s in steward_rules.evaluate(current, steward_config.Policy())}
        self.assertIsNone(signals['channel_verification'].active)
        now = 1700000000
        for protocol, code, source, status in [('http', 'channel_check_rejected', 'upstream', 400),
                ('openai', 'channel_check_rejected', 'upstream', 400),
                ('openai', 'gateway.auth.denied', 'gateway', 401),
                ('openai', 'session_dead', 'upstream', 401),
                ('openai', 'no_healthy_account', 'upstream', 503)]:
            db.execute('INSERT INTO request_outcomes(ts,request_id,endpoint,method,protocol,http_status,terminal_state,'
                       'error_code,error_source) VALUES(?,?,?,?,?,?,?,?,?)',
                       (now, 'synthetic-' + protocol + code, '/v1/fixture', 'POST', protocol, status,
                        'upstream_error', code, source))
        with patch('server.ledgerreplay.channel_state', return_value={'state': 'healthy'}):
            facts = steward_snapshot.local_facts(now)['traffic']
        self.assertEqual([facts[k] for k in ('channel_rejections', 'upstream_auth_failures', 'pool_unavailable')], [1, 1, 1])


class WatchdogWindowTests(unittest.TestCase):
    setUp = watchdog_fixture.WatchdogTests.setUp
    run_at = watchdog_fixture.WatchdogTests.run_at
    state = watchdog_fixture.WatchdogTests.state
    save = watchdog_fixture.WatchdogTests.save

    def test_boundary_legacy_state_restart_and_recovery_exemption(self):
        self.run_at(100)
        self.run_at(160)
        self.ready = True
        self.run_at(200)
        self.run_at(260)
        self.ready = False
        self.run_at(1899)
        self.run_at(1960)  # Exactly thirty minutes after the first attempt.
        self.assertEqual(self.sender.call_count, 3)
        self.assertEqual(self.state()['outbox'][-1]['attempted_at'], 1960)
        self.ready = True
        self.run_at(2020)
        self.run_at(2080)
        self.assertEqual(self.sender.call_count, 4)
        state = self.state()
        for item in state['outbox']:
            item.pop('attempted_at', None)  # Accepted legacy schema1 still readable.
        self.save(state)
        self.ready = False
        self.run_at(2140)
        self.run_at(2200)
        self.assertEqual(self.sender.call_count, 4)
        self.assertEqual(self.state()['outbox'][-1]['code'], 'alert_cooldown_30m')

    def test_unknown_firing_blocks_new_generation_but_not_recovery(self):
        self.sender.return_value = {'status': 'unknown', 'code': 'smtp_acceptance_unknown'}
        self.run_at(100)
        self.run_at(160)
        self.sender.return_value = {'status': 'accepted'}
        self.ready = True
        self.run_at(220)
        self.run_at(280)
        self.ready = False
        self.run_at(340)
        self.run_at(400)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.state()['outbox'][0]['status'], 'unknown')

    def test_recipient_removed_between_batch_sends_is_rechecked(self):
        self.selected.recipients.append('other@example.com')
        def sent(*args, **kwargs):
            self.selected.recipients = ['fixture@example.com']
            return {'status': 'accepted'}
        self.sender.side_effect = sent
        self.run_at(100)
        self.run_at(160)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.state()['outbox'][1]['status'], 'cancelled')


class BrandMailTests(unittest.TestCase):
    def test_confirmed_brand_asset_matches_console_and_remains_small(self):
        logo = steward_mail.brand_logo()
        root = Path(__file__).resolve().parents[2]
        web = root / 'web'
        if not web.exists():
            web = root.parent / 'src/three-platform/web'
        public = web / 'public/favicon/android-chrome-192x192.png'
        self.assertEqual(hashlib.sha256(logo).hexdigest(), hashlib.sha256(public.read_bytes()).hexdigest())
        self.assertLess(len(logo), 40000)

    def test_maximum_unicode_mail_fits_bounded_smtp_worker(self):
        smtp = FakeSMTP()
        event = {'event': 'firing', 'title': '提醒' * 150, 'detail': '状态' * 500,
                 'analysis': '辅助分析，不是根因。' * 600, 'observed_at': 1700000000, 'severity': 'critical'}
        result = steward_mail.send({'host': 'smtp.qq.com', 'port': 465, 'username': 'fixture@example.com', 'password': 'synthetic'},
                                   'target@example.com', event, delivery_id=1, smtp_factory=lambda *a, **k: smtp)
        self.assertEqual(result['status'], 'accepted')
        self.assertLessEqual(len(smtp.raw), 131072)
        mail = BytesParser(policy=email_policy.default).parsebytes(smtp.raw)
        self.assertEqual(mail['From'].addresses[0].display_name, '小汇')
        self.assertEqual(len([part for part in mail.walk() if part.get_content_type() == 'image/png']), 1)


if __name__ == '__main__':
    unittest.main()
