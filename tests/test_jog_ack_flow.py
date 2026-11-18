import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.model import Config, LabError
from lab.service import Service

class JogAckFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(Config(mode='sim', state_file=str(Path(self.tmp.name)/'state.json'),stop_timeout_s=.12))
        self.s.connected=True
        self.s.connection_state='connected'
        self.c=self.s.controller
        self.sim=self.s.transport
        self.sim.inject('physical_cm',5)
        await self.c.arm()

    async def asyncTearDown(self):
        self.sim.faults.update(offline=False,lost_stop=0)
        await self.s.close()
        self.tmp.cleanup()

    def writes(self):
        frames=[bytes.fromhex(x['frame']) for x in self.s.trace.items if x['kind']=='tx']
        return [(int.from_bytes(f[2:4],'big'),int.from_bytes(f[4:6],'big')) for f in frames if f[1]==6]

    async def jog(self,direction='up',token='first'):
        return await self.c.command(dict(action=direction,intent_id=token))

    async def test_stop_ack_returns_without_sampling_or_claiming_stationary(self):
        await self.jog()
        self.c.sample['speed_rpm']=200
        self.c._observed_motion=True
        self.s.trace.clear()
        with patch.object(self.c,'observe',side_effect=AssertionError('stop must not wait for feedback')):
            result=await self.c.stop()
        self.assertEqual(self.writes(),[(0x0d0b,1)])
        self.assertEqual(result['details']['stop_confirmation'],'ack')
        self.assertEqual(result['details']['motion'],'stop_acknowledged')
        self.assertEqual(result['details']['speed_rpm'],200)
        self.assertTrue(self.c.armed)
        self.assertFalse(self.c.pending_stop)
        from lab.readiness import build_readiness
        guidance=build_readiness(self.s,'test')
        self.assertTrue(guidance['can_jog_up'])
        self.assertTrue(guidance['can_jog_down'])
        self.assertNotIn('非本后端',guidance['summary'])

    async def test_both_directions_restart_with_nonzero_feedback_and_no_enable_cycle(self):
        await self.jog()
        await self.c.stop()
        self.s.trace.clear()
        original=self.c.driver.sample
        async def decaying_feedback():
            data=await original()
            data['speed_rpm']=80
            return data
        with patch.object(self.c.driver,'sample',side_effect=decaying_feedback):
            for n,direction in enumerate(('up','down','up')):
                await self.jog(direction,str(n))
                self.assertEqual(self.c.sample['speed_rpm'],80)
                await self.c.stop()
        commands=[v for a,v in self.writes() if a==0x0d0b]
        self.assertEqual(commands,[17,1,33,1,17,1])
        self.assertTrue(self.c.armed)

    async def test_parallel_http_stop_watchdog_and_duplicate_stop_share_one_write(self):
        await self.jog()
        entered,resume=asyncio.Event(),asyncio.Event()
        original=self.c.driver.stop
        async def delayed(emergency=False):
            entered.set()
            await resume.wait()
            await original(emergency)
        self.s.trace.clear()
        with patch.object(self.c.driver,'stop',side_effect=delayed):
            first=asyncio.create_task(self.c.stop())
            await entered.wait()
            epoch=self.c.epoch
            watcher=asyncio.create_task(self.c._confirm_stop())
            duplicate=asyncio.create_task(self.c.stop())
            await asyncio.sleep(0)
            self.assertEqual(self.c.epoch,epoch)
            resume.set()
            await asyncio.gather(first,watcher,duplicate)
        self.assertEqual(self.writes(),[(0x0d0b,1)])

    async def test_late_old_release_cannot_stop_new_direction(self):
        await self.jog('up','old')
        await self.c.command(dict(action='stop',intent_id='old'))
        await self.jog('down','new')
        self.s.trace.clear()
        await self.c.command(dict(action='stop',intent_id='old'))
        await self.c._confirm_stop()  # stale background waiter
        self.assertEqual(self.writes(),[])
        self.assertEqual(self.c.intent,'new')
        self.assertEqual(self.sim.motion,'down')
        await self.c.command(dict(action='estop',intent_id='old'))
        self.assertFalse(self.c.armed)
        self.assertFalse(self.sim.enabled)

    async def test_direction_change_while_running_uses_stop_ack_only(self):
        await self.jog('up','old')
        self.s.trace.clear()
        await self.jog('down','new')
        self.assertEqual([v for a,v in self.writes() if a==0x0d0b],[1,33])
        self.assertEqual(self.c.intent,'new')

    async def test_speed_setting_needs_ack_not_immediate_readback(self):
        original=self.c.driver.wire.read_registers
        async def forbid_speed_read(address,count):
            if address==0x4604: raise AssertionError('unexpected immediate speed read')
            return await original(address,count)
        with patch.object(self.c.driver.wire,'read_registers',side_effect=forbid_speed_read):
            await self.jog()
        self.assertEqual(self.sim.motion,'up')
        self.sim.regs[0x4604]+=1
        with self.assertRaises(LabError):
            await self.c.driver.check_active_parameters('pv',lambda:None)

    async def test_stop_ack_failure_keeps_pending_and_does_not_auto_restart(self):
        await self.jog()
        self.sim.faults['lost_stop']=100
        with self.assertRaises(LabError): await self.c.stop()
        self.assertTrue(self.c.pending_stop)
        self.assertFalse(self.c.armed)
        with self.assertRaises(LabError): await self.jog('down','blocked')
        self.sim.faults['lost_stop']=0
        await self.c._confirm_stop()
        self.assertFalse(self.c.pending_stop)
        self.assertFalse(self.c.armed)
        self.assertIsNone(self.sim.motion)

    async def test_stop_after_explicit_disable_does_not_reenable(self):
        await self.jog()
        await self.c.command(dict(action='servo_disable'))
        self.s.trace.clear()
        await self.c.stop()
        self.assertEqual(self.writes(),[(0x0d0b,65)])
        self.assertFalse(self.sim.enabled)

    async def test_new_jog_after_explicit_disable_enables_once_without_disable_cycle(self):
        await self.jog()
        await self.c.command(dict(action='servo_disable'))
        await self.c.arm()
        self.s.trace.clear()
        await self.jog('down','reenable')
        self.assertEqual([v for a,v in self.writes() if a==0x0d0b],[1,33])
