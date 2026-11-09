"""Threaded imports must enforce no-overwrite under the same file lock."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from server.routers import accounts
from server.services import tencent


class ConcurrentImportContracts(unittest.TestCase):
    def test_two_new_imports_of_same_uid_have_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            barrier=threading.Barrier(2)
            original=tencent.file_lock
            @contextmanager
            def synchronized_lock(*args,**kwargs):
                # Both calls reach the lock after any unsafe pre-lock exists check.
                barrier.wait(timeout=5)
                with original(*args,**kwargs):yield
            def attempt(token):
                try:
                    result=accounts._persist_uploaded_account({'uid':'parallel-import','accessToken':token},'one.json',base,False)
                    return {'ok':True,'token':token,'result':result}
                except tencent.AccountExistsError:
                    return {'ok':False,'token':token}
            with patch.object(tencent,'file_lock',synchronized_lock):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results=list(pool.map(attempt,['synthetic-first','synthetic-second']))
            winners=[r for r in results if r['ok']]
            self.assertEqual(len(winners),1,'no-overwrite check raced outside the file lock')
            saved=json.loads((base/'workbuddy-parallel-import.json').read_text(encoding='utf8'))
            self.assertEqual(saved['auth']['accessToken'],winners[0]['token'])
            self.assertEqual(len(list(base.glob('*.json'))),1)
