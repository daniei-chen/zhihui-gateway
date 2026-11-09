"""Explicit, one-shot first-floor authority; never used by ordinary startup."""
import json
import os

from launch_guard import protected
from state_guard import Rejected, _json


class FirstInstall:
    def __init__(self, guard, journal):
        self.guard, self.journal = guard, journal
        self.root = guard.paths.floor.parent / 'first-install'
        self.intent = self.root / 'intent.json'

    @staticmethod
    def _identity(transaction, candidate, predecessor):
        return {'schema': 'zhihui.writer-first-install', 'version': 1,
                'transaction': transaction, 'candidate': candidate.release_id,
                'candidate_sha256': candidate.manifest_sha256,
                'predecessor': predecessor.release_id,
                'predecessor_sha256': predecessor.manifest_sha256}

    def _save(self, value):
        self.journal._atomic(self.intent, (json.dumps(value, sort_keys=True) + '\n').encode())

    def prepare(self, authority, transaction, candidate, predecessor):
        self.journal._authorized(authority)
        paths = self.guard.paths
        if any(p.exists() or p.is_symlink() for p in (paths.floor, paths.initialized, self.root)):
            raise Rejected('first installation already attempted; inspect retained history')
        protected(paths.floor.parent, paths, directory=True)
        # Exclusive directory creation is the retained attempt witness. Even a
        # crash before intent persistence is a repair case, never a new install.
        self.root.mkdir(mode=0o700)
        if os.name == 'posix':
            fd = os.open(self.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self._save({**self._identity(transaction, candidate, predecessor), 'phase': 'prepared'})

    def ensure(self, authority, record, candidate, observed, permit):
        self.journal._authorized(authority)
        self.guard._locked(permit, exclusive=True)
        protected(self.root, self.guard.paths, directory=True)
        value = _json(self.journal._read(self.intent), 'first installation')
        identity = {key: record[key] for key in ('transaction', 'candidate', 'candidate_sha256',
                                                'predecessor', 'predecessor_sha256')}
        expected = {'schema': 'zhihui.writer-first-install', 'version': 1, **identity}
        if (type(value) is not dict or set(value) != set(expected) | {'phase'}
                or any(value[k] != v for k, v in expected.items())
                or value['phase'] not in ('prepared', 'persisting', 'complete')
                or candidate.release_id != record['candidate']
                or candidate.manifest_sha256 != record['candidate_sha256']):
            raise Rejected('first installation identity mismatch')
        paths = self.guard.paths
        if value['phase'] == 'prepared':
            if any(p.exists() or p.is_symlink() for p in (paths.floor, paths.initialized)):
                raise Rejected('writer floor appeared outside first installation')
            self._save({**value, 'phase': 'persisting'})
            floor = self.guard.initialize_floor(observed, candidate, permit, record['transaction'])
        else:
            # A persistence attempt without a readable floor cannot initialize
            # again, even if both state_guard files disappeared afterwards.
            floor = self.guard.load_floor()
            if (floor.transaction_id != record['transaction']
                    or floor.capabilities != candidate.capabilities):
                raise Rejected('first installation floor differs from retained attempt')
        self._save({**value, 'phase': 'complete'})
        return floor
