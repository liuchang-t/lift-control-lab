import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baseline.lift_modbus import build_write_single_register
from lab.controller import MotionCancelled
from lab.model import Config
from lab.service import Service


class ManualPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=Service(Config(mode='sim',state_file=str(Path(self.tmp.name)/'state.json'),stop_timeout_s=.3))
        self.ctrl=self.service.controller
        self.bus=self.service.transport
        # No background service or network connection is needed for these races.
        self.service.connected=True
        self.service.connection_state='connected'

    async def asyncTearDown(self):
        await self.ctrl.close(stop=False)
        self.tmp.cleanup()

    async def wait_until(self,check):
        for _ in range(100):
            if check():return
            await asyncio.sleep(.001)
        self.fail('race checkpoint was not reached')

    async def test_manual_takeover_cancels_queued_single_and_multi_parameter_writes(self):
        for multi in (False,True):
            with self.subTest(multi=multi):
                self.service._clear_manual()
                entered=asyncio.Event()
                async def automatic():
                    async with self.ctrl._sequence:
                        entered.set()
                        if multi:
                            await self.ctrl.driver.wire.write_registers(0x100E,[123,0])
                        else:
                            await self.ctrl.driver.jog_speed(10000)
                await self.bus.lock.acquire()
                old=asyncio.create_task(automatic())
                await entered.wait()
                await asyncio.sleep(0)
                mark=self.service.trace.serial
                frame=build_write_single_register(1,0x1003,6).hex(' ')
                manual=asyncio.create_task(self.service.manual_transaction('tester',dict(frame=frame,confirm_write=True)))
                try:
                    await self.wait_until(lambda:bool(self.service.manual_active))
                finally:
                    self.bus.lock.release()
                with self.assertRaises(MotionCancelled):await old
                await manual
                writes=[item['frame'] for item in self.service.trace.items if item['id']>mark and item['kind']=='tx' and bytes.fromhex(item['frame'])[1] in (6,16)]
                self.assertEqual(writes,[frame])
                self.assertEqual(self.ctrl.state,'MANUAL')

    async def test_queued_read_remains_available_after_takeover(self):
        await self.bus.lock.acquire()
        reading=asyncio.create_task(self.ctrl.driver.wire.read_registers(0x1003,1))
        await asyncio.sleep(0)
        self.ctrl.take_manual_control()
        self.bus.lock.release()
        self.assertEqual(await reading,[3])

    async def test_write_cancelled_after_tcp_connect_before_bytes_sent(self):
        service=Service(Config(mode='real',state_file=str(Path(self.tmp.name)/'real.json')))
        ctrl=service.controller
        opened=asyncio.Event();release=asyncio.Event()
        class Writer:
            def __init__(self):self.frames=[]
            def write(self,frame):self.frames.append(frame)
            def close(self):pass
            async def wait_closed(self):pass
        writer=Writer()
        async def connect(*args):
            opened.set()
            await release.wait()
            return object(),writer
        try:
            with patch('asyncio.open_connection',side_effect=connect):
                writing=asyncio.create_task(ctrl.driver.wire.write_register(0x4604,100))
                await opened.wait()
                ctrl.take_manual_control()
                release.set()
                with self.assertRaises(MotionCancelled):await writing
            self.assertEqual(writer.frames,[])
        finally:
            await ctrl.close(stop=False)

    async def test_expired_stop_failure_does_not_replace_manual_state(self):
        for background in (False,True):
            with self.subTest(background=background):
                self.ctrl._closed=False
                self.ctrl.manual_active=False
                self.ctrl._stop_intent()
                entered=asyncio.Event();release=asyncio.Event()
                async def stop(*args):
                    entered.set()
                    await release.wait()
                    raise TimeoutError('old stop response lost')
                with patch.object(self.ctrl.driver,'stop',side_effect=stop):
                    waiter=asyncio.create_task(self.ctrl._watchdog() if background else self.ctrl._wait_for_stop())
                    await entered.wait()
                    self.ctrl.take_manual_control()
                    release.set()
                    if background:
                        await asyncio.sleep(.01)
                        self.ctrl._closed=True
                    await waiter
                self.assertFalse(self.ctrl.pending_stop)
                self.assertEqual(self.ctrl.state,'MANUAL')
                self.assertEqual(self.ctrl.fault,'')
