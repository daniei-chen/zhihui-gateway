from unittest.mock import patch

from server import config
from server.tests.test_v007_protocol_replay import ProtocolReplayTest


class SharedProtocolTest(ProtocolReplayTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from server.main import app
        cls.app = app
        from server import observability
        cls.old_ref = observability._middleware_ref
        cls.old_stack = app.middleware_stack
        app.middleware_stack = None
        cls.optin = patch.object(config, 'SHARED_ADMISSION', True)
        cls.optin.start()

    @classmethod
    def tearDownClass(cls):
        layer = cls.app.middleware_stack
        while layer is not None:
            coordinator = getattr(layer, 'shared', None)
            if coordinator is not None:
                coordinator.close()
            layer = getattr(layer, 'app', None)
        cls.app.middleware_stack = cls.old_stack
        from server import observability
        observability._middleware_ref = cls.old_ref
        cls.c.close()
        cls.optin.stop()
        super().tearDownClass()

    def tearDown(self):
        layer = self.app.middleware_stack
        found = False
        while layer is not None:
            coordinator = getattr(layer, 'shared', None)
            if coordinator is not None:
                found = True
                self.assertEqual(coordinator.snapshot()['leases'], 0)
            layer = getattr(layer, 'app', None)
        self.assertTrue(found)
