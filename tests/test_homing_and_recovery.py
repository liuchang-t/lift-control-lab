import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.model import Config, LabError
from lab.service import Service


async def until(predicate, timeout=5):
    deadline=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>deadline:
            raise AssertionError('condition timed out')
        await asyncio.sleep(.02)


class HomingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cfg=Config(state_file=str(Path(self.tmp.name)/'zero.json'),poll_s=.02,
                        travel_cm=5,soft_upper_cm=4,max_move_pulse=1000000,
                        deadman_s=.3,home_timeout_s=1.5,home_start_timeout_s=.4,
                        stop_timeout_s=.4,control_latency_s=.1,limit_margin_cm=.05)
        self.svc=Service(self.cfg)
        self.ctrl=self.svc.controller
        self.sim=self.svc.transport
        await self.svc.start()
        await until(lambda:bool(self.ctrl.sample))
        await self.ctrl.arm()

    async def asyncTearDown(self):
        self.sim.faults.update(offline=False,lost_stop=0,delay_ms=0,stall=False,
                               write_failure=-1,home_never_complete=False,
                               home_stale_complete=False,reset_failure=False)
        await self.svc.close()
        self.tmp.cleanup()

    async def home(self):
        await self.ctrl.command({'action':'home','confirm_homing':True})
        await until(lambda:self.ctrl.trusted and not self.ctrl.pending_stop)

    async def retract(self):
        await self.ctrl.command({'action':'min'})
        await until(lambda:self.ctrl.mode is None)

    def writes(self):
        frames=[bytes.fromhex(x['frame']) for x in self.svc.trace.items if x['kind']=='tx']
        return [(int.from_bytes(x[2:4],'big'),int.from_bytes(x[4:6],'big')) for x in frames if x[1] in (6,16)]

    async def test_unreferenced_normal_movement_is_rejected(self):
        for command in ({'action':'up','intent_id':'unhomed'},{'action':'goto','target_cm':2}):
            with self.assertRaises(LabError):
                await self.ctrl.command(command)
        self.assertIsNone(self.sim.motion)

    async def test_home_17_uses_driver_reference_then_full_retract_is_one_cm(self):
        await self.home()
        self.assertEqual(self.sim.regs[0x1023],17)
        self.assertIn((0x0D08,128),self.writes())
        self.assertEqual(self.ctrl.zero,self.cfg.home_reference_pulse)
        self.assertFalse(self.ctrl.sample['lower_limit'])
        await self.retract()
        status=self.ctrl.snapshot()['details']
        self.assertAlmostEqual(status['height_cm'],1,places=2)
        self.assertTrue(status['position_trusted'])
        prohibited={0x0C0D,0x0A28,0x0A29,0x0A2B,0x1012,0x1014,0x0D14}
        self.assertFalse(any(address in prohibited for address,_ in self.writes()))

    async def test_stale_home_complete_cannot_establish_new_reference(self):
        self.sim.inject('home_stale_complete',True)
        try:
            await self.ctrl.command({'action':'home','confirm_homing':True})
        except LabError:
            pass
        await until(lambda:not self.ctrl.armed)
        self.assertFalse(self.ctrl.trusted)
        await until(lambda:not self.ctrl.pending_stop)
        self.assertIsNone(self.sim.motion)

    async def test_home_timeout_stops_and_leaves_reference_unknown(self):
        self.sim.inject('home_never_complete',True)
        await self.ctrl.command({'action':'home','confirm_homing':True})
        await until(lambda:not self.ctrl.armed)
        await until(lambda:not self.ctrl.pending_stop)
        self.assertFalse(self.ctrl.trusted)
        self.assertIsNone(self.sim.motion)

    async def test_stop_cancels_homing_and_late_completion_is_not_trusted(self):
        self.sim.inject('home_never_complete',True)
        await self.ctrl.command({'action':'home','confirm_homing':True})
        await self.ctrl.stop()
        self.sim.inject('home_never_complete',False)
        await asyncio.sleep(.15)
        self.assertFalse(self.ctrl.trusted)
        self.assertIsNone(self.sim.motion)

    async def test_home_record_save_failure_does_not_establish_trusted_reference(self):
        with patch.object(self.ctrl.store,'save',side_effect=OSError('disk full')):
            await self.ctrl.command({'action':'home','confirm_homing':True})
            await until(lambda:not self.ctrl.armed)
        await until(lambda:not self.ctrl.pending_stop)
        self.assertFalse(self.ctrl.trusted)
        self.assertIsNone(self.sim.motion)

    async def test_pp_rejects_outside_bounds_and_pv_stops_before_upper_limit(self):
        await self.home()
        await self.retract()
        for target in (.9,4.1):
            with self.assertRaises(LabError):
                await self.ctrl.command({'action':'goto','target_cm':target})
        self.cfg.deadman_s=5
        await self.ctrl.command({'action':'up','intent_id':'boundary'})
        await until(lambda:self.ctrl.mode is None,timeout=6)
        height=self.ctrl.snapshot()['details']['height_cm']
        self.assertLessEqual(height,self.cfg.soft_upper_cm)
        self.assertFalse(self.ctrl.sample['upper_limit'])

    async def test_speed_settings_are_independent_and_restore_without_arming(self):
        await self.home()
        await self.retract()
        await self.ctrl.command({'action':'set_speed','mode':'pp','speed_cm_s':.7})
        await self.ctrl.command({'action':'set_speed','mode':'pv','speed_cm_s':.35})
        await self.ctrl.command({'action':'up','intent_id':'speed'})
        self.assertEqual(self.sim._i32(0x102A),10000)
        await self.ctrl.command({'action':'set_speed','mode':'pp','speed_cm_s':1.05})
        self.assertEqual(self.sim._i32(0x102A),10000)
        await self.ctrl.stop()
        restarted=Service(self.cfg)
        self.assertEqual(restarted.controller.speeds,{'pp':1.05,'pv':.35})
        self.assertFalse(restarted.controller.armed)
        self.assertFalse(restarted.controller.trusted)
        self.assertIsNone(restarted.transport.motion)

    async def test_running_speed_write_failure_stops_without_claiming_applied_value(self):
        await self.home()
        await self.retract()
        await self.ctrl.command({'action':'up','intent_id':'speed-failure'})
        self.sim.inject('write_failure',0x102A)
        with self.assertRaises((LabError,TimeoutError)):
            await self.ctrl.command({'action':'set_speed','mode':'pv','speed_cm_s':.35})
        await until(lambda:not self.ctrl.pending_stop)
        self.assertFalse(self.ctrl.armed)
        self.assertIsNone(self.sim.motion)

    async def test_speed_save_failure_is_explicit_and_old_value_survives(self):
        previous=dict(self.ctrl.speeds)
        with patch.object(self.ctrl.speed_store,'save',side_effect=OSError('disk full')):
            with self.assertRaises((LabError,OSError)):
                await self.ctrl.command({'action':'set_speed','mode':'pv','speed_cm_s':.35})
        self.assertEqual(self.ctrl.speeds,previous)

    async def test_alarm_stops_pp_and_manual_reset_does_not_restart_or_trust(self):
        await self.home()
        await self.retract()
        await self.ctrl.command({'action':'goto','target_cm':3})
        self.sim.inject('alarm',0xB00)
        await until(lambda:not self.ctrl.armed)
        await until(lambda:not self.ctrl.pending_stop)
        self.assertFalse(self.ctrl.trusted)
        with self.assertRaises(LabError):
            await self.ctrl.command({'action':'reset_fault'})
        await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
        self.assertEqual(self.sim.regs[0x0B22],0)
        self.assertFalse(self.ctrl.armed)
        self.assertFalse(self.ctrl.trusted)
        self.assertIsNone(self.sim.motion)
        self.assertIn((0x0D01,1),self.writes())

    async def test_nonresettable_and_unknown_alarms_do_not_receive_reset_write(self):
        for alarm in (0x104,0x731,0x1234):
            self.sim.inject('alarm',alarm)
            await self.ctrl.observe()
            self.svc.trace.items.clear()
            with self.assertRaises(LabError):
                await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
            self.assertNotIn((0x0D01,1),self.writes())

    async def test_latched_expected_home_limit_can_be_reset_after_release(self):
        self.sim.inject('alarm',0x952)
        await self.home()
        self.assertFalse(self.ctrl.sample['lower_limit'])
        self.assertEqual(self.ctrl.sample['alarm_code'],0x952)
        await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
        self.assertEqual(self.sim.regs[0x0B22],0)
        self.assertFalse(self.ctrl.armed)

    async def test_active_limit_cannot_be_cleared_to_bypass_switch(self):
        self.sim.inject('alarm',0x952)
        self.svc.trace.items.clear()
        with self.assertRaises(LabError):
            await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
        self.assertNotIn((0x0D01,1),self.writes())
        self.assertEqual(self.sim.regs[0x0B22],0x952)

    async def test_alarm_change_during_reset_setup_prevents_reset_write(self):
        self.sim.inject('alarm',0x620)
        original=self.ctrl.driver.disable
        async def changed_alarm():
            await original()
            self.sim.inject('alarm',0x104)
        with patch.object(self.ctrl.driver,'disable',side_effect=changed_alarm):
            with self.assertRaises(LabError):
                await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
        self.assertNotIn((0x0D01,1),self.writes())

    async def test_reset_ack_without_clearing_alarm_is_not_success(self):
        self.sim.inject('alarm',0x620)
        self.sim.inject('alarm_persistent',True)
        with self.assertRaises(LabError):
            await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
        self.assertEqual(self.sim.regs[0x0B22],0x620)
        self.assertFalse(self.ctrl.armed)

    async def test_zero_speed_stops_even_when_saving_fails(self):
        await self.home()
        await self.retract()
        await self.ctrl.command({'action':'up','intent_id':'zero-save-failure'})
        with patch.object(self.ctrl.speed_store,'save',side_effect=OSError('disk full')):
            with self.assertRaises((LabError,OSError)):
                await self.ctrl.command({'action':'set_speed','mode':'pv','speed_cm_s':0})
        await until(lambda:not self.ctrl.pending_stop)
        self.assertIsNone(self.sim.motion)

    async def test_eeprom_setting_change_during_motion_requests_stop(self):
        await self.home()
        await self.retract()
        await self.ctrl.command({'action':'up','intent_id':'eeprom-changed'})
        self.sim.regs[0x0C0D]=1
        await until(lambda:not self.ctrl.armed)
        await until(lambda:not self.ctrl.pending_stop)
        self.assertIsNone(self.sim.motion)
        self.assertEqual(self.sim.regs[0x0C0D],1)
        self.assertFalse(any(address==0x0C0D for address,_ in self.writes()))

    async def test_parameter_request_uses_cache_during_motion(self):
        await self.home()
        await self.retract()
        await self.ctrl.command({'action':'up','intent_id':'parameter-cache'})
        with patch.object(self.ctrl.driver,'read_parameters',side_effect=AssertionError('must use cache')):
            result=await self.svc.request('GET','/api/parameters')
        self.assertTrue(result)
        self.assertEqual(self.ctrl.state,'MOVING')

    async def test_stop_interrupts_slow_speed_persistence_before_register_write(self):
        await self.home()
        await self.retract()
        await self.ctrl.command({'action':'up','intent_id':'slow-save'})
        previous=self.sim._i32(0x102A)
        original=self.ctrl.speed_store.save
        def slow_save(values):
            time.sleep(.2)
            original(values)
        with patch.object(self.ctrl.speed_store,'save',side_effect=slow_save):
            setting=asyncio.create_task(self.ctrl.command({'action':'set_speed','mode':'pv','speed_cm_s':.35}))
            await asyncio.sleep(.03)
            await self.ctrl.stop()
            with self.assertRaises(LabError):
                await setting
        self.assertEqual(self.sim._i32(0x102A),previous)
        self.assertIsNone(self.sim.motion)


if __name__=='__main__':
    unittest.main()
