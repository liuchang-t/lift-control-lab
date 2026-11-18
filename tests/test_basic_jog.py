import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.model import Config, LabError
from lab.service import Service
from lab.alarms import jog_blocked
from lab.readiness import build_readiness


class BasicJogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(Config(mode='sim', state_file=str(Path(self.tmp.name)/'state.json'),poll_s=.02,deadman_s=.15,stop_timeout_s=.12))
        self.c=self.s.controller
        self.s.connected=True
        self.s.connection_state='connected'
        self.sim=self.s.transport
        self.sim.inject('physical_cm',5)
        await self.c.arm()

    async def asyncTearDown(self):
        self.sim.faults.update(offline=False,lost_stop=0,write_failure=-1)
        await self.s.close()
        self.tmp.cleanup()

    def writes(self):
        frames=[bytes.fromhex(x['frame']) for x in self.s.trace.items if x['kind']=='tx']
        return [(int.from_bytes(f[2:4],'big'),int.from_bytes(f[4:6],'big')) for f in frames if f[1]==6]

    async def jog(self,d='up',i='hold'):
        return await self.c.command(dict(action=d,intent_id=i))

    async def test_unhomed_jog_uses_capture_registers_and_rpm(self):
        self.c.speeds['pv']=self.c.config.units.rpm_to_cm_s(300)
        self.c.config.mode='real'  # Transport remains the explicit simulator.
        self.s.trace.clear()
        await self.jog()
        self.assertFalse(self.c.trusted)
        self.assertEqual(self.sim.regs[0x4604],300)
        self.assertEqual(self.writes(),[(0x4604,300),(0x0d0b,1),(0x0d0b,17)])
        self.assertEqual(self.sim.regs[0x0d11],0)
        await self.c.stop()
        self.assertEqual(self.writes()[-1],(0x0d0b,1))
        self.assertIsNone(self.sim.motion)
        self.assertTrue(self.sim.enabled)

    async def test_position_and_software_bounds_do_not_block_jog(self):
        self.sim.inject('physical_cm',.1)
        await self.jog('down')
        self.assertEqual(self.sim.motion,'down')
        await self.c._monitor_motion(await self.c.observe())
        self.assertFalse(self.c.pending_stop)

    async def test_mismatched_950_lower_limit_allows_only_up(self):
        self.sim.inject('physical_cm',0)
        self.sim.regs[0x0b22]=0xe950
        await self.c.arm()
        self.s.trace.clear()
        with self.assertRaisesRegex(LabError,'物理限位'):
            await self.jog('down')
        self.assertEqual(self.writes(),[])
        await self.jog('up')
        self.assertEqual(self.sim.motion,'up')

    async def test_overload_and_unknown_alarm_block_start(self):
        for alarm in (0x6620,0x909,0x1234):
            self.sim.regs[0x0b22]=alarm
            self.s.trace.clear()
            with self.assertRaises(LabError): await self.jog()
            self.assertEqual(self.writes(),[])

    async def test_limit_stop_then_reverse_without_rearm_or_home(self):
        await self.jog('down')
        self.sim.inject('physical_cm',0)
        self.sim.regs[0x0b22]=0xe950
        await self.c._monitor_motion(await self.c.observe())
        self.assertTrue(self.c.pending_stop)
        await self.c._confirm_stop()
        self.assertTrue(self.c.armed)
        await self.jog('up','reverse')
        self.assertEqual(self.sim.motion,'up')

    async def test_force_di_cleared_before_enable_never_set_to_one(self):
        self.sim.regs[0x0d11]=1
        self.s.trace.clear()
        await self.jog()
        self.assertIn((0x0d11,0),self.writes())
        self.assertNotIn((0x0d11,1),self.writes())
        self.assertFalse(any(a==0x0d12 for a,v in self.writes()))

    async def test_speed_ack_failure_cannot_start(self):
        original=self.c.driver.transport.transaction
        async def wrong(request,expected_length,before_send=None):
            response=await original(request,expected_length,before_send)
            return response[:-1]+bytes([response[-1]^1]) if request[1]==6 and request[2:4]==bytes.fromhex('4604') else response
        with patch.object(self.c.driver.transport,'transaction',side_effect=wrong):
            with self.assertRaises(LabError): await self.jog()
        self.assertNotIn((0x0d0b,17),self.writes())
        self.assertTrue(self.c.pending_stop)

    async def test_stop_cancels_late_preparation(self):
        entered,resume=asyncio.Event(),asyncio.Event()
        original=self.c.driver.jog_speed
        async def delayed(speed):
            result=await original(speed)
            entered.set(); await resume.wait()
            return result
        with patch.object(self.c.driver,'jog_speed',side_effect=delayed):
            task=asyncio.create_task(self.jog())
            await entered.wait()
            await self.c.stop()
            resume.set()
            with self.assertRaises(LabError): await task
        self.assertNotIn((0x0d0b,17),self.writes())
        self.assertFalse(self.sim.enabled)

    async def test_heartbeat_timeout_stops_and_never_renews(self):
        await self.c.start()
        await self.jog()
        await asyncio.sleep(.35)
        self.assertIsNone(self.sim.motion)
        self.assertIsNone(self.c.intent)
        with self.assertRaises(LabError):
            await self.c.command(dict(action='heartbeat',intent_id='hold'))

    async def test_communication_failure_revokes_motion_and_retries_stop(self):
        await self.c.start()
        await self.jog()
        self.sim.faults['offline']=True
        await asyncio.sleep(.15)
        self.assertTrue(self.c.pending_stop)
        self.assertFalse(self.c.armed)
        self.sim.faults['offline']=False
        await asyncio.sleep(.25)
        self.assertIsNone(self.sim.motion)
        self.assertFalse(self.c.armed)

    async def test_live_speed_writes_rpm_not_pv_register(self):
        await self.jog()
        self.s.trace.clear()
        await self.c.command(dict(action='set_speed',mode='pv',speed_cm_s=1.75))
        self.assertEqual(self.sim.regs[0x4604],300)
        self.assertFalse(any(a==0x102a for a,v in self.writes()))

    async def test_active_speed_and_force_changes_stop(self):
        await self.jog()
        self.sim.regs[0x4604]+=1
        with self.assertRaises(LabError):
            await self.c.driver.check_active_parameters('pv',lambda:None)
        self.sim.regs[0x0d11]=1
        await self.c._monitor_motion(await self.c.observe())
        self.assertTrue(self.c.pending_stop)

    async def test_readiness_offers_jog_without_reference(self):
        v=build_readiness(self.s,'test')
        self.assertTrue(v['can_jog_up'])
        self.assertTrue(v['can_jog_down'])
        self.assertIn('无需回零',v['summary'])

    async def test_real_pp_home_cannot_enter_old_force_di_path(self):
        self.c.config.mode='real'
        self.c.trusted=True
        self.s.trace.clear()
        with self.assertRaisesRegex(LabError,'PP/HOME'):
            await self.c.driver.start('pp',10000,1000,'up',lambda:None)
        with self.assertRaisesRegex(LabError,'PP/HOME'):
            await self.c.driver.home(lambda:None,lambda:None)
        self.assertEqual(self.writes(),[])

    def test_direction_policy_matrix(self):
        base=dict(di4_function=14,di5_function=15,di4_logic=0,di5_logic=0)
        for code in (0,0x950,0x952):
            for upper,lower in ((False,True),(True,False)):
                sample=dict(base,upper_limit=upper,lower_limit=lower,alarm_code=code)
                self.assertFalse(jog_blocked(sample,'down' if upper else 'up'))
                self.assertTrue(jog_blocked(sample,'up' if upper else 'down'))
        for upper,lower in ((True,True),(None,False)):
            self.assertTrue(jog_blocked(dict(base,upper_limit=upper,lower_limit=lower),'up'))
        for code in (0x950,0x952):
            for direction in ('up','down'):
                self.assertFalse(jog_blocked(dict(base,upper_limit=False,lower_limit=False,alarm_code=code),direction))

    async def test_queued_jog_command_is_cancelled_inside_bus_lock(self):
        generation=self.c.epoch
        await self.sim.lock.acquire()
        self.s.trace.clear()
        task=asyncio.create_task(self.c.driver.jog_command(17,lambda:self.c._checkpoint(generation)))
        await asyncio.sleep(0)
        self.c._stop_intent()
        self.sim.lock.release()
        with self.assertRaises(LabError): await task
        self.assertNotIn((0x0d0b,17),self.writes())

    async def test_lost_start_ack_requests_jog_stop_without_restart(self):
        self.sim.faults['lost_start_ack']=True
        self.s.trace.clear()
        with self.assertRaises(LabError): await self.jog()
        self.assertTrue(self.c.pending_stop)
        await self.c._confirm_stop()
        self.assertIsNone(self.sim.motion)
        self.assertEqual(self.writes().count((0x0d0b,17)),1)

    async def test_renewal_uses_direction_command_then_release_stops(self):
        await self.jog('down')
        self.c.driver.jog_last_sent=time.monotonic()-2
        await self.c.driver.renew_jog('down',lambda:None)
        self.assertEqual(self.writes().count((0x0d0b,33)),2)
        await self.c.stop()
        self.assertEqual(self.writes()[-1],(0x0d0b,1))
        self.assertIsNone(self.sim.motion)

    async def test_consecutive_jog_after_idle_speed_returns_to_100(self):
        from lab.registers import MONITOR_GROUPS
        await self.c.command(dict(action='set_speed',mode='pv',speed_cm_s=3.5))
        for number,direction in enumerate(('up','up','down','up')):
            await self.jog(direction,'repeat-'+str(number))
            self.assertEqual(self.sim.regs[0x4604],600)
            await self.c.stop()
            # Observed onsite: idle readback returns to 100, cause undetermined.
            self.sim.regs[0x4604]=100
            self.c.driver.monitor_index=MONITOR_GROUPS.index((0x4604,1))
            self.c.driver.monitor_last=0
            await self.c.driver.monitor_parameter_turn(compare=True)
            self.assertTrue(self.c.armed)
            self.assertEqual(self.c.fault,'')
            self.assertEqual(self.c.speeds['pv'],3.5)
            self.assertEqual(self.c.driver.expected_words[0x4604],600)
            self.assertEqual(self.c.driver.parameter_words[0x4604]['value'],100)
        self.assertFalse(self.c.trusted)

    async def test_active_jog_speed_change_still_stops_and_disarms(self):
        await self.jog()
        self.sim.regs[0x4604]+=1
        await self.c.start()
        deadline=time.monotonic()+2
        while self.c.armed or self.c.pending_stop:
            self.assertLess(time.monotonic(),deadline)
            await asyncio.sleep(.01)
        self.assertIn('0x4604',self.c.fault)
        self.assertIsNone(self.sim.motion)
        with self.assertRaises(LabError): await self.jog('down','after-fault')

    async def test_idle_jog_speed_does_not_block_pp_parameter_check(self):
        await self.jog()
        await self.c.stop()
        self.sim.regs[0x4604]=100
        await self.c.driver.verify_before_motion()
        # Other parameter checks remain strict.
        self.sim.regs[0x0502]+=1
        with self.assertRaises(LabError):
            await self.c.driver.verify_before_motion()
