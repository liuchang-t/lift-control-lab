import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from lab.model import Config, LabError
from lab.registers import MONITOR_GROUPS, polling_policy
from lab.service import Service


class ParameterMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=Service(Config(mode='sim', state_file=str(Path(self.tmp.name)/'zero.json'),poll_s=.02,stop_timeout_s=.4))
        self.ctrl=self.service.controller
        self.sim=self.service.transport
        await self.service.start()
        await self.until(lambda:bool(self.ctrl.sample))
        self.sim.inject('physical_cm',2)
        await self.ctrl.observe()
        await self.ctrl.arm()
        self.ctrl.trusted=True

    async def asyncTearDown(self):
        self.sim.faults['offline']=False
        await self.service.close()
        self.tmp.cleanup()

    async def until(self,predicate,timeout=3):
        deadline=time.monotonic()+timeout
        while not predicate():
            if time.monotonic()>deadline:
                self.fail('condition timed out')
            await asyncio.sleep(.01)

    async def move(self):
        await self.ctrl.command({'action':'goto','target_cm':10})
        self.assertEqual(self.ctrl.state,'MOVING')

    async def test_external_pp_target_and_speed_are_fast_checked(self):
        await self.move()
        self.sim.regs[0x100E]+=100
        await self.until(lambda:not self.ctrl.armed and self.sim.motion is None)
        self.assertIn('0x100E',self.ctrl.fault)

    async def test_external_active_speed_is_fast_checked(self):
        await self.move()
        self.sim.regs[0x1019]+=100
        await self.until(lambda:not self.ctrl.armed and self.sim.motion is None)
        self.assertIn('0x1019',self.ctrl.fault)

    async def test_fixed_parameters_checked_before_pp_not_background(self):
        from lab.driver import ParameterChanged
        for address in (0x0502,0x0202,0x0306):
            old=self.sim.regs.get(address,0)
            self.sim.regs[address]=old+1
            with self.assertRaises(ParameterChanged):
                await self.ctrl.driver.verify_before_motion()
            self.sim.regs[address]=old

    async def test_fixed_parameters_are_not_background_polled(self):
        self.service.trace.clear()
        for _ in range(len(MONITOR_GROUPS)):
            self.ctrl.driver.monitor_last=0
            await self.ctrl.driver.monitor_parameter_turn()
            await self.ctrl.observe()
        frames=[bytes.fromhex(x['frame']) for x in self.service.trace.items if x['kind']=='tx']
        addresses={int.from_bytes(f[2:4],'big') for f in frames if f[1]==3}
        self.assertFalse(addresses & {0x001F,0x0502,0x0507,0x0202,0x0306,0x0308})
        self.assertTrue({0x0D0B,0x0B03}.issubset(addresses))

    async def test_own_running_speed_write_updates_expected_after_readback(self):
        await self.move()
        await self.ctrl.command({'action':'set_speed','mode':'pp','speed_cm_s':.7})
        await asyncio.sleep(.15)
        self.assertTrue(self.ctrl.armed)
        self.assertEqual(self.ctrl.state,'MOVING')
        self.assertEqual(self.ctrl.driver.expected_words[0x1019],20000)

    async def test_external_motion_is_observed_without_interference(self):
        self.service.trace.clear()
        self.sim.enabled=True
        self.sim.motion='up'
        await asyncio.sleep(.15)
        self.assertEqual(self.sim.motion,'up')
        self.assertIsNone(self.ctrl.mode)
        self.assertTrue(self.ctrl.snapshot()['details']['untracked_motion'])
        frames=[bytes.fromhex(x['frame']) for x in self.service.trace.items if x['kind']=='tx']
        self.assertTrue(all(frame[1]==3 for frame in frames))

    async def test_fixed_reference_change_is_detected_on_check(self):
        self.service.trace.clear()
        self.sim.regs[0x0502]+=1
        with self.assertRaises(LabError):
            await self.ctrl.arm()
        self.assertFalse(self.ctrl.trusted)
        self.assertFalse(self.ctrl.armed)
        frames=[bytes.fromhex(x['frame']) for x in self.service.trace.items if x['kind']=='tx']
        self.assertTrue(all(frame[1]==3 for frame in frames))

    async def test_idle_external_position_change_preserves_reference_and_does_not_stop(self):
        self.service.trace.clear()
        self.sim.inject('physical_cm',100)
        await asyncio.sleep(.1)
        self.assertTrue(self.ctrl.trusted)
        self.assertTrue(self.ctrl.armed)
        frames=[bytes.fromhex(x['frame']) for x in self.service.trace.items if x['kind']=='tx']
        self.assertTrue(all(frame[1]==3 for frame in frames))

    async def test_deprecated_delayed_disable_does_not_interfere_with_external_motion(self):
        self.ctrl.config.release_enable_s=.01
        self.ctrl.stop_at=time.monotonic()-.1
        self.sim.enabled=True
        self.sim.motion='up'
        self.service.trace.clear()
        await asyncio.sleep(.15)
        self.assertTrue(self.sim.enabled)
        self.assertEqual(self.sim.motion,'up')
        frames=[bytes.fromhex(x['frame']) for x in self.service.trace.items if x['kind']=='tx']
        self.assertTrue(all(frame[1]==3 for frame in frames))

    async def test_monitor_age_failure_stops_without_accepting_new_baseline(self):
        await self.move()
        for item in self.ctrl.driver.parameter_words.values():
            item['at']-=20
        await self.until(lambda:not self.ctrl.armed and self.sim.motion is None)
        self.assertIn('12秒',self.ctrl.fault)

    async def test_before_motion_check_catches_idle_parameter_change(self):
        self.sim.regs[0x101D]+=1
        with self.assertRaises(LabError):
            await self.ctrl.command({'action':'goto','target_cm':10})
        self.assertIsNone(self.sim.motion)
        self.assertFalse(self.ctrl.armed)

    def test_polling_labels_expose_trigger_and_home_semantics(self):
        self.assertEqual(polling_policy(0x0D08)['polling_kind'],'command')
        self.assertIn('HOME',polling_policy(0x1024)['polling_label'])
        self.assertEqual(polling_policy(0x0502)['polling_kind'],'on_demand')


if __name__=='__main__':
    unittest.main()
