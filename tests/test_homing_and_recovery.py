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
        self.cfg=Config(mode='sim',home_enable_hold_s=0,home_disable_hold_s=0, state_file=str(Path(self.tmp.name)/'zero.json'),poll_s=.02,
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
        for command in ({'action':'goto','target_cm':2},):
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

    async def test_home_then_repeated_jog_preserves_reference_without_home_cleanup_writes(self):
        await self.home()
        self.assertFalse(self.ctrl.driver.home_enable_owned)
        self.assertEqual(self.sim.regs[0x0307],0)
        self.cfg.deadman_s=2
        self.svc.trace.clear()
        for i,direction in enumerate(('up','up','down','up')):
            before=self.sim.physical
            await self.ctrl.command(dict(action=direction,intent_id='after-home-%s'%i))
            await until(lambda:abs(self.sim.physical-before)>30)
            self.assertEqual(self.sim.motion,direction)
            self.assertGreater((self.sim.physical-before)*(1 if direction=='up' else -1),0)
            await self.ctrl.stop()
            self.assertTrue(self.ctrl.armed)
            self.assertTrue(self.ctrl.trusted)
            self.assertFalse(self.ctrl.pending_stop)
            self.assertEqual(self.ctrl.stop_confirmation,'ack')
        writes=self.writes()
        self.assertFalse(any(a in (0x0307,0x0D08,0x1003,0x0D11,0x0D12) for a,v in writes))
        self.assertNotIn((0x0D0B,65),writes)
        self.assertEqual(self.ctrl.home_status,'complete')
        self.assertEqual(self.ctrl.zero,self.cfg.home_reference_pulse)

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

    async def test_pp_rejects_outside_bounds_but_jog_ignores_software_limit(self):
        await self.home()
        await self.retract()
        for target in (.9,4.1):
            with self.assertRaises(LabError):
                await self.ctrl.command({'action':'goto','target_cm':target})
        self.cfg.deadman_s=5
        await self.ctrl.command({'action':'up','intent_id':'boundary'})
        await until(lambda:self.ctrl.snapshot()['details']['height_cm']>self.cfg.soft_upper_cm,timeout=4)
        self.assertEqual(self.ctrl.mode,'pv')
        await self.ctrl.stop()
        self.assertIsNone(self.sim.motion)

    async def test_speed_settings_are_independent_and_restore_without_arming(self):
        await self.home()
        await self.retract()
        await self.ctrl.command({'action':'set_speed','mode':'pp','speed_cm_s':.7})
        await self.ctrl.command({'action':'set_speed','mode':'pv','speed_cm_s':.35})
        await self.ctrl.command({'action':'up','intent_id':'speed'})
        self.assertEqual(self.sim.regs[0x4604],60)
        await self.ctrl.command({'action':'set_speed','mode':'pp','speed_cm_s':1.05})
        self.assertEqual(self.sim.regs[0x4604],60)
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
        self.sim.inject('write_failure',0x4604)
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

    async def test_pp_alarm_is_advisory_and_manual_reset_does_not_restart_or_trust(self):
        await self.home()
        await self.retract()
        await self.ctrl.command({'action':'goto','target_cm':3})
        self.sim.inject('alarm',0xB00)
        await self.ctrl.observe()
        self.assertTrue(self.ctrl.armed)
        self.assertFalse(self.ctrl.pending_stop)
        self.assertFalse(self.ctrl.trusted)
        await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
        self.assertEqual(self.sim.regs[0x0B22],0)
        self.assertFalse(self.ctrl.armed)
        self.assertFalse(self.ctrl.trusted)
        self.assertIsNone(self.ctrl.mode)
        self.assertIn((0x0D01,1),self.writes())

    async def test_any_alarm_allows_one_explicit_reset_attempt(self):
        for alarm in (0x104,0x731,0x1234):
            self.sim.inject('alarm',alarm)
            await self.ctrl.observe()
            self.svc.trace.items.clear()
            await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
            self.assertEqual(self.writes().count((0x0D01,1)),1)

    async def test_latched_expected_home_limit_can_be_reset_after_release(self):
        self.sim.inject('alarm',0x952)
        await self.home()
        self.assertFalse(self.ctrl.sample['lower_limit'])
        self.assertEqual(self.ctrl.sample['alarm_code'],0x952)
        await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
        self.assertEqual(self.sim.regs[0x0B22],0)
        self.assertTrue(self.ctrl.armed)
        self.assertIsNone(self.sim.motion)

    async def test_active_limit_allows_reset_attempt_without_bypassing_direction(self):
        self.sim.inject('alarm',0x952)
        self.svc.trace.items.clear()
        await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
        self.assertIn((0x0D01,1),self.writes())
        from lab.alarms import jog_blocked
        self.assertTrue(jog_blocked(await self.ctrl.observe(),'down'))

    async def test_alarm_change_is_logged_but_does_not_gate_explicit_reset(self):
        self.sim.inject('alarm',0x620)
        original=self.ctrl.driver.disable
        async def changed_alarm(checkpoint=lambda:None):
            await original()
            self.sim.inject('alarm',0x104)
        with patch.object(self.ctrl.driver,'disable',side_effect=changed_alarm):
            await self.ctrl.command({'action':'reset_fault','confirm_reset':True})
        self.assertEqual(self.writes().count((0x0D01,1)),1)

    async def test_reset_ack_without_clearing_alarm_reports_remaining_alarm(self):
        self.sim.inject('alarm',0x620)
        self.sim.inject('alarm_persistent',True)
        await self.ctrl.command({'action':'reset_fault'})
        self.assertTrue(self.ctrl.operator_result['write_ack'])
        self.assertFalse(self.ctrl.operator_result['cleared'])
        self.assertEqual(self.ctrl.operator_result['read_value'],0x620)

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


    async def test_601_reset_does_not_run_disable_sequence(self):
        self.sim.inject('alarm',0xE601)
        self.ctrl.driver.home_enable_owned=True
        self.sim.regs[0x0307]=1
        self.svc.trace.clear()
        await self.ctrl.command({'action':'reset_fault'})
        self.assertEqual(self.writes(),[(0x0D01,1)])
        self.assertEqual(self.ctrl.operator_result['read_value'],self.sim.regs[0x0B22])

    async def test_601_uncleared_alarm_is_reported_without_retry(self):
        self.sim.inject('alarm',0xE601)
        self.sim.inject('alarm_persistent',True)
        self.svc.trace.clear()
        await self.ctrl.command({'action':'reset_fault'})
        self.assertEqual(self.writes(),[(0x0D01,1)])
        self.assertFalse(self.ctrl.operator_result['cleared'])
        self.assertEqual(self.ctrl.operator_result['read_value'],0xE601)

    async def test_reset_has_no_di3_or_preflight_dependency(self):
        self.sim.inject('alarm',0xE601)
        with patch.object(self.ctrl.driver,'preflight',side_effect=AssertionError('unexpected preflight')), patch.object(self.ctrl,'_wait_for_stop',side_effect=AssertionError('unexpected stop')):
            await self.ctrl.command({'action':'reset_fault'})
        self.assertEqual(self.ctrl.operator_result['write_ack'],True)

    async def test_reset_keeps_emergency_without_gating_reset(self):
        self.ctrl.emergency=True
        await self.ctrl.command({'action':'reset_fault'})
        self.assertTrue(self.ctrl.emergency)
        self.assertFalse(self.ctrl.armed)
        self.assertTrue(self.ctrl.operator_result['write_ack'])

    async def test_local_home_timeout_recovers_without_inventing_driver_601(self):
        self.sim.inject('home_never_complete',True)
        await self.ctrl.command({'action':'home','confirm_homing':True})
        await until(lambda:bool(self.ctrl.fault))
        await until(lambda:not self.ctrl.pending_stop)
        self.assertEqual(self.sim.regs[0x0B22],0)
        self.assertFalse(self.ctrl.trusted)
        self.assertNotIn((0x0D01,1),self.writes())
        self.sim.inject('home_never_complete',False)
        await self.ctrl.arm()
        await self.home()


    async def test_reset_retires_home_without_implicit_cleanup(self):
        self.ctrl.driver.home_enable_owned=True
        self.ctrl.mode='home'
        self.ctrl.home_phase='settling'
        self.svc.trace.clear()
        await self.ctrl.command({'action':'reset_fault'})
        self.assertEqual(self.writes(),[(0x0D01,1)])
        self.assertIsNone(self.ctrl.mode)
        self.assertFalse(self.ctrl.armed)

    async def test_reset_does_not_require_parameter_scan(self):
        with patch.object(self.ctrl.driver,'read_parameters',side_effect=AssertionError('unexpected scan')):
            await self.ctrl.command({'action':'reset_fault'})
        self.assertTrue(self.ctrl.operator_result['cleared'])


    async def test_601_automatic_stop_preserves_alarm_until_explicit_reset(self):
        self.ctrl.mode='home'
        self.ctrl.state='HOMING'
        self.ctrl.driver.home_enable_owned=True
        self.sim.regs[0x0307]=1
        self.sim.inject('alarm',0xE601)
        self.svc.trace.clear()
        await self.ctrl.stop()
        self.assertEqual(self.sim.regs[0x0B22],0xE601)
        self.assertFalse(self.ctrl.pending_stop)
        self.assertIsNone(self.ctrl.mode)
        self.assertFalse(self.ctrl.armed)
        self.assertIn('故障复位',self.ctrl.fault)
        self.assertFalse(any(a in (0x0307,0x0D11,0x0D12,0x0D01) for a,v in self.writes()))
        self.svc.trace.clear()
        await self.ctrl.command({'action':'reset_fault'})
        self.assertEqual(self.writes(),[(0x0D01,1)])
        self.assertFalse(self.ctrl.armed)

    async def test_reset_without_alarm_still_sends_requested_command(self):
        self.sim.regs[0x0B22]=0
        self.svc.trace.clear()
        await self.ctrl.command({'action':'reset_fault'})
        self.assertEqual(self.writes(),[(0x0D01,1)])
        self.assertTrue(self.ctrl.operator_result['cleared'])


if __name__=='__main__':
    unittest.main()
