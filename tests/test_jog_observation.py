import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.model import Config, LabError
from lab.service import Service
from lab.readiness import build_readiness

class JogObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(Config(mode='sim', state_file=str(Path(self.tmp.name)/'state.json'),poll_s=.02,deadman_s=.15,stop_timeout_s=.1))
        self.s.connected=True
        self.s.connection_state='connected'
        self.c=self.s.controller
        self.sim=self.s.transport
        self.sim.inject('physical_cm',5)
        await self.c.arm()

    async def asyncTearDown(self):
        self.sim.faults.update(offline=False,stall=False)
        await self.s.close()
        self.tmp.cleanup()

    async def jog(self,token='hold'):
        await self.c.command(dict(action='up',intent_id=token))

    def tx(self):
        return [bytes.fromhex(x['frame']) for x in self.s.trace.items if x['kind']=='tx']

    def commands(self):
        return [int.from_bytes(f[4:6],'big') for f in self.tx() if f[1]==6 and f[2:4]==bytes.fromhex('0d0b')]

    async def no_motion(self):
        self.sim.inject('stall')
        await self.jog()
        self.c.started=self.c.last_motion=time.monotonic()-5
        await self.c._monitor_motion(await self.c.observe())
        self.assertTrue(self.c.jog_no_motion)

    async def test_current_one_skips_enable_even_without_historical_ack(self):
        self.sim.regs[0x0d0b]=1
        self.c.driver.jog_enable_epoch=None
        self.s.trace.clear()
        await self.jog()
        self.assertEqual(self.commands(),[17])
        self.assertTrue(any(f[1]==3 and f[2:4]==bytes.fromhex('0d0b') for f in self.tx()))

    async def test_nonone_always_enables_even_with_historical_ack(self):
        for n,value in enumerate((0,17,33,65)):
            self.sim.regs[0x0d0b]=value
            self.c.driver.jog_enable_epoch=self.sim.epoch
            self.s.trace.clear()
            await self.jog(str(n))
            self.assertEqual(self.commands(),[1,17])
            relevant=[(f[1],int.from_bytes(f[2:4],'big')) for f in self.tx()]
            self.assertLess(relevant.index((3,0x0d0b)),relevant.index((6,0x0d0b)))
            await self.c.stop()

    async def test_command_read_failure_prevents_enable_and_start(self):
        original=self.c.driver.wire.read_registers
        async def failed(address,count):
            if address==0x0d0b and any(f[1]==6 and f[2:4]==bytes.fromhex('4604') for f in self.tx()): raise TimeoutError('P0D-11 read failed')
            return await original(address,count)
        self.s.trace.clear()
        with patch.object(self.c.driver.wire,'read_registers',side_effect=failed):
            with self.assertRaises(LabError): await self.jog()
        self.assertEqual(self.commands(),[])
        self.assertTrue(self.c.pending_stop)
        self.assertFalse(self.c.armed)

    async def test_release_during_command_read_cancels_enable_and_start(self):
        entered,resume=asyncio.Event(),asyncio.Event()
        original=self.c.driver.wire.read_registers
        async def delayed(address,count):
            values=await original(address,count)
            if address==0x0d0b and any(f[1]==6 and f[2:4]==bytes.fromhex('4604') for f in self.tx()):
                entered.set()
                await resume.wait()
            return values
        with patch.object(self.c.driver.wire,'read_registers',side_effect=delayed):
            task=asyncio.create_task(self.jog())
            await asyncio.wait_for(entered.wait(),1)
            await self.c.stop()
            resume.set()
            with self.assertRaises(LabError): await task
        self.assertNotIn(17,self.commands())
        self.assertNotIn(1,self.commands())

    async def test_no_motion_notice_allows_heartbeat_renewal_stop_and_next_jog(self):
        await self.no_motion()
        self.assertTrue(self.c.armed)
        self.assertFalse(self.c.pending_stop)
        self.assertEqual(self.c.fault,'')
        self.assertIn('未检测到运动',self.c.snapshot()['details']['motion_notice'])
        guide=build_readiness(self.s,'test')
        self.assertIn('未检测到运动',guide['summary'])
        self.assertNotIn('arm',[a['id'] for a in guide['actions']])
        await self.c.command(dict(action='heartbeat',intent_id='hold'))
        self.c.driver.jog_last_sent-=2
        await self.c.driver.renew_jog('up',lambda:None)
        await self.c.command(dict(action='stop',intent_id='hold'))
        self.assertFalse(self.c.jog_no_motion)
        self.assertIsNone(self.sim.motion)
        await self.jog('next')
        self.assertTrue(self.c.armed)

    async def test_movement_clears_notice_without_recheck(self):
        await self.no_motion()
        self.sim.inject('stall',False)
        data=await self.c.observe()
        data['speed_rpm']=20
        await self.c._monitor_motion(data)
        self.assertEqual(self.c.snapshot()['details']['motion_notice'],'')
        self.assertTrue(self.c.armed)

    async def test_real_overload_still_stops_and_locks_after_notice(self):
        await self.no_motion()
        self.sim.regs[0x0b22]=0x6620
        await self.c._monitor_motion(await self.c.observe())
        self.assertFalse(self.c.armed)
        self.assertTrue(self.c.pending_stop)
        self.assertNotIn('未检测到运动',self.c.fault)
        await self.c._confirm_stop()
        self.assertIsNone(self.sim.motion)

    async def test_physical_limit_still_stops_after_notice(self):
        await self.no_motion()
        self.sim.inject('physical_cm',self.sim.config.travel_cm+1)
        await self.c._monitor_motion(await self.c.observe())
        self.assertTrue(self.c.pending_stop)
        await self.c._confirm_stop()
        self.assertIsNone(self.sim.motion)

    async def test_heartbeat_timeout_still_stops_after_notice(self):
        await self.no_motion()
        await self.c.start()
        await asyncio.sleep(.3)
        self.assertIsNone(self.c.intent)
        self.assertIsNone(self.sim.motion)

    async def test_communication_loss_still_stops_after_notice(self):
        await self.no_motion()
        self.sim.faults['offline']=True
        await self.c.start()
        await asyncio.sleep(.1)
        self.assertFalse(self.c.armed)
        self.assertTrue(self.c.pending_stop)
        self.sim.faults['offline']=False
        await asyncio.sleep(.15)
        self.assertIsNone(self.sim.motion)

    async def test_950_no_motion_advice_is_diagnostic_and_does_not_reset(self):
        await self.no_motion()
        for code in (0xe950,0xe952,0):
            self.sim.regs[0x0b22]=code
            await self.c._monitor_motion(await self.c.observe())
            self.s.trace.clear()
            data=self.c.snapshot()['details']
            guide=build_readiness(self.s,'test')
            if code==0xe950:
                self.assertIn('Er.950',data['motion_notice'])
                self.assertIn('可能是驱动器内部仍在阻止运动',data['motion_advice'])
                self.assertIn('人工故障复位',data['motion_advice'])
                self.assertIn('先松开点动',data['motion_advice'])
                self.assertIn(data['motion_advice'],guide['steps'])
            else:
                self.assertEqual(data['motion_advice'],'')
                self.assertNotIn('驱动器内部',str(guide['steps']))
            self.assertEqual(self.sim.regs[0x0b22],code)
            self.assertEqual(self.tx(),[])
            self.assertTrue(self.c.armed)
            self.assertFalse(self.c.pending_stop)

    async def test_950_advice_clears_when_motion_returns_or_hold_ends(self):
        await self.no_motion()
        self.sim.regs[0x0b22]=0xe950
        await self.c._monitor_motion(await self.c.observe())
        self.assertTrue(self.c.snapshot()['details']['motion_advice'])
        data=await self.c.observe()
        data['speed_rpm']=20
        await self.c._monitor_motion(data)
        self.assertEqual(self.c.snapshot()['details']['motion_advice'],'')
        self.c.jog_no_motion=True
        self.assertTrue(self.c.snapshot()['details']['motion_advice'])
        await self.c.command(dict(action='stop',intent_id='hold'))
        self.assertEqual(self.c.snapshot()['details']['motion_advice'],'')
