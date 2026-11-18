import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.model import Config, LabError
from lab.service import Service

class ServoButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=Service(Config(mode='sim', state_file=str(Path(self.temp.name)/'zero.json'),stop_timeout_s=.15))
        self.ctrl=self.service.controller
        self.sim=self.service.transport

    def tearDown(self):
        self.temp.cleanup()

    def writes(self):
        frames=[bytes.fromhex(x['frame']) for x in self.service.trace.items if x['kind']=='tx']
        return [(int.from_bytes(f[2:4],'big'),int.from_bytes(f[4:6],'big')) for f in frames if f[1]==6]

    async def test_enable_without_home_never_starts_motion(self):
        await self.ctrl.command({'action':'servo_enable'})
        self.assertTrue(self.sim.enabled)
        self.assertIsNone(self.sim.motion)
        self.assertFalse(self.ctrl.trusted)
        self.assertFalse(self.ctrl.armed)
        self.assertEqual(self.sim.regs[0x0D11],0)
        self.assertEqual(self.sim.regs[0x0D0B],1)
        self.assertFalse(any(a==0x0D08 and v in (1,3,5,7,8,16,32,64,128) for a,v in self.writes()))

    async def test_disable_stops_motion_before_force_write(self):
        self.sim.enabled=True
        self.sim.motion='up'
        self.sim.physical=self.sim.config.pulse(5)
        self.ctrl.mode='pv'
        self.ctrl.driver.jog_context=True
        self.ctrl.driver.jog_running=True
        self.ctrl.state='MOVING'
        self.ctrl.armed=True
        await self.ctrl.command({'action':'servo_disable'})
        self.assertFalse(self.sim.enabled)
        self.assertIsNone(self.sim.motion)
        events=list(self.service.trace.items)
        confirmed=next(i for i,x in enumerate(events) if x['kind']=='stop_acknowledged')
        force=next(i for i,x in enumerate(events) if x['kind']=='tx' and bytes.fromhex(x['frame'])[1:6]==bytes([6,13,11,0,65]))
        self.assertLess(confirmed,force)

    async def test_stop_failure_does_not_send_disable(self):
        self.sim.faults['lost_stop']=100
        with self.assertRaises(LabError):
            await self.ctrl.command({'action':'servo_disable'})
        self.assertFalse(any(a in (0x0D11,0x0D12) for a,v in self.writes()))
        self.assertTrue(self.ctrl.pending_stop)

    async def test_enable_rejected_while_moving(self):
        self.ctrl.mode='pv'
        with self.assertRaises(LabError):
            await self.ctrl.command({'action':'servo_enable'})
        self.assertEqual(self.writes(),[])

    async def test_alarm_prevents_enable_but_allows_disable(self):
        self.sim.regs[0x0B22]=0x410
        with self.assertRaises(LabError):
            await self.ctrl.command({'action':'servo_enable'})
        self.assertNotIn((0x0D12,507),self.writes())
        await self.ctrl.command({'action':'servo_disable'})
        self.assertEqual(self.sim.regs[0x0D12],511)

    async def test_stop_cancels_enable_during_preparation(self):
        entered=asyncio.Event()
        resume=asyncio.Event()
        original=self.ctrl.driver.prepare_jog
        async def delayed(checkpoint):
            await original(checkpoint)
            entered.set()
            await resume.wait()
            checkpoint()
        with patch.object(self.ctrl.driver,'prepare_jog',side_effect=delayed):
            task=asyncio.create_task(self.ctrl.command({'action':'servo_enable'}))
            await asyncio.wait_for(entered.wait(),1)
            await self.ctrl.stop()
            resume.set()
            with self.assertRaises(LabError): await task
        self.assertNotIn((0x0D0B,1),self.writes())

    async def test_force_selection_readback_failure_prevents_enable(self):
        self.sim.regs[0x0d11]=1
        original=self.ctrl.driver.wire.read_registers
        async def wrong(address,count):
            values=await original(address,count)
            if address==0x0D11: return [1,511]
            return values
        with patch.object(self.ctrl.driver.wire,'read_registers',side_effect=wrong):
            with self.assertRaises(LabError):
                await self.ctrl.command({'action':'servo_enable'})
        self.assertNotIn((0x0D0B,1),self.writes())
        self.assertTrue(self.ctrl.pending_stop)
