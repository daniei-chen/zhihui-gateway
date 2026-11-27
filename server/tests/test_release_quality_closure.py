"""Immutable source/artifact identity and stable-channel failure contracts."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

from tools import ci_artifacts, promote_images, release_policy

ROOT = Path(__file__).resolve().parents[2]
SHA = 'a' * 40


class StaticArtifactTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'out'
        for name, content in {'index.html': '<html>home</html>', 'login/index.html': '<html>login</html>',
                              '_next/static/app.js': 'const app = true'}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        ci_artifacts.create(self.root, SHA)

    def test_exact_static_export_verifies_and_wrong_source_fails(self):
        self.assertEqual(ci_artifacts.verify(self.root, SHA)['source_commit'], SHA)
        with self.assertRaisesRegex(ValueError, 'source commit'):
            ci_artifacts.verify(self.root, 'b' * 40)

    def test_changed_missing_or_extra_bytes_are_rejected(self):
        for operation in ('changed', 'missing', 'extra'):
            with self.subTest(operation=operation):
                path = self.root / '_next/static/app.js'
                path.write_text('const app = true')
                extra = self.root / 'extra.html'
                extra.unlink(missing_ok=True)
                ci_artifacts.create(self.root, SHA)
                if operation == 'changed':
                    path.write_text('const app = false')
                elif operation == 'missing':
                    path.unlink()
                else:
                    extra.write_text('unreviewed')
                with self.assertRaises(ValueError):
                    ci_artifacts.verify(self.root, SHA)

    def test_missing_pages_and_symlinks_cannot_be_attested(self):
        (self.root / 'login/index.html').write_text('')
        with self.assertRaisesRegex(ValueError, 'page missing or empty'):
            ci_artifacts.create(self.root, SHA)
        (self.root / 'login/index.html').write_text('login')
        try:
            (self.root / 'alias.js').symlink_to('_next/static/app.js')
        except OSError:
            self.skipTest('symlink creation unavailable')
        with self.assertRaisesRegex(ValueError, 'non-regular'):
            ci_artifacts.create(self.root, SHA)

    def test_artifact_resource_limits_fail_closed(self):
        for attribute, value in (('MAX_FILES', 2), ('MAX_BYTES', 1)):
            with self.subTest(attribute=attribute), mock.patch.object(ci_artifacts, attribute, value):
                with self.assertRaisesRegex(ValueError, 'resource limits'):
                    ci_artifacts.verify(self.root, SHA)


class ReleaseIdentityTests(unittest.TestCase):
    def test_tag_resolves_to_commit_and_version_not_a_colliding_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                                       *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            git('init', '-q')
            (root / 'VERSION').write_text('1.2.3\n')
            git('add', 'VERSION')
            git('commit', '-qm', 'synthetic release')
            first = git('rev-parse', 'HEAD')
            git('tag', 'v1.2.3')
            (root / 'VERSION').write_text('9.9.9\n')
            git('commit', '-qam', 'different source')
            git('branch', 'v1.2.3')
            self.assertEqual(release_policy.resolve('v1.2.3', root),
                             {'tag': 'v1.2.3', 'version': '1.2.3', 'source_sha': first})
            git('tag', 'v1.2.4')
            with self.assertRaisesRegex(ValueError, 'VERSION'):
                release_policy.resolve('v1.2.4', root)

    def test_untrusted_tag_text_is_rejected_before_git(self):
        for tag in ('main', '--help', 'v1.2.3;touch /tmp/no', 'v1.2.3\nsource_sha=bad', 'v01.2.3', 'v1.2.3-rc1'):
            with self.subTest(tag=tag), mock.patch.object(release_policy.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    release_policy.resolve(tag, ROOT)
                run.assert_not_called()


def promotion_facts():
    repository, tag = 'example/gateway', 'v1.2.3'
    images = {'manager': {'name': 'ghcr.io/example/workbuddy-manager', 'digest': 'sha256:' + '1' * 64},
              'upstream': {'name': 'ghcr.io/example/workbuddy2api', 'digest': 'sha256:' + '2' * 64}}
    release = {'id': 23, 'tag_name': tag, 'draft': False, 'prerelease': False, 'published_at': '2026-11-27T00:00:00Z'}
    manifest = {'schema': release_policy.SCHEMA, 'repository': repository, 'tag': tag,
                'source_commit': SHA, 'workflow_source_sha': 'b' * 40, 'run_id': 31, 'run_attempt': 1,
                'images': images}
    run = {'id': 31, 'run_attempt': 1, 'path': '.github/workflows/release.yml', 'event': 'workflow_dispatch',
           'status': 'completed', 'conclusion': 'success', 'head_sha': 'b' * 40,
           'repository': {'full_name': repository}}
    jobs = [{'name': 'quality / ' + name, 'status': 'completed', 'conclusion': 'success'}
            for name in ('frontend', 'manager', 'gateway')]
    return {'repository': repository, 'tag': tag, 'source': SHA, 'manifest': manifest,
            'release': release, 'latest': copy.deepcopy(release), 'releases': [copy.deepcopy(release)],
            'run': run, 'jobs': jobs}


class PromotionEvidenceTests(unittest.TestCase):
    def test_published_latest_with_full_same_run_quality_has_a_plan(self):
        facts = promotion_facts()
        plan = release_policy.validate_promotion(**facts)
        self.assertEqual(plan['source_commit'], SHA)
        self.assertEqual(set(plan['images']), {'manager', 'upstream'})

    def test_draft_prerelease_history_and_downgrade_never_promote(self):
        for target, key, value in (('release', 'draft', True), ('release', 'prerelease', True),
                                   ('release', 'published_at', None), ('latest', 'tag_name', 'v1.2.4'),
                                   ('latest', 'id', 99)):
            with self.subTest(target=target, key=key):
                facts = promotion_facts()
                facts[target][key] = value
                with self.assertRaises(ValueError):
                    release_policy.validate_promotion(**facts)
        facts = promotion_facts()
        facts['releases'].append({'tag_name': 'v2.0.0', 'draft': False, 'prerelease': False})
        with self.assertRaisesRegex(ValueError, 'downgrade'):
            release_policy.validate_promotion(**facts)

    def test_wrong_source_digest_and_missing_image_evidence_are_rejected(self):
        for mutation in ('source', 'repository', 'digest', 'image_name', 'missing_image'):
            with self.subTest(mutation=mutation):
                facts = promotion_facts()
                manifest = facts['manifest']
                if mutation == 'source':
                    manifest['source_commit'] = 'c' * 40
                elif mutation == 'repository':
                    manifest['repository'] = 'someone/else'
                elif mutation == 'digest':
                    manifest['images']['manager']['digest'] = 'latest'
                elif mutation == 'image_name':
                    manifest['images']['manager']['name'] = 'ghcr.io/someone/else'
                else:
                    del manifest['images']['upstream']
                with self.assertRaises(ValueError):
                    release_policy.validate_promotion(**facts)

    def test_failed_wrong_or_retried_workflow_is_not_build_evidence(self):
        for key, value in (('conclusion', 'failure'), ('status', 'in_progress'), ('run_attempt', 2),
                           ('head_sha', 'c' * 40), ('path', '.github/workflows/other.yml'),
                           ('event', 'pull_request'), ('repository', {'full_name': 'someone/fork'})):
            with self.subTest(key=key):
                facts = promotion_facts()
                facts['run'][key] = value
                with self.assertRaises(ValueError):
                    release_policy.validate_promotion(**facts)

    def test_missing_skipped_or_duplicate_quality_job_blocks_promotion(self):
        for mutation in ('missing', 'skipped', 'duplicate'):
            with self.subTest(mutation=mutation):
                facts = promotion_facts()
                if mutation == 'missing':
                    facts['jobs'].pop()
                elif mutation == 'skipped':
                    facts['jobs'][0]['conclusion'] = 'skipped'
                else:
                    facts['jobs'].append(copy.deepcopy(facts['jobs'][0]))
                with self.assertRaises(ValueError):
                    release_policy.validate_promotion(**facts)


class FakeRegistry:
    def __init__(self, plan, fail_at=0, after_effect=False):
        self.aliases = {value['name']: 'sha256:' + '9' * 64 for value in plan['images'].values()}
        self.original = dict(self.aliases)
        self.calls = []
        self.writes = 0
        self.fail_at, self.after_effect = fail_at, after_effect

    def __call__(self, args):
        self.calls.append(list(args))
        if args[3] == 'inspect':
            ref = args[4]
            digest = ref.split('@', 1)[1] if '@' in ref else self.aliases[ref.removesuffix(':latest')]
            return json.dumps({'digest': digest})
        if args[3] != 'create':
            raise AssertionError(args)
        self.writes += 1
        name, digest = args[-1].split('@', 1)
        if self.writes == self.fail_at and not self.after_effect:
            raise OSError('simulated registry write failure')
        self.aliases[name] = digest
        if self.writes == self.fail_at:
            raise OSError('ambiguous response after registry accepted the write')
        return ''


class PromotionTransitionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.report = Path(self.directory.name) / 'receipt.json'
        self.plan = release_policy.validate_promotion(**promotion_facts())

    def test_dry_run_is_read_only_and_success_promotes_exact_digests(self):
        registry = FakeRegistry(self.plan)
        result = promote_images.apply_plan(self.plan, apply=False, report=self.report, run=registry)
        self.assertEqual(result['state'], 'planned')
        self.assertEqual(registry.writes, 0)
        result = promote_images.apply_plan(self.plan, apply=True, report=self.report, run=registry)
        self.assertEqual(result['state'], 'promoted')
        self.assertEqual(registry.aliases, {row['name']: row['digest'] for row in self.plan['images'].values()})
        promote_images.apply_plan(self.plan, apply=True, report=self.report, run=registry)
        self.assertEqual(registry.writes, 2, 'idempotent rerun must not rebuild or rewrite identical images')

    def test_second_write_failure_restores_both_even_after_ambiguous_success(self):
        for after_effect in (False, True):
            with self.subTest(after_effect=after_effect):
                registry = FakeRegistry(self.plan, fail_at=2, after_effect=after_effect)
                with self.assertRaises(OSError):
                    promote_images.apply_plan(self.plan, apply=True, report=self.report, run=registry)
                self.assertEqual(registry.aliases, registry.original)
                self.assertEqual(json.loads(self.report.read_text())['state'], 'rolled_back')

    def test_latest_release_changed_between_writes_restores_first(self):
        registry = FakeRegistry(self.plan)
        calls = 0
        def recheck():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError('newer release published')
        with self.assertRaises(ValueError):
            promote_images.apply_plan(self.plan, apply=True, report=self.report, run=registry, recheck=recheck)
        self.assertEqual(registry.aliases, registry.original)

    def test_missing_previous_alias_blocks_all_writes(self):
        registry = FakeRegistry(self.plan)
        registry.aliases.pop(next(iter(registry.aliases)))
        with self.assertRaises(KeyError):
            promote_images.apply_plan(self.plan, apply=True, report=self.report, run=registry)
        self.assertEqual(registry.writes, 0)

    def test_failed_rollback_is_reported_as_repair_required(self):
        registry = FakeRegistry(self.plan, fail_at=2)
        def blocked_restore(args):
            if args[3] == 'create' and args[-1].endswith('sha256:' + '9' * 64):
                raise OSError('registry refuses rollback')
            return registry(args)
        with self.assertRaises(OSError):
            promote_images.apply_plan(self.plan, apply=True, report=self.report, run=blocked_restore)
        value = json.loads(self.report.read_text())
        self.assertEqual(value['state'], 'repair_required')
        self.assertEqual(len(value['rollback_errors']), 1)

    def test_cancellation_after_first_move_restores_previous_alias(self):
        registry = FakeRegistry(self.plan)
        calls = 0
        def recheck():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt('cancelled')
        with self.assertRaises(KeyboardInterrupt):
            promote_images.apply_plan(self.plan, apply=True, report=self.report, run=registry, recheck=recheck)
        self.assertEqual(registry.aliases, registry.original)
        self.assertEqual(json.loads(self.report.read_text())['state'], 'rolled_back')


class WorkflowClosureTests(unittest.TestCase):
    def test_release_requires_reusable_quality_and_same_immutable_checkout(self):
        release = yaml.load((ROOT / '.github/workflows/release.yml').read_text(), Loader=yaml.BaseLoader)
        jobs = release['jobs']
        self.assertEqual(jobs['quality']['uses'], './.github/workflows/quality.yml')
        self.assertEqual(jobs['quality']['with']['source_sha'], '${{ needs.resolve.outputs.source_sha }}')
        self.assertEqual(set(jobs['release']['needs']), {'resolve', 'quality'})
        checkouts = [step for step in jobs['release']['steps'] if step.get('uses', '').startswith('actions/checkout@')]
        self.assertEqual(checkouts[0]['with']['ref'], '${{ needs.resolve.outputs.source_sha }}')
        build_steps = [step for step in jobs['release']['steps'] if step.get('uses', '').startswith('docker/build-push-action@')]
        self.assertEqual(len(build_steps), 2)
        for step in build_steps:
            self.assertNotIn(':latest', step['with']['tags'])
            self.assertIn("draft_writable == 'true'", step['if'])

    def test_backend_http_gates_require_frontend_artifact_and_verify_its_source(self):
        quality = yaml.load((ROOT / '.github/workflows/quality.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertIn('workflow_call', quality['on'])
        for name in ('manager', 'gateway'):
            job = quality['jobs'][name]
            self.assertEqual(job['needs'], 'frontend')
            steps = job['steps']
            download = next(i for i, step in enumerate(steps) if step.get('uses', '').startswith('actions/download-artifact@'))
            verify = next(i for i, step in enumerate(steps) if 'ci_artifacts.py verify' in step.get('run', ''))
            test = next(i for i, step in enumerate(steps) if 'ci_regression.py' in step.get('run', ''))
            self.assertLess(download, verify)
            self.assertLess(verify, test)
        promotion = yaml.load((ROOT / '.github/workflows/promote-images.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(promotion['on']['release']['types'], ['published'])
        self.assertNotIn('push', promotion['on'])
        self.assertEqual(promotion['concurrency']['cancel-in-progress'], 'false')


if __name__ == '__main__':
    unittest.main()
