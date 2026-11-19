import unittest
import time
from unittest.mock import patch
from lab.driver import Driver, StopNotSettled
from lab.model import Config
from lab.transport import Simulator, Trace

class BusCleanupSettlingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg=Config(mode='sim',home_enable_hold_s=0,home_disable_hold_s=0)
        self.trace=Trace();self.sim=Simulator(self.cfg,self.trace)
        self.driver=Driver(self.cfg,self.sim)
        self.driver.bus_enable_owned=True
        self.sim.regs[0x0307]=1

    def writes(self):
        frames=[bytes.fromhex(x['frame']) for x in self.trace.items if x['kind']=='tx']
        return [(int.from_bytes(f[2:4],'big'),int.from_bytes(f[4:6],'big')) for f in frames if f[1]==6]

    async def test_disable_settling_exits_force_and_retry_does_not_enable_again(self):
        original=self.driver.hold_home_di3
        async def settle(given,*args,**kwargs):
            if given==511:raise StopNotSettled('unload settling')
            return await original(given,*args,**kwargs)
        with patch.object(self.driver,'hold_home_di3',side_effect=settle):
            with self.assertRaises(StopNotSettled):await self.driver.release_bus_enable()
        self.assertEqual(self.sim.regs[0x0d11],0)
        self.assertTrue(self.driver.bus_enable_owned)
        self.assertTrue(await self.driver.release_bus_enable())
        self.assertEqual(self.writes().count((0x0d12,507)),1)
        self.assertFalse(self.driver.bus_enable_owned)

    async def test_force_exit_ack_loss_retries_exit_only(self):
        original=self.driver._write_register
        async def lose(address,value):
            await original(address,value)
            if (address,value)==(0x0d11,0):raise TimeoutError('exit ACK lost')
        with patch.object(self.driver,'_write_register',side_effect=lose):
            with self.assertRaises(TimeoutError):await self.driver.release_bus_enable()
        self.assertTrue(await self.driver.release_bus_enable())
        self.assertEqual(self.writes().count((0x0d12,507)),1)
        self.assertEqual(self.writes().count((0x0d11,0)),2)

    async def test_enable_hold_motion_does_not_claim_completion(self):
        async def move(*args,**kwargs):raise StopNotSettled('moving in 507')
        with patch.object(self.driver,'hold_home_di3',side_effect=move):
            with self.assertRaises(StopNotSettled):await self.driver.release_bus_enable()
        self.assertTrue(self.driver.bus_enable_owned)
        self.assertIsNone(self.driver._bus_cleanup_tail)
        self.assertEqual(self.driver.home_cleanup_serial,0)

    async def test_manual_ownership_handover_clears_pending_tail(self):
        self.driver._bus_cleanup_tail=(self.sim.epoch,'stationary')
        self.driver.bus_enable_owned=False
        self.assertIsNone(self.driver._bus_cleanup_tail)

    async def test_real_hold_detects_motion_as_non_reference_failure(self):
        self.cfg.home_disable_hold_s=.1
        self.sim.regs[0x0d11]=1;self.sim.regs[0x0d12]=511
        original=self.driver._read_words
        async def speed(address,count):
            if address==0x0b00:return [2]
            return await original(address,count)
        with patch.object(self.driver,'_read_words',side_effect=speed):
            with self.assertRaises(StopNotSettled) as caught:
                await self.driver.hold_home_di3(511,lambda:None)
        self.assertFalse(caught.exception.reference_lost)

    async def test_pending_tail_is_not_reused_on_new_connection(self):
        self.driver._bus_cleanup_tail=(self.sim.epoch-1,'stationary')
        self.assertTrue(await self.driver.release_bus_enable())
        self.assertEqual(self.writes().count((0x0d12,507)),1)

    async def test_feedback_motion_after_force_exit_keeps_waiting_without_507(self):
        original=self.driver.sample
        async def moving():
            data=await original()
            if self.driver._bus_cleanup_tail and self.driver._bus_cleanup_tail[1]=='stationary':
                data['speed_rpm']=2
            return data
        with patch.object(self.driver,'sample',side_effect=moving):
            with self.assertRaises(StopNotSettled):await self.driver.release_bus_enable()
            with self.assertRaises(StopNotSettled):await self.driver.release_bus_enable()
        self.assertEqual(self.sim.regs[0x0d11],0)
        self.assertEqual(self.writes().count((0x0d12,507)),1)
        self.assertTrue(await self.driver.release_bus_enable())

    async def test_hold_communication_error_is_not_settling(self):
        original=self.driver.hold_home_di3
        async def fail(given,*args,**kwargs):
            if given==511:raise TimeoutError('feedback unavailable')
            return await original(given,*args,**kwargs)
        with patch.object(self.driver,'hold_home_di3',side_effect=fail):
            with self.assertRaises(TimeoutError):await self.driver.release_bus_enable()
        self.assertEqual(self.driver.home_cleanup_serial,0)
        self.assertTrue(await self.driver.release_bus_enable())
        self.assertEqual(self.writes().count((0x0d12,507)),1)

    async def test_disable_motion_preserves_minimum_hold_then_exits_without_reenable(self):
        self.cfg.home_disable_hold_s=.12
        original=self.driver._read_words
        first_motion_at=None
        async def movement(address,count):
            nonlocal first_motion_at
            if address==0x0b00 and self.driver._bus_cleanup_tail and self.driver._bus_cleanup_tail[1]=='hold':
                if first_motion_at is None:first_motion_at=time.monotonic()
                return [2]
            return await original(address,count)
        with patch.object(self.driver,'_read_words',side_effect=movement):
            with self.assertRaises(StopNotSettled):await self.driver.release_bus_enable()
        self.assertIsNotNone(first_motion_at)
        self.assertGreaterEqual(time.monotonic()-first_motion_at,.10)
        self.assertEqual(self.sim.regs[0x0d11],0)
        self.assertTrue(any(x['kind']=='home_di3_hold_elapsed' and x.get('given')==511 for x in self.trace.items))
        self.assertTrue(await self.driver.release_bus_enable())
        self.assertEqual(self.writes().count((0x0d12,507)),1)
