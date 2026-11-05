"""The audit catalog must retain effective routes and distinguish unknown auth."""
from pathlib import Path
import importlib.util
import unittest

from fastapi import APIRouter, Depends, FastAPI

spec = importlib.util.spec_from_file_location('control_plane_audit',
    Path(__file__).resolve().parents[1] / 'tools/audit_control_plane.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class ControlPlaneInventoryTest(unittest.TestCase):
    def test_nested_router_prefixes_and_include_guards_are_retained(self):
        def current_user():
            return {'role': 'viewer'}

        child = APIRouter(prefix='/api')

        @child.get('/accounts')
        def accounts():
            return []

        parent = APIRouter(prefix='/internal')
        parent.include_router(child, dependencies=[Depends(current_user)])
        app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        app.include_router(parent, prefix='/mounted')
        rows = list(audit._iter_routes(app.routes))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], '/mounted/internal/api/accounts')
        self.assertIn('current_user', rows[0][2])

    def test_missing_dependency_metadata_never_implies_anonymous_access(self):
        self.assertEqual(audit._auth_mechanism('new.router', 'endpoint', '/new', []),
                         ('review_required', None, None))
        self.assertEqual(audit._auth_mechanism('server.routers.gateway', 'chat_v1', '/v1/chat/completions', [])[1:],
                         (True, False))
        self.assertEqual(audit._auth_mechanism('server.qtgcore.routers.compat', 'endpoint', '/__qtg/api/accounts', [])[1:],
                         (True, False))

    def test_portal_dependency_is_recognized_as_separate_session(self):
        self.assertEqual(audit._auth_mechanism('server.routers.portal', 'overview', '/api/portal/overview', ['current']),
                         ('portal_session_dependency', True, False))
