from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from deploy.cohort_rollout import Contract, InferenceRollout, Refused, compatible


def contract(name):
    return Contract(name, ('a' if name == 'old' else 'b') * 64, 'same-host-inode',
                    *(['c' * 64] * 5), background_owner='stable-control-plane')


class Operations:
    def __init__(self):
        self.active = 'old'
        self.moves = []
        self.retired = []
        self.drain_ok = True
        self.fail_after_cut = False
        self.fail_reload = False
        self.verified = True
    async def verify_artifacts(self, old, new): return self.verified
    async def active_generation(self): return self.active
    async def healthy(self, target):
        return not (self.fail_after_cut and target.generation == 'new' and self.active == 'new')
    async def route(self, target):
        if self.fail_reload: raise Refused('nginx_config_invalid')
        self.moves.append(target.generation)
        self.active = target.generation
    async def drained(self, target, timeout): return self.drain_ok
    async def retire(self, target): self.retired.append(target.generation)


class CohortContractTest(unittest.TestCase):
    def test_current_nonparticipating_release_cannot_be_silently_overlapped(self):
        with self.assertRaisesRegex(Refused, 'bootstrap_required'):
            compatible(replace(contract('old'), shared_protocol=0), contract('new'))

    def test_engine_schema_limits_credentials_and_jobs_are_separate_changes(self):
        for field in ('coordinator_id', 'admission_policy_sha256', 'schema_sha256',
                      'engine_sha256', 'engine_config_sha256', 'authority_sha256', 'background_owner'):
            value = 'd' * 64 if field.endswith('sha256') else 'another'
            with self.subTest(field=field), self.assertRaisesRegex(Refused, 'incompatible_'):
                compatible(contract('old'), replace(contract('new'), **{field: value}))
        with self.assertRaisesRegex(Refused, 'handoff_not_admitted'):
            compatible(contract('old'), replace(contract('new'), background_jobs=True))


class CohortControllerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = Path(self.tmp.name) / 'journal.json'
        self.ops = Operations()
        self.rollout = InferenceRollout(contract('old'), contract('new'), self.ops, self.journal)

    async def test_route_observe_drain_retire(self):
        result = await self.rollout.run(observe_seconds=0, drain_seconds=1)
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(self.ops.moves, ['new'])
        self.assertEqual(self.ops.retired, ['old'])

    async def test_accepted_new_streams_survive_health_rollback(self):
        self.ops.fail_after_cut = True
        with self.assertRaisesRegex(Refused, 'health_failed'):
            await self.rollout.run(observe_seconds=0, drain_seconds=1)
        self.assertEqual(self.ops.moves, ['new', 'old'])
        self.assertEqual(self.ops.retired, [])

    async def test_long_old_stream_defers_retirement_without_forcing(self):
        self.ops.drain_ok = False
        result = await self.rollout.run(observe_seconds=0, drain_seconds=1)
        self.assertEqual(result['phase'], 'retirement_deferred')
        self.assertEqual(self.ops.active, 'new')
        self.assertEqual(self.ops.retired, [])

    async def test_invalid_nginx_config_keeps_old_route(self):
        self.ops.fail_reload = True
        with self.assertRaisesRegex(Refused, 'config_invalid'):
            await self.rollout.run(observe_seconds=0, drain_seconds=1)
        self.assertEqual(self.ops.active, 'old')
        self.assertEqual(self.ops.retired, [])

    async def test_missing_trust_evidence_refuses_before_any_route(self):
        self.ops.verified = False
        with self.assertRaisesRegex(Refused, 'verification_failed'):
            await self.rollout.run()
        self.assertEqual(self.ops.moves, [])

    async def test_interrupted_journal_is_not_blindly_replayed(self):
        self.rollout.record('routed')
        with self.assertRaisesRegex(Refused, 'requires_inspection'):
            InferenceRollout(contract('old'), contract('new'), self.ops, self.journal)

    async def test_unconfirmed_retirement_cannot_roll_back_to_a_stopped_process(self):
        async def failed_retire(target):
            self.ops.retired.append(target.generation)
            raise RuntimeError('exit acknowledgement missing')
        self.ops.retire = failed_retire
        with self.assertRaisesRegex(Refused, 'retirement_requires_inspection'):
            await self.rollout.run(observe_seconds=0, drain_seconds=1)
        self.assertEqual(self.ops.active, 'new')
        self.assertEqual(self.ops.moves, ['new'])
