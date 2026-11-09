"""Synthetic user stores for authorization tests; never bootstrap live paths."""
from pathlib import Path
from contextlib import contextmanager
import tempfile
import unittest
from unittest import mock

from server import config, db, security


@contextmanager
def isolated_data():
    """Detach the shared connection before any helper can resolve proxy settings."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        previous_connection = db._conn
        with mock.patch.object(config, 'DATA_DIR', root), \
                mock.patch.object(config, 'DB_PATH', root / 'fixture.db'), \
                mock.patch.object(config, 'USERS_FILE', root / 'users.json'):
            db._conn = None
            try:
                yield root
            finally:
                connection = db._conn
                db._conn = previous_connection
                if connection is not None and connection is not previous_connection:
                    connection.close()


class IsolatedDataTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(isolated_data())


def install_users(*names):
    password_hash = security.make_hash('synthetic-password')
    cfg = {'secret': 'synthetic-authorization-test-secret', 'role_policy_version': 2,
           'users': [{'username': name, 'role': 'admin', 'sv': 0,
                      'pwd_hash': password_hash} for name in dict.fromkeys(('admin', *names))],
           'api_keys': []}
    security.save_users(cfg)
    return cfg


def owner(username='admin', role='admin'):
    cfg = security.load_users(allow_bootstrap=False)
    row = next(row for row in cfg['users'] if row['username'] == username)
    return security.authorization_owner({
        'username': username, 'role': role,
        '_authorization_binding': security._authorization_fingerprint(cfg, row)})


def web_root():
    for parent in Path(__file__).resolve().parents:
        for candidate in (parent / 'web', parent / 'src' / 'three-platform' / 'web'):
            if (candidate / 'app').is_dir():
                return candidate
    raise RuntimeError('Frontend source required for this contract test')


def clear_login_state():
    from server.routers import accounts
    for name in ('_login_scopes', '_login_bindings', '_login_locks', '_login_ready',
                 '_login_polls', '_login_owner', '_login_regions', '_provisioned_states'):
        getattr(accounts, name).clear()
