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
        self.service=Service(Config(mode='sim',home_enable_hold_s=0,home_disable_hold_s=0, state_file=str(Path(self.temp.name)/'zero.json'),stop_timeout_s=.15))
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
        self.assertEqual(self.sim.regs[0x0D11],1)
        self.assertEqual(self.sim.regs[0x0D12],507)
        self.assertFalse(any(a==0x0D0B for a,v in self.writes()))
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
        confirmed=next(i for i,x in enumerate(events) if x['kind']=='servo_stationary_confirmed')
        force=next(i for i,x in enumerate(events) if x['kind']=='tx' and bytes.fromhex(x['frame'])[1:6]==bytes([6,13,17,0,1]))
        self.assertLess(confirmed,force)

    async def test_stop_failure_does_not_send_disable(self):
        self.sim.faults['lost_stop']=100
        with self.assertRaises(LabError):
            await self.ctrl.command({'action':'servo_disable'})
        self.assertFalse(any(a in (0x0D11,0x0D12) for a,v in self.writes()))
        self.assertFalse(self.ctrl.pending_stop)
        self.assertTrue(self.ctrl.manual_failed)

    async def test_enable_stops_current_motion_then_sets_given(self):
        self.ctrl.mode='pv'
        self.ctrl.driver.jog_context=True
        self.ctrl.driver.jog_running=True
        self.sim.enabled=True
        self.sim.motion='up'
        self.sim.physical=self.sim.config.pulse(5)
        await self.ctrl.command({'action':'servo_enable'})
        self.assertIsNone(self.sim.motion)
        self.assertTrue(self.sim.enabled)
        self.assertLess(self.writes().index((0x0D08,256)),self.writes().index((0x0D11,1)))

    async def test_alarm_does_not_gate_enable_or_disable(self):
        self.sim.regs[0x0B22]=0x6121
        await self.ctrl.command({'action':'servo_enable'})
        self.assertIn((0x0D12,507),self.writes())
        await self.ctrl.command({'action':'servo_disable'})
        self.assertEqual(self.sim.regs[0x0D12],511)

    async def test_unknown_alarm_allows_enable_and_disable_without_reset(self):
        self.sim.regs[0x0B22]=0xFFFF
        await self.ctrl.command({'action':'servo_enable'})
        await self.ctrl.command({'action':'servo_disable'})
        self.assertIn((0x0D12,507),self.writes())
        self.assertIn((0x0D12,511),self.writes())
        self.assertNotIn((0x0D01,1),self.writes())
        self.assertEqual(self.sim.regs[0x0B22],0xFFFF)

    async def test_stop_cancels_enable_during_preparation(self):
        entered=asyncio.Event()
        resume=asyncio.Event()
        async def delayed(enabled,checkpoint):
            entered.set()
            await resume.wait()
            checkpoint()
        with patch.object(self.ctrl.driver,'set_force_servo_given',side_effect=delayed):
            task=asyncio.create_task(self.ctrl.command({'action':'servo_enable'}))
            await asyncio.wait_for(entered.wait(),1)
            await self.ctrl.stop()
            resume.set()
            with self.assertRaises(LabError): await task
        self.assertNotIn((0x0D12,507),self.writes())

    async def test_force_selection_readback_failure_prevents_enable(self):
        self.sim.regs[0x0d11]=1
        original=self.ctrl.driver.wire.read_registers
        async def wrong(address,count):
            values=await original(address,count)
            if address==0x0D11: return [0,511]
            return values
        with patch.object(self.ctrl.driver.wire,'read_registers',side_effect=wrong):
            with self.assertRaises(LabError):
                await self.ctrl.command({'action':'servo_enable'})
        self.assertNotIn((0x0D12,507),self.writes())
        self.assertFalse(self.ctrl.pending_stop)
        self.assertTrue(self.ctrl.manual_failed)


    async def test_buttons_write_and_read_each_force_step_in_order(self):
        for enabled in (True,False):
            self.service.trace.clear()
            await self.ctrl.command({'action':'servo_enable' if enabled else 'servo_disable'})
            expected=[(0x0D11,1),(0x0D12,507 if enabled else 511)]
            if not enabled: expected.append((0x0D11,0))
            frames=[bytes.fromhex(x['frame']) for x in self.service.trace.items if x['kind']=='tx']
            indices=[i for i,f in enumerate(frames) if f[1]==6 and int.from_bytes(f[2:4],'big') in (0x0D11,0x0D12)]
            self.assertEqual([(int.from_bytes(frames[i][2:4],'big'),int.from_bytes(frames[i][4:6],'big')) for i in indices],expected)
            for i in indices:
                self.assertEqual(frames[i+1][1:6],bytes([3,13,17,0,2]))
            self.assertEqual(self.sim.enabled,enabled)

    async def test_each_force_read_failure_aborts_following_writes(self):
        for enabled in (True,False):
            sequence=[(0x0D11,1),(0x0D12,507 if enabled else 511)]+([] if enabled else [(0x0D11,0)])
            for fail_index in range(len(sequence)):
                for timeout in (False,True):
                    self.service.trace.clear()
                    read=self.ctrl.driver.wire.read_registers
                    write=self.ctrl.driver.wire.write_register
                    count=0
                    async def track(address,value):
                        nonlocal count
                        await write(address,value)
                        count+=1
                    async def bad(address,n):
                        values=await read(address,n)
                        if count==fail_index+1 and address==0x0D11:
                            if timeout: raise TimeoutError('read failed')
                            values[sequence[fail_index][0]-0x0D11]^=1
                        return values
                    with patch.object(self.ctrl.driver.wire,'write_register',side_effect=track),patch.object(self.ctrl.driver.wire,'read_registers',side_effect=bad):
                        with self.assertRaises((LabError,TimeoutError)):
                            await self.ctrl.driver.set_force_servo_given(enabled,lambda:None)
                    self.assertEqual(self.writes(),sequence[:fail_index+1])

    async def test_jog_stop_ack_with_residual_motion_cannot_switch_force_di(self):
        self.ctrl.driver.jog_context=True
        self.ctrl.driver.jog_running=True
        sample=await self.ctrl.driver.sample()
        sample['speed_rpm']=30
        with patch.object(self.ctrl,'observe',return_value=sample):
            with self.assertRaises(LabError):
                await self.ctrl.command({'action':'servo_disable'})
        self.assertFalse(any(a in (0x0D11,0x0D12) for a,v in self.writes()))

    async def test_failed_old_cleanup_never_runs_from_explicit_buttons(self):
        for enabled in (True,False):
            self.ctrl.mode='pp'
            self.ctrl.pending_stop=True
            self.ctrl.driver.bus_enable_owned=True
            self.ctrl.fault='old cleanup timeout'
            with patch.object(self.ctrl.driver,'release_bus_enable',side_effect=AssertionError('old cleanup must not run')):
                await self.ctrl.command({'action':'servo_enable' if enabled else 'servo_disable'})
            self.assertFalse(self.ctrl.driver.bus_enable_owned)
            self.assertFalse(self.ctrl.pending_stop)
            self.assertFalse(self.ctrl.armed)
            self.assertEqual(self.sim.regs[0x0D12],507 if enabled else 511)

    async def test_software_estop_and_alarm_do_not_block_given_and_latch_remains(self):
        for action in ('servo_enable','servo_disable'):
            self.ctrl.emergency=True
            self.ctrl.pending_stop=True
            self.sim.regs[0x0B22]=0xFFFF
            await self.ctrl.command({'action':action})
            self.assertTrue(self.ctrl.emergency)
            self.assertFalse(self.ctrl.armed)
        self.assertNotIn((0x0D01,1),self.writes())

    async def test_service_manual_and_stop_pending_allow_explicit_servo_buttons(self):
        self.service.connected=True
        for action in ('servo_enable','servo_disable'):
            self.service.connection_state='stop_pending'
            self.service.manual_active={'holder_id':'old-client'}
            self.ctrl.manual_active=True
            self.ctrl.pending_stop=True
            await self.service.request('POST','/api/control',{'action':action},{'X-Patrol-Client-Id':'new-client'})
            self.assertEqual(self.sim.regs[0x0D12],507 if action=='servo_enable' else 511)
            self.assertFalse(self.ctrl.manual_active)

    async def test_new_explicit_button_cancels_inflight_old_cleanup(self):
        entered,resume=asyncio.Event(),asyncio.Event()
        self.ctrl.mode='pp'
        self.ctrl.driver.bus_enable_owned=True
        self.ctrl._stop_intent()
        async def old_cleanup(checkpoint):
            entered.set()
            await resume.wait()
            checkpoint()
            raise AssertionError('old cleanup resumed')
        with patch.object(self.ctrl.driver,'release_bus_enable',side_effect=old_cleanup):
            old=asyncio.create_task(self.ctrl._confirm_stop())
            await asyncio.wait_for(entered.wait(),1)
            await asyncio.wait_for(self.ctrl.command({'action':'servo_disable'}),1)
            resume.set()
            with self.assertRaises(LabError):await old
        self.assertEqual(self.sim.regs[0x0D11],0)
        self.assertEqual(self.sim.regs[0x0D12],511)
        self.assertFalse(self.ctrl.driver.bus_enable_owned)
        self.assertFalse(self.ctrl.pending_stop)

    async def test_failed_explicit_sequence_allows_next_button_without_recovery(self):
        original=self.ctrl.driver.set_force_servo_given
        with patch.object(self.ctrl.driver,'set_force_servo_given',side_effect=TimeoutError('readback failed')):
            with self.assertRaises(LabError):await self.ctrl.command({'action':'servo_enable'})
        self.assertTrue(self.ctrl.manual_failed)
        self.assertFalse(self.ctrl.pending_stop)
        await self.ctrl.command({'action':'servo_disable'})
        self.assertEqual(self.sim.regs[0x0D12],511)
        self.assertFalse(self.ctrl.manual_failed)
