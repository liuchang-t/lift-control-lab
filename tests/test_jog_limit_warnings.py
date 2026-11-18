import asyncio
import tempfile
import unittest
from pathlib import Path
from lab.model import Config, LabError
from lab.service import Service
from lab.readiness import build_readiness

class JogLimitWarningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(Config(mode='sim',home_enable_hold_s=0,home_disable_hold_s=0, state_file=str(Path(self.tmp.name)/'state.json'),poll_s=.02,deadman_s=.12,stop_timeout_s=.15))
        self.s.connected=True
        self.s.connection_state='connected'
        self.c=self.s.controller
        self.sim=self.s.transport
        self.sim.inject('physical_cm',5)
        await self.c.arm()

    async def asyncTearDown(self):
        self.sim.faults['offline']=False
        await self.s.close()
        self.tmp.cleanup()

    async def jog(self,direction='up',token='hold'):
        await self.c.command(dict(action=direction,intent_id=token))

    async def test_residual_warnings_allow_both_directions_without_recheck(self):
        for code in (0xe950,0xe952):
            self.sim.regs[0x0b22]=code
            await self.c.observe()
            view=build_readiness(self.s,'test')
            self.assertTrue(view['can_jog_up'] and view['can_jog_down'])
            self.assertNotIn('arm',[a['id'] for a in view['actions']])
            self.assertIn('reset_fault',[a['id'] for a in view['actions']])
            for direction in ('up','down'):
                await self.jog(direction,str(code)+direction)
                await self.c._monitor_motion(await self.c.observe())
                await self.c.command(dict(action='heartbeat',intent_id=str(code)+direction))
                self.assertFalse(self.c.pending_stop)
                await self.c.command(dict(action='stop',intent_id=str(code)+direction))
                self.assertIsNone(self.sim.motion)
                self.assertTrue(self.c.armed)
                self.assertEqual(self.c.fault,'')

    async def test_warning_arriving_during_motion_does_not_stop(self):
        await self.jog()
        for code in (0xe950,0xe952):
            self.sim.regs[0x0b22]=code
            await self.c._monitor_motion(await self.c.observe())
            self.assertTrue(self.c.armed)
            self.assertFalse(self.c.pending_stop)
            self.assertEqual(self.sim.motion,'up')

    async def test_physical_limit_stop_escape_then_release_retains_readiness(self):
        await self.jog('down')
        self.sim.inject('physical_cm',0)
        self.sim.regs[0x0b22]=0xe950
        await self.c._monitor_motion(await self.c.observe())
        self.assertTrue(self.c.pending_stop)
        await self.c._confirm_stop()
        self.assertTrue(self.c.armed)
        with self.assertRaisesRegex(LabError,'物理限位'): await self.jog('down','blocked')
        await self.jog('up','escape')
        self.sim.inject('physical_cm',.5)
        await self.c._monitor_motion(await self.c.observe())
        self.assertFalse(self.c.pending_stop)
        self.assertTrue(self.c.armed)

    async def test_warning_reset_preserves_existing_check_without_motion(self):
        for code in (0xe950,0xe952):
            self.sim.regs[0x0b22]=code
            await self.c.observe()
            await self.c.reset_fault({'confirm_reset':True})
            self.assertEqual(self.c.sample['alarm_code'],0)
            self.assertTrue(self.c.armed)
            self.assertEqual(self.c.fault,'')
            self.assertFalse(self.c.trusted)
            self.assertIsNone(self.sim.motion)
            await self.jog(token=str(code))
            await self.c.stop()

    async def test_reset_does_not_grant_missing_check_or_clear_estop_latch(self):
        for emergency in (False,True):
            self.c.armed=False
            self.c.emergency=emergency
            self.sim.regs[0x0b22]=0xe950
            await self.c.observe()
            await self.c.reset_fault({'confirm_reset':True})
            self.assertFalse(self.c.armed)
            self.assertEqual(self.c.emergency,emergency)

    async def test_manual_reset_at_active_limit_keeps_direction_protection(self):
        self.sim.inject('physical_cm',0)
        self.sim.regs[0x0b22]=0xe950
        await self.c.reset_fault({'confirm_reset':True})
        self.assertTrue(self.c.armed)
        await self.jog('up')

    async def test_alarm_change_does_not_create_controller_lock(self):
        self.sim.regs[0x0b22]=0xe950
        await self.jog()
        self.sim.regs[0x0b22]=0x6620
        await self.c._monitor_motion(await self.c.observe())
        self.assertFalse(self.c.pending_stop)
        self.assertTrue(self.c.armed)
        self.assertEqual(self.c.sample['alarm_code'],0x620)
        await self.c.stop()
        self.assertIsNone(self.sim.motion)

    async def test_heartbeat_timeout_still_stops_with_warning(self):
        self.sim.regs[0x0b22]=0xe952
        await self.jog()
        await self.c.start()
        await asyncio.sleep(.3)
        self.assertIsNone(self.sim.motion)
        self.assertIsNone(self.c.intent)

    async def test_communication_failure_still_locks_with_warning(self):
        self.sim.regs[0x0b22]=0xe950
        await self.jog()
        self.sim.faults['offline']=True
        await self.c.start()
        await asyncio.sleep(.1)
        self.assertFalse(self.c.armed)
        self.assertTrue(self.c.pending_stop)
        self.sim.faults['offline']=False
        await asyncio.sleep(.2)
        self.assertIsNone(self.sim.motion)
