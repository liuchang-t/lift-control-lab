import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.model import Config, LabError
from lab.driver import Driver
from lab.controller import Controller, MotionCancelled
from lab.transport import Simulator, Trace


class PPPhysicalSequenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(mode='sim', home_enable_hold_s=0, home_disable_hold_s=0,
                             state_file=str(Path(self.tmp.name)/'reference.json'))
        self.trace = Trace()
        self.sim = Simulator(self.config, self.trace)
        self.sim.inject('physical_cm', 5)
        self.driver = Driver(self.config, self.sim)
        self.ctrl = Controller(self.config, self.driver, self.trace)
        self.ctrl.armed = True
        self.target = self.config.pulse(8)

    def tearDown(self):
        self.tmp.cleanup()

    def frames(self):
        return [bytes.fromhex(x['frame']) for x in self.trace.items if x['kind']=='tx']

    def writes(self):
        return [(int.from_bytes(x[2:4],'big'),int.from_bytes(x[4:6],'big'))
                for x in self.frames() if x[1] in (6,16)]

    async def start(self, checkpoint=lambda:None):
        await self.driver.start('pp', 20000, self.target, 'up', checkpoint)

    async def test_real_mode_uses_physical_di_and_prepares_before_enable_start(self):
        self.config.mode = 'real'  # Transport remains Simulator; no sockets are opened.
        self.sim.regs[0x0200] = 0
        await self.start()
        writes = self.writes()
        order = [(0x0D08,256),(0x0D08,0),(0x0307,0),(0x0200,9),
                 (0x1003,1),(0x1019,2),(0x100E,2),(0x0307,1),(0x0D08,3)]
        self.assertEqual([writes.index(x) for x in order], sorted(writes.index(x) for x in order))
        self.assertFalse(any(a in (0x0D11,0x0D12,0x0D0B) for a,v in writes))
        self.assertEqual(writes.count((0x0D08,3)),1)
        self.assertEqual(self.sim.regs[0x0D11],0)
        self.assertEqual(self.sim._i32(0x100E),self.target)
        self.assertTrue(self.driver.bus_enable_owned)
        self.assertEqual(self.sim.motion,'pp')

    async def test_each_parameter_readback_precedes_next_write(self):
        await self.start()
        frames = self.frames()
        for address in (0x0307,0x1003,0x1019,0x100E):
            for i,frame in enumerate(frames):
                if frame[1] in (6,16) and int.from_bytes(frame[2:4],'big')==address:
                    next_write = next((j for j in range(i+1,len(frames)) if frames[j][1] in (6,16)),len(frames))
                    self.assertTrue(any(f[1]==3 and int.from_bytes(f[2:4],'big')==address for f in frames[i+1:next_write]),hex(address))

    async def test_target_readback_failure_never_enables_or_starts(self):
        original = self.driver._read_words
        async def mismatch(address,count):
            words=await original(address,count)
            if address==0x100E:return [0,0]
            return words
        with patch.object(self.driver,'_read_words',side_effect=mismatch):
            with self.assertRaises(LabError):await self.start()
        self.assertNotIn((0x0307,1),self.writes())
        self.assertNotIn((0x0D08,3),self.writes())

    async def test_enable_ack_loss_preserves_ownership_without_start_retry(self):
        original = self.driver.wire.write_register
        async def lost(address,value):
            await original(address,value)
            if (address,value)==(0x0307,1):raise TimeoutError('enable applied; ACK lost')
        with patch.object(self.driver.wire,'write_register',side_effect=lost):
            with self.assertRaises(TimeoutError):await self.start()
        self.assertTrue(self.driver.bus_enable_owned)
        self.assertTrue(self.sim.enabled)
        self.assertNotIn((0x0D08,3),self.writes())

    async def test_start_ack_loss_is_not_retransmitted(self):
        self.sim.inject('lost_start_ack',True)
        with self.assertRaises(TimeoutError):await self.start()
        self.assertEqual(self.writes().count((0x0D08,3)),1)
        self.assertTrue(self.driver.bus_enable_owned)
        await self.driver.stop()
        await self.driver.release_bus_enable()
        self.assertFalse(self.sim.enabled)

    async def test_cancel_after_enable_prevents_start(self):
        def checkpoint():
            if self.sim.regs[0x0307]==1:raise MotionCancelled('explicit stop')
        with self.assertRaises(MotionCancelled):await self.start(checkpoint)
        self.assertNotIn((0x0D08,3),self.writes())
        self.assertTrue(self.driver.bus_enable_owned)

    async def test_limit_changes_before_trigger_prevent_start(self):
        original = self.driver.sample
        count = 0
        async def limit_changed():
            nonlocal count
            count += 1
            data=await original()
            if self.sim.regs[0x0307]==1:data['upper_limit']=True
            return data
        with patch.object(self.driver,'sample',side_effect=limit_changed):
            with self.assertRaises(LabError):await self.start()
        self.assertNotIn((0x0D08,3),self.writes())

    async def test_force_di_and_invalid_di3_rejected_before_motion_writes(self):
        for address,value in ((0x0D11,1),(0x0306,2)):
            self.sim.regs[0x0D11]=0
            self.sim.regs[0x0306]=1
            self.sim.regs[address]=value
            self.trace.clear()
            with self.assertRaises(LabError):await self.start()
            self.assertEqual(self.writes(),[])

    async def test_unknown_alarm_does_not_gate_pp_start(self):
        self.sim.inject('alarm',0xFFFF)
        await self.start()
        self.assertIn((0x0D08,3),self.writes())
        self.assertNotIn((0x0D01,1),self.writes())

    async def test_stationary_jog_context_is_retired_before_bus_enable(self):
        await self.driver.jog_command(1,lambda:None)
        self.driver.jog_context=True
        self.trace.clear()
        await self.start()
        self.assertLess(self.writes().index((0x0D0B,65)),self.writes().index((0x0307,1)))
        self.assertFalse(self.driver.jog_context)

    async def test_moving_feedback_prevents_preparation(self):
        self.sim.enabled=True
        self.sim.motion='up'
        self.sim.jog_active=True
        self.sim.regs[0x4604]=300
        with self.assertRaises(LabError):await self.start()
        self.assertEqual(self.writes(),[])

    async def test_stop_cleanup_and_jog_reuse_shared_bus_lifecycle(self):
        await self.start()
        self.trace.clear()
        await self.driver.stop()
        await self.driver.release_bus_enable()
        writes=self.writes()
        order=[(0x0D08,256),(0x0D08,0),(0x0307,0),(0x0D11,1),
               (0x0D12,507),(0x0D12,511),(0x0D11,0)]
        self.assertEqual([writes.index(x) for x in order],sorted(writes.index(x) for x in order))
        self.assertFalse(self.driver.bus_enable_owned)
        self.assertFalse(self.driver.home_enable_owned)
        self.assertFalse(self.sim.enabled)
        self.assertEqual(self.sim.regs[0x0D11],0)
        await self.driver.start_jog(10000,'up',lambda:None)
        self.assertEqual(self.sim.motion,'up')

    async def test_pp_stop_and_estop_use_bus_channel(self):
        for emergency,value in ((False,256),(True,512)):
            await self.start()
            self.trace.clear()
            await self.driver.stop(emergency)
            self.assertEqual(self.writes(),[(0x0D08,value)])
            await self.driver.release_bus_enable()

    async def test_active_pp_detects_changed_force_and_polarity(self):
        await self.start()
        self.sim.regs[0x0D11]=1
        await self.driver.read_force_inputs()
        with self.assertRaises(LabError):await self.driver.check_active_parameters('pp',lambda:None)
        self.sim.regs[0x0D11]=0
        await self.driver.read_force_inputs()
        self.sim.regs[0x0307]=0
        with self.assertRaises(LabError):await self.driver.check_active_parameters('pp',lambda:None)

    async def test_owned_601_is_not_automatically_cleared_to_prepare_pp(self):
        self.driver.bus_enable_owned=True
        self.sim.regs[0x0307]=1
        self.sim.inject('alarm',0xE601)
        with self.assertRaises(LabError):await self.start()
        self.assertNotIn((0x0D01,1),self.writes())
        self.assertNotIn((0x0D12,507),self.writes())
        self.assertNotIn((0x0D08,3),self.writes())

    async def test_unowned_p03_given_is_released_before_pp_parameters(self):
        await self.driver.wire.write_register(0x0307,1)
        self.driver.bus_enable_owned=False
        self.trace.clear()
        await self.start()
        writes=self.writes()
        self.assertLess(writes.index((0x0D12,507)),writes.index((0x0D12,511)))
        self.assertLess(writes.index((0x0D11,0)),writes.index((0x1003,1)))
        self.assertEqual(self.sim.regs[0x0D11],0)
        self.assertEqual(self.sim.motion,'pp')

    async def test_old_home_and_jog_monitor_age_does_not_block_new_pp(self):
        await self.driver.preflight()
        for address,item in self.driver.parameter_words.items():
            item['at']-=100
        await self.start()
        self.driver.require_monitor_fresh()
        self.assertEqual(self.sim.motion,'pp')
        # The UI can still report old unrelated observations accurately.
        self.assertGreater(self.driver.monitor_status()['max_age_s'],90)

    async def test_pp_settings_are_refreshed_after_old_cleanup(self):
        await self.driver.preflight()
        self.driver.bus_enable_owned=True
        self.sim.regs[0x0307]=1
        original=self.driver.release_bus_enable
        async def cleaned(*args,**kwargs):
            result=await original(*args,**kwargs)
            for item in self.driver.parameter_words.values():item['at']-=100
            return result
        with patch.object(self.driver,'release_bus_enable',side_effect=cleaned):
            await self.start()
        self.driver.require_monitor_fresh()

    async def test_stale_pp_deceleration_still_invalidates_monitor(self):
        await self.start()
        self.driver.parameter_words[0x101D]['at']-=100
        with self.assertRaises(LabError):self.driver.require_monitor_fresh()

    async def test_changed_position_direction_is_not_silently_adopted(self):
        await self.driver.preflight()
        self.sim.regs[0x0202]=self.sim.regs.get(0x0202,0)^1
        with self.assertRaises(LabError):await self.start()
        self.assertNotIn((0x0D08,3),self.writes())

if __name__=='__main__':
    unittest.main()
