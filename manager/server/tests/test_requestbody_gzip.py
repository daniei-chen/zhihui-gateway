"""Synthetic gzip uploads: no real accounts, credentials or inference calls."""
import asyncio
import gzip
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from server import observability, requestbody
from server.routers import gateway
from server.tests.authorization_test_support import isolated_data


class DecoderTests(unittest.IsolatedAsyncioTestCase):
    async def decode(self, raw, *, limit=1024*1024, split=7, reserve=None):
        pieces = [raw[i:i+split] for i in range(0,len(raw),split)] or [b'']
        async def receive():
            part = pieces.pop(0)
            return {'type':'http.request','body':part,'more_body':bool(pieces)}
        return b''.join([m['body'] async for m in requestbody.body_frames(
            receive, 'gzip', limit, reserve=reserve)])

    async def test_tiny_chunked_unicode_and_crc_are_validated(self):
        raw=json.dumps({'text':'中文 / 🌊' * 30000},ensure_ascii=False).encode()
        sizes=[]
        self.assertEqual(await self.decode(gzip.compress(raw),reserve=sizes.append),raw)
        self.assertTrue(all(a<=b for a,b in zip(sizes,sizes[1:])))
        self.assertEqual(sizes[-1],len(raw)+len(gzip.compress(raw)))

    async def test_zip_bomb_is_rejected_at_decoded_limit(self):
        with self.assertRaises(requestbody.BodySizeExceeded):
            await self.decode(gzip.compress(b'x'*2000000),limit=10000,split=4096)

    async def test_corrupt_truncated_trailing_and_concatenated_members_are_rejected(self):
        valid=gzip.compress(b'{"messages":[]}')
        bad_crc=valid[:-8]+bytes([valid[-8]^1])+valid[-7:]
        for value in (b'',b'not-gzip',valid[:-1],bad_crc,valid+b'extra',valid+valid):
            with self.subTest(size=len(value)),self.assertRaises(requestbody.BodyEncodingError):
                await self.decode(value)

    async def test_wire_limit_is_independent_of_decoded_limit(self):
        raw=gzip.compress(os.urandom(10000),compresslevel=1)
        with self.assertRaises(requestbody.BodySizeExceeded):
            await self.decode(raw,limit=9999,split=len(raw))

    async def test_capacity_exception_propagates_before_retaining_more_output(self):
        sizes=[]
        def reserve(size):
            sizes.append(size)
            if size>100000:raise observability.CapacityError()
        with self.assertRaises(observability.CapacityError):
            await self.decode(gzip.compress(b'x'*1000000),reserve=reserve)
        self.assertLess(sizes[-1],200000)

    async def test_tiny_identity_frames_are_coalesced_not_retained_per_byte(self):
        count=200000
        async def receive():
            nonlocal count
            count-=1
            return {'type':'http.request','body':b'x','more_body':count>0}
        frames=[m async for m in requestbody.body_frames(receive,'identity',300000)]
        self.assertEqual(sum(len(m['body'])for m in frames),200000)
        self.assertEqual(len(frames),4)

    def test_unknown_duplicate_or_stacked_encodings_rejected(self):
        self.assertEqual(requestbody.content_encoding([(b'content-encoding',b'GZip')]),'gzip')
        for rows in ([(b'content-encoding',b'br')],[(b'content-encoding',b'gzip, gzip')],
                     [(b'content-encoding',b'gzip'),(b'content-encoding',b'gzip')]):
            with self.subTest(rows=rows),self.assertRaises(requestbody.BodyEncodingError):
                requestbody.content_encoding(rows)


class GzipAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(isolated_data())
        self.enterContext(patch('server.db.execute'))
        self.enterContext(patch('server.budget.finish'))
        self.enterContext(patch('server.observability.ensure_outcome_schema',return_value=False))
        self.auth=self.enterContext(patch('server.routers.gateway.preauthorize_body',return_value=None))

    async def drive(self, raw, *, encoding=b'gzip', auth=None, limit=1024*1024, capacity=2*1024*1024):
        calls,entered,frames=[],[],[]
        source=[raw[i:i+17] for i in range(0,len(raw),17)] or [b'']
        async def receive():
            calls.append(True)
            chunk=source.pop(0)
            return {'type':'http.request','body':chunk,'more_body':bool(source)}
        async def send(message):frames.append(message)
        async def app(scope,receive,send):
            entered.append(True)
            self.assertNotIn(b'content-encoding',dict(scope['headers']))
            body,error=await gateway._read_json_body(__import__('starlette.requests',fromlist=['Request']).Request(scope,receive))
            self.assertIsNone(error)
            self.assertEqual(body,{'model':'fixture','messages':[]})
            observability.current.get().reserve_wire_body(100)
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'fixture-only'})
        scope={'type':'http','method':'POST','path':'/v1/chat/completions',
               'headers':[(b'content-encoding',encoding),(b'content-length',str(len(raw)).encode())]}
        self.auth.return_value=auth
        with patch.dict(os.environ,{'WB_GATEWAY_MAX_INFLIGHT_BODY_BYTES':str(capacity),
                'WB_GATEWAY_MIN_BODY_RESERVATION_BYTES':'1'}),patch.object(gateway,'max_body_bytes',return_value=limit):
            middleware=observability.RequestAuditMiddleware(app)
            await middleware(scope,receive,send)
        self.assertEqual((middleware.active,middleware.large,middleware.inflight_body_bytes),(0,0,0))
        return frames,calls,entered

    async def test_gzip_is_decoded_once_then_routes_receive_json(self):
        frames,_,entered=await self.drive(gzip.compress(b'{"model":"fixture","messages":[]}'))
        self.assertEqual(frames[0]['status'],200)
        self.assertEqual(entered,[True])

    async def test_invalid_auth_precedes_decompression_and_receive(self):
        frames,calls,entered=await self.drive(b'invalid',auth=gateway._oai_error('fixture',401))
        self.assertEqual(frames[0]['status'],401)
        self.assertEqual((calls,entered),([],[]))

    async def test_bad_body_unsupported_bomb_and_capacity_never_dispatch(self):
        for raw,encoding,limit,capacity,status in ((b'bad',b'gzip',1000,10000,400),
                (b'{}',b'br',1000,10000,415),
                (gzip.compress(b'x'*100000),b'gzip',1000,10000,413),
                (gzip.compress(b'x'*100000),b'gzip',200000,1000,503)):
            with self.subTest(status=status):
                frames,_,entered=await self.drive(raw,encoding=encoding,limit=limit,capacity=capacity)
                self.assertEqual(frames[0]['status'],status)
                self.assertEqual(entered,[])


class UploadPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path=Path(__file__).resolve().parents[2]/'client/zhihui_upload.py'
        spec=importlib.util.spec_from_file_location('zhihui_upload_fixture',path)
        cls.helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.helper)

    def test_small_requests_skip_compression_and_large_text_uses_it(self):
        body,headers=self.helper.encode_upload({'text':'small'})
        self.assertNotIn('Content-Encoding',headers)
        payload={'text':'x'*(256*1024)}
        body,headers=self.helper.encode_upload(payload)
        self.assertEqual(headers['Content-Encoding'],'gzip')
        self.assertEqual(json.loads(gzip.decompress(body)),payload)

    def test_incompressible_large_body_stays_plain_and_limit_is_precompression(self):
        import base64
        body,headers=self.helper.encode_upload({'text':base64.b64encode(os.urandom(300000)).decode()})
        # Base64 still commonly compresses >10%; use deterministic encoder injection
        # to prove the policy rejects a gzip result that does not save enough.
        with patch.object(self.helper.gzip,'compress',side_effect=lambda raw,**kw:raw):
            body,headers=self.helper.encode_upload({'text':'x'*300000})
            self.assertNotIn('Content-Encoding',headers)
        with patch.object(self.helper,'MAX_DECODED_BYTES',100),self.assertRaises(ValueError):
            self.helper.encode_upload({'text':'x'*101})


if __name__=='__main__':unittest.main()
