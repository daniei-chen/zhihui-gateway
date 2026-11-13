import os
from pathlib import Path
import tempfile
import unittest

from deploy.relay_route import Refused, read_route, switch


class RelayRouteContractTest(unittest.TestCase):
    def test_switch_creates_monotonic_route_with_local_permissions(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'relay-route.json'
            self.assertEqual(switch(path, 'old'), {'epoch': 1, 'target': 'old'})
            self.assertEqual(switch(path, 'new'), {'epoch': 2, 'target': 'new'})
            self.assertEqual(read_route(path), {'epoch': 2, 'target': 'new'})
            if os.name == 'posix':
                mode = path.stat().st_mode & 0o777
                if os.geteuid() == 0:
                    self.assertEqual(mode, 0o640)
                    self.assertEqual(path.stat().st_gid, 10001)
                else:
                    self.assertEqual(mode, 0o600)

    def test_relative_route_is_rejected(self):
        with self.assertRaisesRegex(Refused, 'route_must_be_absolute'):
            switch('relative-route.json', 'old')
