from __future__ import annotations
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.model import Config, LabError
from lab.service import Service


async def until(predicate,timeout=5):
    deadline=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>deadline:
            raise AssertionError('condition timed out')
        await asyncio.sleep(.02)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.cfg=Config(state_file=str(Path(self.temp.name)/'zero.json'),soft_lower_cm=0,poll_s=.02,deadman_s=.3,startup_grace_s=.15,stall_s=.1,stop_timeout_s=.3,bridge_enabled=True,bridge_token='test')
        self.service=Service(self.cfg)
        self.ctrl=self.service.controller
        self.sim=self.service.transport
        await self.service.start()
        await until(lambda:bool(self.ctrl.sample))
        await self.ctrl.arm()
        await self.calibrate()

    async def asyncTearDown(self):
        self.sim.faults.update(offline=False,lost_stop=0,delay_ms=0,stall=False,write_failure=-1)
        await self.service.close()
        self.temp.cleanup()

    async def calibrate(self):
        await self.ctrl.command(dict(action='home',confirm_homing=True))
        await until(lambda:self.ctrl.trusted and not self.ctrl.pending_stop,timeout=5)
        return self.ctrl.snapshot()

    async def jog(self,identity='a'):
        return await self.ctrl.command(dict(action='up',intent_id=identity))

    async def test_jog_heartbeat_stop_and_physical_height(self):
        async def hold():
            # Like the page's pointer hold, renew during preparation as well
            # as motion; preparation now includes complete parameter checks.
            while True:
                if self.ctrl.intent=='a' and self.ctrl.mode=='pv' and self.ctrl.armed and not self.ctrl.pending_stop:
                    await self.ctrl.command(dict(action='heartbeat',intent_id='a'))
                await asyncio.sleep(.05)
        heartbeat=asyncio.create_task(hold())
        try:
            await self.jog()
            await asyncio.sleep(.36)
            self.assertEqual(self.ctrl.state,'MOVING')
            self.assertGreater(self.sim.physical,0)
            await self.ctrl.stop()
            self.assertIsNone(self.sim.motion)
            self.assertFalse(self.ctrl.pending_stop)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat,return_exceptions=True)

    async def test_deadman_stops_without_status_request(self):
        await self.jog()
        await until(lambda:self.sim.motion is None)
        self.assertIsNone(self.ctrl.intent)
        self.assertIn('点动保活超时',self.ctrl.snapshot()['details']['stop_reason'])
        with self.assertRaisesRegex(LabError,'点动保活超时'):
            await self.ctrl.command(dict(action='heartbeat',intent_id='a'))

    async def test_pv_preparation_does_not_consume_first_motion_heartbeat_window(self):
        self.cfg.deadman_s=.1
        original=self.ctrl.driver.start
        async def slow_preparation(*args):
            await asyncio.sleep(.16)
            return await original(*args)
        with patch.object(self.ctrl.driver,'start',side_effect=slow_preparation):
            await self.jog('slow-prepare')
        self.assertEqual(self.ctrl.state,'MOVING')
        self.assertFalse(self.ctrl.pending_stop)
        await until(lambda:self.sim.motion is None)
        self.assertIsNone(self.ctrl.intent)

    async def test_lost_start_ack_remains_stop_pending_until_confirmed(self):
        self.sim.inject('lost_start_ack')
        with self.assertRaises(LabError):
            await self.jog()
        await until(lambda:not self.ctrl.pending_stop)
        self.assertIsNone(self.sim.motion)
        self.assertFalse(self.ctrl.armed)

    async def test_stop_failure_is_retried(self):
        await self.jog()
        self.sim.inject('lost_stop',3)
        await self.ctrl.stop()
        self.assertEqual(self.sim.faults['lost_stop'],0)
        self.assertIsNone(self.sim.motion)

    async def test_stop_offline_is_unknown_and_recovers_without_restart(self):
        await self.jog()
        self.sim.inject('offline')
        with self.assertRaises(LabError):
            await self.ctrl.stop()
        self.assertTrue(self.ctrl.pending_stop)
        self.assertEqual(self.ctrl.snapshot()['details']['motion'],'unknown')
        self.sim.inject('offline',False)
        await until(lambda:not self.ctrl.pending_stop)
        self.assertIsNone(self.sim.motion)
        self.assertFalse(self.ctrl.armed)

    async def test_old_heartbeat_and_delayed_start_rejected(self):
        await self.jog()
        await self.ctrl.command(dict(action='stop',intent_id='a'))
        for body in [dict(action='heartbeat',intent_id='a'),dict(action='up',intent_id='a')]:
            with self.assertRaises(LabError):
                await self.ctrl.command(body)
        await self.ctrl.command(dict(action='stop',intent_id='not-yet-delivered'))
        with self.assertRaises(LabError):
            await self.jog('not-yet-delivered')
        self.assertIsNone(self.sim.motion)

    async def test_stop_interrupts_setup_before_start_register(self):
        self.sim.inject('delay_ms',15)
        moving=asyncio.create_task(self.jog())
        await asyncio.sleep(.04)
        await self.ctrl.stop(True)
        with self.assertRaises(LabError):
            await moving
        self.assertIsNone(self.sim.motion)
        self.assertFalse(self.ctrl.armed)

    async def test_arbitrary_zero_setting_is_not_a_homing_substitute(self):
        before=self.ctrl.zero
        with self.assertRaises(LabError):
            await self.ctrl.command(dict(action='set_retracted_zero',confirm_retracted=True))
        self.assertEqual(self.ctrl.zero,before)

    async def test_home_requires_explicit_confirmation(self):
        with self.assertRaises(LabError):
            await self.ctrl.command(dict(action='home'))
        self.assertIsNone(self.sim.motion)

    async def test_power_cycle_invalidates_even_small_height(self):
        await self.calibrate()
        self.sim.inject('physical_cm',1)
        await self.ctrl.observe()
        self.sim.inject('power_cycle')
        await self.ctrl.observe()
        self.assertFalse(self.ctrl.trusted)
        with self.assertRaises(LabError):
            await self.ctrl.command(dict(action='goto',height=5))

    async def test_goto_uses_fresh_external_position_without_invalidating_reference(self):
        await self.calibrate()
        self.sim.inject('physical_cm',20)
        await self.ctrl.command(dict(action='goto',height=10))
        self.assertTrue(self.ctrl.trusted)
        self.assertEqual(self.ctrl.direction,'down')
        self.assertEqual(self.ctrl.target_cm,18)

    async def test_goto_reaches_target_and_bounds_are_enforced(self):
        await self.calibrate()
        await self.ctrl.command(dict(action='set_speed',mode='pp',speed_cm_s=self.cfg.cm(self.cfg.max_pp_speed)))
        await self.ctrl.command(dict(action='goto',height=.2/self.cfg.travel_cm*100))
        await until(lambda:self.ctrl.mode is None)
        self.assertAlmostEqual(self.cfg.cm(self.sim.physical+self.sim.counter_offset-self.cfg.home_reference_pulse),.2,places=2)
        with self.assertRaises(LabError):
            await self.ctrl.command(dict(action='max'))

    async def test_zero_speed_never_falls_back_to_default(self):
        await self.ctrl.command(dict(action='set_speed',mode='pv',speed_cm_s=0))
        with self.assertRaises(LabError):
            await self.jog()
        self.assertEqual(self.cfg.speed(0),0)
        self.assertIsNone(self.sim.motion)

    async def test_running_speed_change_writes_register(self):
        await self.jog()
        await self.ctrl.command(dict(action='set_speed',mode='pv',speed_cm_s=.35))
        self.assertEqual(self.sim._i32(0x102A),10000)
        self.assertEqual(self.ctrl.applied_speed,10000)

    async def test_stall_from_start_latches_fault(self):
        self.cfg.deadman_s=2
        self.sim.inject('stall')
        await self.jog()
        await until(lambda:not self.ctrl.armed)
        await until(lambda:not self.ctrl.pending_stop)
        self.assertIsNone(self.sim.motion)
        self.assertIn('堵转',self.ctrl.fault)

    async def test_live_lower_limit_rejects_down(self):
        with self.assertRaises(LabError):
            await self.ctrl.command(dict(action='down',intent_id='lower'))

    async def test_arm_during_motion_cannot_disable_watchdog(self):
        await self.jog()
        with self.assertRaises(LabError):
            await self.ctrl.arm()
        self.assertEqual(self.ctrl.state,'MOVING')
        await until(lambda:self.sim.motion is None)

    async def test_early_release_is_cancellation_not_fault(self):
        self.sim.inject('delay_ms',10)
        task=asyncio.create_task(self.jog())
        await asyncio.sleep(.02)
        await self.ctrl.command(dict(action='stop',intent_id='a'))
        with self.assertRaises(LabError):
            await task
        self.assertEqual(self.ctrl.fault,'')
        self.assertTrue(self.ctrl.armed)
        self.assertIsNone(self.sim.motion)

    async def test_full_travel_max_min_with_explicit_limit(self):
        self.cfg.travel_cm=1
        self.cfg.soft_upper_cm=1
        await self.calibrate()
        await self.ctrl.command(dict(action='set_speed',mode='pp',speed_cm_s=self.cfg.cm(self.cfg.max_pp_speed)))
        await self.ctrl.command(dict(action='max'))
        await until(lambda:self.ctrl.mode is None)
        self.assertAlmostEqual(self.cfg.cm(self.sim.physical+self.sim.counter_offset-self.cfg.home_reference_pulse),1,places=2)
        await self.ctrl.command(dict(action='min'))
        await until(lambda:self.ctrl.mode is None)
        self.assertAlmostEqual(self.cfg.cm(self.sim.physical+self.sim.counter_offset-self.cfg.home_reference_pulse),0,places=2)

    async def test_partial_pp_failure_reprepares_pv_mode(self):
        await self.calibrate()
        original=self.ctrl.driver._write_int32
        target=self.cfg.pulse(1)
        async def fail_after_enable(address,value):
            if address==0x100E and value==target:
                raise TimeoutError('PP target after enable failed')
            return await original(address,value)
        with patch.object(self.ctrl.driver,'_write_int32',fail_after_enable):
            with self.assertRaises(LabError):
                await self.ctrl.command(dict(action='goto',height=1/self.cfg.travel_cm*100))
        await until(lambda:not self.ctrl.pending_stop)
        self.assertEqual(self.sim.regs[0x1003],1)
        await self.ctrl.arm()
        if not self.ctrl.trusted:
            await self.calibrate()
        await self.jog('after-pp-failure')
        self.assertEqual(self.sim.regs[0x1003],3)

    async def test_preflight_refuses_eeprom_and_wrong_di(self):
        self.sim.regs[0x0C0D]=1
        with self.assertRaises(LabError):
            await self.ctrl.arm()
        self.sim.regs[0x0C0D]=0
        self.sim.regs[0x0307]=1
        with self.assertRaises(LabError):
            await self.ctrl.arm()

    async def test_any_local_client_can_explicitly_estop_shared_control(self):
        await self.service.acquire('a','local','A')
        await self.jog()
        result=await self.service.request('POST','/api/devices/lift_pole_1/lift/control',{'action':'estop'},{'X-Patrol-Client-Id':'b'})
        self.assertTrue(result['ok'])
        self.assertIsNone(self.sim.motion)

    async def test_bridge_auth_and_hidden_actions_without_exclusive_owner(self):
        with self.assertRaises(LabError) as ctx:
            await self.service.request('GET','/api/bridge/v1/health')
        self.assertEqual(ctx.exception.status,401)
        headers={'X-Patrol-Bridge-Token':'test'}
        await self.service.request('POST','/api/bridge/v1/lift/lease/acquire',{},headers)
        await self.service.acquire('local','local','Local')
        result=await self.service.request('POST','/api/bridge/v1/lift/control',{'action':'up','intent_id':'central'},headers)
        self.assertTrue(result['ok'])
        self.assertEqual(self.sim.motion,'up')
        with self.assertRaises(LabError) as ctx:
            await self.service.request('POST','/api/bridge/v1/lift/control',{'action':'set_retracted_zero'},headers)
        self.assertEqual(ctx.exception.status,400)

    async def test_compatibility_lease_and_idle_client_do_not_stop_shared_motion(self):
        self.cfg.lease_s=.01
        await self.service.acquire('a','local','A')
        await self.ctrl.command(dict(action='goto',target_cm=1))
        for client in ('b','c'):
            headers={'X-Patrol-Client-Id':client}
            for endpoint in ('acquire','renew','release'):
                result=await self.service.request('POST','/api/devices/lift_pole_1/lease/'+endpoint,{},headers)
                self.assertTrue(result['can_control'])
                self.assertFalse(result['enabled'])
        await asyncio.sleep(.1)
        self.assertEqual(self.ctrl.state,'MOVING')
        self.assertEqual(self.sim.motion,'pp')
        self.assertIsNone(self.service.lease)

    async def test_local_command_needs_no_lease_or_client_identity(self):
        result=await self.service.request('POST','/api/control',{'action':'goto','target_cm':1})
        self.assertTrue(result['ok'])
        self.assertEqual(self.sim.motion,'pp')


if __name__=='__main__':
    unittest.main()
