import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.model import Config, LabError
from lab.service import Service

async def until(predicate, timeout=4):
    deadline=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>deadline: raise AssertionError('timed out')
        await asyncio.sleep(.01)

class PositionControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cfg=Config(mode='sim',state_file=str(Path(self.tmp.name)/'state.json'),home_enable_hold_s=0,home_disable_hold_s=0,
            travel_cm=5,soft_lower_cm=.1,soft_upper_cm=4.5,poll_s=.02,stop_timeout_s=.3,
            startup_grace_s=.1,stall_s=.1,deadman_s=.3,max_move_pulse=1)
        self.s=Service(self.cfg);self.c=self.s.controller;self.sim=self.s.transport
        await self.s.start()
        await until(lambda:bool(self.c.sample))
        await self.c.arm()
        await self.c.command({'action':'home','confirm_homing':True})
        await until(lambda:self.c.trusted and not self.c.pending_stop)
        self.c.speeds['pp']=self.cfg.cm(self.cfg.max_pp_speed)
        self.s.trace.clear()

    async def asyncTearDown(self):
        self.sim.faults.update(offline=False,stall=False,lost_stop=0,write_failure=-1)
        await self.s.close();self.tmp.cleanup()

    def writes(self):
        fs=[bytes.fromhex(x['frame']) for x in self.s.trace.items if x['kind']=='tx']
        return [(int.from_bytes(f[2:4],'big'),int.from_bytes(f[4:6],'big')) for f in fs if f[1]==6]

    async def test_real_mode_on_simulator_full_range_and_continuous_targets_then_jog(self):
        self.cfg.mode='real'  # Existing simulator transport; no network connection.
        for action,target in [('max',4.5),('min',.1),('goto',1.0)]:
            await self.c.command({'action':action,'target_cm':target})
            await until(lambda:self.c.mode is None and not self.c.pending_stop)
            self.assertEqual(self.c.position_status,'complete')
            self.assertAlmostEqual(self.c.snapshot()['details']['height_cm'],target,delta=self.cfg.pp_tolerance_cm)
            self.assertTrue(self.c.trusted)
            self.assertFalse(self.sim.enabled)
        await self.c.command({'action':'up','intent_id':'after-targets'})
        self.assertEqual(self.sim.regs[0x0d0b],17)
        await self.c.command({'action':'stop','intent_id':'after-targets'})
        await self.c.command({'action':'down','intent_id':'again'})
        self.assertEqual(self.sim.regs[0x0d0b],33)

    async def test_noop_requires_stationary_feedback_and_sends_no_start(self):
        self.sim.inject('physical_cm',1)
        await self.c.observe()
        height=self.c.snapshot()['details']['height_cm']
        self.s.trace.clear()
        await self.c.command({'action':'goto','target_cm':height+.01})
        self.assertEqual(self.c.position_status,'already_at_target')
        self.assertEqual(self.writes(),[])

    async def test_outside_range_returns_inward_with_saved_pp_speed(self):
        for current,target in [(0,.1),(4.8,4.5)]:
            self.sim.inject('physical_cm',current)
            await self.c.observe();self.c.trusted=True
            await self.c.command({'action':'goto','target_cm':target})
            self.assertEqual(self.c.applied_speed,self.cfg.max_pp_speed)
            await until(lambda:self.c.mode is None and not self.c.pending_stop)
            self.assertEqual(self.c.position_status,'complete')

    async def test_out_of_range_and_untrusted_targets_do_not_start(self):
        for target in (-1,5):
            with self.assertRaises(LabError):await self.c.command({'action':'goto','target_cm':target})
        self.c.trusted=False
        with self.assertRaises(LabError):await self.c.command({'action':'goto','target_cm':1})
        self.assertNotIn((0x0d08,3),self.writes())

    async def test_alarm_and_no_motion_do_not_lock_or_retrigger(self):
        self.sim.inject('alarm',0x950)
        self.sim.inject('stall',True)
        await self.c.command({'action':'goto','target_cm':2})
        await until(lambda:self.c.pp_no_motion)
        self.assertTrue(self.c.armed)
        self.assertFalse(self.c.pending_stop)
        self.assertIn('未检测到运动',self.c.snapshot()['details']['motion_notice'])
        self.assertEqual(self.writes().count((0x0d08,3)),1)
        self.sim.inject('stall',False)

    async def test_stale_arrival_bit_alone_cannot_complete(self):
        # Isolate feedback evaluation from the live polling state machine.
        for task in self.c._tasks:task.cancel()
        await asyncio.gather(*self.c._tasks,return_exceptions=True)
        self.c.mode='pp';self.c.state='MOVING';self.c.target_cm=3
        self.c.position_target_cm=3;self.c.position_connection=self.sim.epoch
        self.sim.regs[0x0307]=1
        data=await self.c.observe();data['status_word']|=1
        for _ in range(4):await self.c._monitor_motion(data)
        self.assertFalse(self.c.pending_stop)
        self.assertNotEqual(self.c.position_status,'complete')

    async def test_position_without_drive_bit_waits_and_bit_with_stability_completes(self):
        # Isolate feedback evaluation from the live polling state machine.
        for task in self.c._tasks:task.cancel()
        await asyncio.gather(*self.c._tasks,return_exceptions=True)
        self.sim.inject('physical_cm',1)
        self.sim.regs[0x0307]=1
        data=await self.c.observe()
        self.c.mode='pp';self.c.state='MOVING';self.c.target_cm=self.c.snapshot()['details']['height_cm']
        self.c.position_target_cm=self.c.target_cm;self.c.position_connection=self.sim.epoch
        data['status_word']&=~1
        for _ in range(4):await self.c._monitor_motion(data)
        self.assertFalse(self.c.pending_stop)
        self.assertIn('等待驱动到位信号',self.c.position_detail)
        data['status_word']|=1
        await self.c._monitor_motion(data);await self.c._monitor_motion(data)
        self.assertTrue(self.c.pending_stop)
        self.assertEqual(self.c.position_status,'settling')

    async def test_fault_601_during_completion_does_not_report_cleanup_complete(self):
        self.c.mode='pp';self.c.position_status='settling';self.c.position_target_cm=1
        self.c._stop_intent(reason='目标到位')
        self.sim.inject('alarm',0xE601)
        await self.c._confirm_stop()
        self.assertEqual(self.c.position_status,'stopped')
        self.assertIn('收尾未执行',self.c.position_detail)
        self.assertNotIn((0x0d01,1),self.writes())

    async def test_explicit_stop_cancels_target_without_restart(self):
        await self.c.command({'action':'goto','target_cm':4})
        await self.c.command({'action':'stop'})
        self.assertEqual(self.c.position_status,'stopped')
        self.assertFalse(self.sim.enabled)
        self.assertIsNone(self.c.mode)
        self.assertEqual(self.writes().count((0x0d08,3)),1)

    async def test_cleanup_failure_blocks_new_motion_but_allows_direct_intervention(self):
        for action in ('servo_enable','servo_disable','servo_p03_enable','servo_p03_disable','reset_fault'):
            self.c.mode='pp'
            self.c.armed=True
            self.c.driver.bus_enable_owned=True
            self.c._stop_intent()
            self.c.fault='失能收尾未完成'
            self.s.connection_state='stop_pending'
            with patch.object(self.c.driver,'release_bus_enable',side_effect=TimeoutError('cleanup unavailable')):
                for command in ({'action':'goto','target_cm':1},{'action':'up','intent_id':'blocked'}, {'action':'home','confirm_homing':True}):
                    with self.assertRaises(LabError):await self.c.command(command)
                await self.s.request('POST','/api/control',{'action':action},{'X-Patrol-Client-Id':'operator'})
            self.assertFalse(self.c.pending_stop)
            self.assertFalse(self.c.driver.bus_enable_owned)
            self.assertFalse(self.c.armed)
            self.assertIsNone(self.c.mode)

    async def test_tolerance_saves_and_reloads_without_device_writes(self):
        from lab.model import load_config
        root=Path(self.tmp.name)
        with patch('lab.service.ROOT',root),patch('lab.model.ROOT',root):
            self.s.trace.clear()
            await self.s.update_config({'pp_tolerance_cm':.08})
            self.assertEqual(load_config().pp_tolerance_cm,.08)
            self.assertEqual(self.writes(),[])

    async def test_pp_acceleration_save_preserves_reference_and_loads_on_restart(self):
        from lab.model import load_config
        root=Path(self.tmp.name)
        desired=dict(pp_acceleration_cm_s2=1.2,pp_deceleration_cm_s2=.8,pp_emergency_deceleration_cm_s2=2)
        with patch('lab.service.ROOT',root),patch('lab.model.ROOT',root):
            baseline=dict(self.c.driver.expected_words)
            self.s.trace.clear()
            await self.s.update_config(desired)
            self.assertTrue(self.c.trusted and self.c.armed)
            self.assertEqual(self.c.home_status,'complete')
            self.assertEqual(self.c.driver.expected_words,baseline)
            for key,value in desired.items():self.assertEqual(getattr(load_config(),key),value)
            self.assertEqual(self.writes(),[])
            await self.s.update_config(dict(pp_deceleration_cm_s2=None))
            self.assertIsNone(load_config().pp_deceleration_cm_s2)
            self.assertTrue(self.c.trusted and self.c.armed)

    async def test_cancelled_task_does_not_claim_physical_stop(self):
        await self.c.command({'action':'goto','target_cm':4})
        await self.c.command({'action':'reset_fault'})
        self.assertEqual(self.c.position_status,'cancelled')
        self.assertIn('设备状态见反馈',self.c.position_detail)

    async def test_stop_intent_does_not_claim_stopped_before_feedback(self):
        await self.c.command({'action':'goto','target_cm':4})
        self.c._stop_intent(global_stop=True)
        self.assertEqual(self.c.position_status,'stopping')
        await self.c._wait_for_stop()
        self.assertEqual(self.c.position_status,'stopped')

    async def test_pp_monitor_ignores_unrelated_home_parameter_changes(self):
        from lab.registers import MONITOR_GROUPS
        self.sim.regs[0x1023]=19
        self.c.driver.monitor_index=next(i for i,g in enumerate(MONITOR_GROUPS) if g[0]==0x1023)
        self.c.driver.monitor_last=0
        await self.c.driver.monitor_parameter_turn(compare=True,mode='pp')
        self.assertEqual(self.c.driver.parameter_words[0x1023]['value'],19)
        self.sim.regs[0x101D]+=1
        self.c.driver.monitor_index=next(i for i,g in enumerate(MONITOR_GROUPS) if g[0]==0x1017)
        self.c.driver.monitor_last=0
        with self.assertRaises(LabError):await self.c.driver.monitor_parameter_turn(compare=True,mode='pp')

    async def test_tolerance_validation(self):
        for value in (0,-1,float('nan')):
            with self.assertRaises(ValueError):Config(mode='sim',pp_tolerance_cm=value).validate()

if __name__=='__main__':unittest.main()
