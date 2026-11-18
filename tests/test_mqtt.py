from __future__ import annotations
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from lab.model import Config
from lab.service import Service
from lab.mqtt import publish_loop


async def read_packet(reader):
    kind=(await reader.readexactly(1))[0]
    size=0;multiplier=1
    while True:
        digit=(await reader.readexactly(1))[0]
        size+=(digit&127)*multiplier
        if not digit&128:
            break
        multiplier*=128
    return kind,await reader.readexactly(size)


class MqttTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_and_snapshot_publish_to_local_fake_broker(self):
        received=[]
        done=asyncio.Event()
        async def handle(reader,writer):
            try:
                kind,body=await read_packet(reader)
                self.assertEqual(kind,0x10)
                self.assertEqual(body[7],0xC2)
                self.assertTrue(body.endswith(b'\x00\x04user\x00\x04pass'))
                writer.write(b'\x20\x02\x00\x00');await writer.drain()
                for _ in range(2):
                    kind,body=await read_packet(reader)
                    self.assertEqual(kind,0x30)
                    length=int.from_bytes(body[:2],'big')
                    received.append((body[2:2+length].decode(),json.loads(body[2+length:])))
                done.set()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
        server=await asyncio.start_server(handle,'127.0.0.1',0)
        with tempfile.TemporaryDirectory() as temp:
            cfg=Config(mode='sim', state_file=str(Path(temp)/'zero.json'),mqtt_host='127.0.0.1',mqtt_port=server.sockets[0].getsockname()[1],mqtt_username='user',mqtt_password='pass')
            svc=Service(cfg)
            task=asyncio.create_task(publish_loop(svc))
            try:
                await asyncio.wait_for(done.wait(),2)
                self.assertEqual([r[0] for r in received],['patrol/lab/lift/status','patrol/lab/snapshot'])
                self.assertIn('details',received[0][1])
                self.assertIn('lift',received[1][1])
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)
                await svc.close()
                server.close();await server.wait_closed()
