import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from baseline.lift_modbus import append_crc
from lab.model import Config, LabError
from lab.service import Service
from lab.transport import Simulator, Trace, TcpTransport


class ConnectionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.root_patch=patch('lab.service.ROOT',self.root)
        self.root_patch.start()
        self.service=Service(Config(mode='sim',home_enable_hold_s=0,home_disable_hold_s=0, state_file=str(self.root/'zero.json'),poll_s=.02,stop_timeout_s=.15))
        self.headers={'X-Patrol-Client-Id':'connection-tester'}
        await self.service.start()
        await self.until(lambda:bool(self.service.controller.sample))

    async def asyncTearDown(self):
        if isinstance(self.service.transport,Simulator):
            self.service.transport.faults['offline']=False
            self.service.transport.faults['lost_stop']=0
        await self.service.close()
        self.root_patch.stop()
        self.temp.cleanup()

    async def until(self,predicate):
        deadline=time.monotonic()+3
        while not predicate():
            if time.monotonic()>deadline:
                self.fail('condition timed out')
            await asyncio.sleep(.01)

    async def request(self,method,path,body=None):
        return await self.service.request(method,path,body,self.headers)

    async def test_disconnect_gets_are_cache_only_and_controls_cannot_reconnect(self):
        old=self.service.transport
        await self.request('POST','/api/connection/disconnect',{})
        self.assertFalse(old.active)
        self.assertFalse(self.service.connected)
        self.assertFalse(self.service.controller.snapshot()['online'])
        self.assertEqual(self.service.controller.state,'DISCONNECTED')
        count=self.service.trace.serial
        for path in ('/api/parameters','/api/status','/api/registers','/api/readiness','/api/connection'):
            await self.request('GET',path)
        self.assertEqual(self.service.trace.serial,count)
        for path,body in [('/api/arm',{}),('/api/registers/refresh',{}),('/api/control',{'action':'home'}),
                          ('/api/manual/transaction',{'frame':append_crc(bytes.fromhex('01 03 0b 00 00 01')).hex()})]:
            with self.subTest(path=path),self.assertRaises(LabError):
                await self.request('POST',path,body)
        with self.assertRaises(ConnectionError):
            await old.transaction(append_crc(bytes.fromhex('01 03 0b 00 00 01')),7)
        self.assertEqual(self.service.trace.serial,count)

    async def test_save_connection_does_not_switch_until_explicit_connect(self):
        old=self.service.transport
        saved=dict(mode='sim',host='192.0.2.10',port=9999,slave_id=2,timeout=.2)
        await self.request('POST','/api/connection/save',saved)
        self.assertIs(self.service.transport,old)
        self.assertEqual(self.service.config.host,'10.21.31.15')
        result=await self.request('GET','/api/connection')
        self.assertEqual(result['saved'],saved)
        await self.request('POST','/api/connection/connect',saved)
        self.assertIsNot(self.service.transport,old)
        self.assertEqual(self.service.config.host,saved['host'])
        self.assertEqual(self.service.config.slave_id,2)

    async def test_reconnect_discards_lease_armed_reference_and_manual_state(self):
        old=self.service.controller
        await old.arm()
        old.trusted=True
        await self.service.acquire('connection-tester','local','tester')
        await self.request('POST','/api/connection/connect',{'mode':'sim'})
        self.assertIsNot(self.service.controller,old)
        self.assertTrue(old._closed)
        self.assertFalse(self.service.controller.armed)
        self.assertFalse(self.service.controller.trusted)
        self.assertIsNone(self.service.lease)
        self.assertFalse(self.service.manual_active)
        self.assertTrue(self.service.controller.snapshot()['online'])

    async def test_failed_stop_retains_old_controller_and_watchdog(self):
        oldctrl,oldtransport=self.service.controller,self.service.transport
        oldctrl.mode='pv'
        oldctrl.state='MOVING'
        oldtransport.faults['lost_stop']=100
        with self.assertRaises(LabError):
            await self.request('POST','/api/connection/disconnect',{})
        self.assertIs(self.service.controller,oldctrl)
        self.assertIs(self.service.transport,oldtransport)
        self.assertFalse(oldctrl._closed)
        self.assertTrue(any(not task.done() for task in oldctrl._tasks))
        self.assertTrue(oldtransport.active)
        self.assertTrue(self.service.connected)
        self.assertEqual(self.service.connection_state,'stop_pending')
        with self.assertRaises(LabError):
            await self.request('POST','/api/arm',{})
        oldtransport.faults['lost_stop']=0
        await self.until(lambda:not oldctrl.pending_stop)
        await self.request('POST','/api/connection/disconnect',{})
        self.assertFalse(self.service.connected)

    async def test_real_connect_requires_confirmation_without_changing_current(self):
        old=self.service.transport
        with patch('lab.service.TcpTransport',side_effect=AssertionError('must not open real transport')):
            with self.assertRaises(LabError):
                await self.request('POST','/api/connection/connect',{'mode':'real'})
        self.assertIs(self.service.transport,old)
        self.assertTrue(self.service.connected)

    async def test_mock_real_connect_only_reads_and_drops_sim_profile_acceptance(self):
        self.service.config.motion_profile_verified=True
        transports=[]
        def factory(config,trace):
            sim=Simulator(config,Trace())
            transports.append(sim)
            return sim
        with patch('lab.service.TcpTransport',side_effect=factory),patch('asyncio.open_connection',side_effect=AssertionError('no hardware network')):
            await self.request('POST','/api/connection/connect',dict(mode='real',host='192.0.2.55',confirm_real=True))
        self.assertEqual(self.service.config.mode,'real')
        self.assertFalse(self.service.config.motion_profile_verified)
        self.assertFalse(self.service.controller.armed)
        self.assertFalse(self.service.controller.trusted)
        requests=[bytes.fromhex(item['frame']) for item in transports[0].trace.items if item['kind']=='tx']
        self.assertTrue(requests)
        self.assertTrue(all(request[1]==3 for request in requests))

    async def test_failed_real_connect_cleans_up_and_stays_disconnected(self):
        transports=[]
        def factory(config,trace):
            sim=Simulator(config,trace)
            sim.faults['offline']=True
            transports.append(sim)
            return sim
        with patch('lab.service.TcpTransport',side_effect=factory),patch('asyncio.open_connection',side_effect=AssertionError('no hardware network')):
            with self.assertRaises(LabError):
                await self.request('POST','/api/connection/connect',{'mode':'real','confirm_real':True})
        self.assertFalse(self.service.connected)
        self.assertEqual(self.service.connection_state,'disconnected')
        self.assertFalse(transports[0].active)
        self.assertFalse(self.service.controller.snapshot()['online'])
        self.assertIn('连接失败',self.service.connection_error)

    async def test_monitor_only_disconnect_does_not_stop_external_motion(self):
        old=self.service.transport
        old.enabled=True
        old.motion='up'
        self.service.trace.clear()
        await self.service.disconnect()
        frames=[bytes.fromhex(x['frame']) for x in self.service.trace.items if x['kind']=='tx']
        self.assertTrue(all(frame[1]==3 for frame in frames))
        self.assertEqual(old.motion,'up')

    async def test_old_tcp_transport_cannot_open_socket_after_shutdown(self):
        transport=TcpTransport(Config(mode='sim',home_enable_hold_s=0,home_disable_hold_s=0),Trace())
        await transport.shutdown()
        with patch('asyncio.open_connection',side_effect=AssertionError('must not reconnect')):
            with self.assertRaises(ConnectionError):
                await transport.transaction(append_crc(bytes.fromhex('01 03 0b 00 00 01')),7)

    async def test_queued_manual_write_never_targets_replacement_controller(self):
        await self.service.acquire('connection-tester','local','tester')
        await self.service._manual_lock.acquire()
        raw=append_crc(bytes.fromhex('01 06 0d 08 00 10')).hex()
        pending=asyncio.create_task(self.request('POST','/api/manual/transaction',{'frame':raw,'confirm_write':True}))
        await asyncio.sleep(.02)
        await self.service.connect({'mode':'sim'})
        await self.service.acquire('connection-tester','local','tester')
        self.service._manual_lock.release()
        with self.assertRaises(LabError):
            await pending
        self.assertFalse(self.service.manual_active)
        self.assertIsNone(self.service.transport.motion)

    async def test_same_verified_real_target_keeps_profile_but_new_target_does_not(self):
        self.service.config.mode='real'
        self.service.config.motion_profile_verified=True
        def factory(config,trace):
            return Simulator(config,trace)
        with patch('lab.service.TcpTransport',side_effect=factory),patch('asyncio.open_connection',side_effect=AssertionError('no hardware network')):
            await self.service.connect({'mode':'real','confirm_real':True})
            self.assertTrue(self.service.config.motion_profile_verified)
            await self.service.connect({'mode':'real','host':'192.0.2.99','confirm_real':True})
            self.assertFalse(self.service.config.motion_profile_verified)

    async def test_disconnect_cancels_queued_start_before_destroying_driver(self):
        ctrl=self.service.controller
        await ctrl.arm()
        self.service.transport.inject('physical_cm',1)
        await ctrl.observe()
        ctrl.trusted=True
        ctrl.zero=0
        entered,release=asyncio.Event(),asyncio.Event()
        original=ctrl.driver.start
        async def delayed_start(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        with patch.object(ctrl.driver,'start',side_effect=delayed_start):
            motion=asyncio.create_task(ctrl.command({'action':'goto','target_cm':2}))
            await entered.wait()
            disconnect=asyncio.create_task(self.service.disconnect())
            await asyncio.sleep(.03)
            release.set()
            with self.assertRaises(LabError):
                await motion
            await disconnect
        self.assertFalse(self.service.connected)
        frames=[bytes.fromhex(x['frame']) for x in self.service.trace.items if x['kind']=='tx']
        self.assertFalse(any(x[1]==6 and x[2:6]==bytes.fromhex('0d 08 00 03') for x in frames))


if __name__=='__main__':
    unittest.main()
