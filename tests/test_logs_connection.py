import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.model import Config, LabError, load_config
from lab.service import Service
from lab.transport import Trace


class TraceTests(unittest.TestCase):
    def test_capacity_tail_and_monotonic_clear(self):
        trace=Trace()
        for i in range(20005):
            trace.add('rx',frame=str(i))
        self.assertEqual(len(trace.items),20000)
        self.assertTrue(trace.tail(1)['truncated'])
        self.assertEqual(len(trace.tail(20000)['items']),5)
        cursor=trace.clear()['cursor']
        trace.add('tx',frame='new')
        self.assertEqual(trace.tail(cursor)['items'][0]['id'],cursor+1)


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_is_deferred_and_survives_explicit_startup_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            service=Service(Config(mode='sim', state_file=str(root/'state.json')))
            body=dict(host='192.168.10.20',port=8899,slave_id=2,timeout=.8)
            with patch('lab.service.ROOT',root),patch('lab.model.ROOT',root):
                await service.request('POST','/api/connection',body,{'X-Patrol-Client-Id':'test'})
                self.assertEqual(service.config.host,'10.21.31.15')
                self.assertEqual(service.config.mode,'sim')
                self.assertEqual(load_config().host,body['host'])
                self.assertEqual(load_config().mode,'sim')
                explicit=root/'explicit.json'
                explicit.write_text(json.dumps(dict(host='127.0.0.1')),encoding='utf-8')
                self.assertEqual(load_config(str(explicit)).host,body['host'])
                observed=await service.request('GET','/api/connection')
                self.assertEqual(observed['saved'],dict(body,mode='sim'))

    async def test_invalid_connection_and_unknown_mode_rejected(self):
        service=Service(Config(mode='sim'))
        headers={'X-Patrol-Client-Id':'test'}
        for body in [dict(host='http://bad',port=1,slave_id=1,timeout=.5),
                     dict(host='host',port=0,slave_id=1,timeout=.5),
                     dict(host='host',port=1,slave_id=1,timeout=.5,mode='invalid')]:
            with self.assertRaises((LabError,ValueError)):
                await service.request('POST','/api/connection',body,headers)

    async def test_log_endpoints_clear_without_resetting_cursor(self):
        service=Service(Config(mode='sim'))
        headers={'X-Patrol-Client-Id':'test'}
        service.trace.add('tx',frame='one')
        batch=await service.request('POST','/api/logs/tail',{'after':0},headers)
        await service.request('POST','/api/logs/clear',{},headers)
        service.trace.add('rx',frame='two')
        after=await service.request('POST','/api/logs/tail',{'after':batch['cursor']},headers)
        self.assertEqual([row['frame'] for row in after['items']],['two'])
