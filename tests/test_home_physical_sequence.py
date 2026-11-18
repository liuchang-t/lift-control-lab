import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.model import Config, LabError
from lab.driver import Driver
from lab.controller import Controller, MotionCancelled
from lab.transport import Simulator, Trace


class HomePhysicalSequenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config=Config(mode='sim',home_enable_hold_s=0,home_disable_hold_s=0, state_file=str(Path(self.tmp.name)/'reference.json'))
        self.trace=Trace()
        self.sim=Simulator(self.config,self.trace)
        self.driver=Driver(self.config,self.sim)
        self.ctrl=Controller(self.config,self.driver,self.trace)
        self.ctrl.armed=True

    def tearDown(self):
        self.tmp.cleanup()

    def writes(self):
        frames=[bytes.fromhex(x['frame']) for x in self.trace.items if x['kind']=='tx']
        return [(int.from_bytes(x[2:4],'big'),int.from_bytes(x[4:6],'big')) for x in frames if x[1] in (6,16)]

    async def test_prepare_enable_start_order_keeps_physical_di_and_real_home_open(self):
        self.config.mode='real'  # Explicit simulator transport: never opens hardware.
        self.sim.regs[0x0200]=0
        self.sim.regs[0x1023]=0
        await self.driver.home(lambda:None,lambda:None)
        writes=self.writes()
        order=[(0x0200,9),(0x1003,6),(0x1023,17),(0x1024,2),(0x0307,1),(0x0D08,128)]
        self.assertEqual(sorted(writes.index(x) for x in order),[writes.index(x) for x in order])
        self.assertFalse(any(a in (0x0D11,0x0D12,0x0D0B) for a,v in writes))
        self.assertEqual(self.sim.regs[0x0D11],0)
        self.assertTrue(self.sim.enabled)

    async def test_saved_motion_parameters_are_read_back_before_enable_and_start(self):
        self.config.home_speed=30000
        self.config.home_creep_speed=5000
        self.config.home_acceleration=20000
        await self.driver.home(lambda:None,lambda:None)
        for address,value in ((0x1024,30000),(0x1026,5000),(0x1028,20000)):
            self.assertEqual(self.sim._i32(address),value)
        frames=[bytes.fromhex(x['frame']) for x in self.trace.items if x['kind']=='tx']
        enable=next(i for i,x in enumerate(frames) if x[1]==6 and x[2:6]==bytes.fromhex('03 07 00 01'))
        for address in (0x1024,0x1026,0x1028):
            write=next(i for i,x in enumerate(frames) if x[1]==16 and int.from_bytes(x[2:4],'big')==address)
            read=next(i for i,x in enumerate(frames[write+1:],write+1) if x[1]==3 and int.from_bytes(x[2:4],'big')==address)
            self.assertLess(write,read)
            self.assertLess(read,enable)
        self.assertEqual(self.driver.parameters['home_acceleration'],20000)

    async def test_parameter_readback_mismatch_prevents_enable_and_start(self):
        self.config.home_creep_speed=5000
        original=self.driver._read_words
        async def mismatch(address,count):
            words=await original(address,count)
            if address==0x1026 and self.sim._i32(address)==5000:
                return [4999,0]
            return words
        with patch.object(self.driver,'_read_words',side_effect=mismatch):
            with self.assertRaises(LabError):
                await self.driver.home(lambda:None,lambda:None)
        self.assertNotIn((0x0307,1),self.writes())
        self.assertNotIn((0x0D08,128),self.writes())

    async def test_saved_speed_above_drive_limit_does_not_start(self):
        self.config.home_speed=2147483647
        self.sim.regs[0x1017]=100000 & 0xffff
        self.sim.regs[0x1018]=100000 >> 16
        with self.assertRaises(LabError):
            await self.driver.home(lambda:None,lambda:None)
        self.assertNotIn((0x0D08,128),self.writes())

    async def test_home_stop_and_emergency_use_bus_control_word_after_jog(self):
        for emergency,value in ((False,256),(True,512)):
            self.driver.jog_context=True
            await self.driver.home(lambda:None,lambda:None)
            self.assertFalse(self.driver.jog_context)
            self.trace.items.clear()
            await self.driver.stop(emergency)
            self.assertEqual(self.writes(),[(0x0D08,value)])

    async def test_reject_force_di_and_incompatible_di3_before_start(self):
        for address,value in ((0x0D11,1),(0x0306,2)):
            self.sim.regs[0x0D11]=0
            self.sim.regs[0x0306]=1
            self.sim.regs[address]=value
            with self.assertRaises(LabError):
                await self.driver.home(lambda:None,lambda:None)
        self.assertNotIn((0x0D08,128),self.writes())

    async def test_cleanup_aborts_between_clear_start_and_polarity_write(self):
        self.driver.home_enable_owned=True
        self.sim.regs[0x0307]=1
        calls=0
        def checkpoint():
            nonlocal calls
            calls+=1
            if calls==4:
                raise MotionCancelled('manual takeover')
        with self.assertRaises(MotionCancelled):
            await self.driver.release_home_enable(checkpoint)
        self.assertEqual(self.sim.regs[0x0307],1)
        self.assertNotIn((0x0307,0),self.writes())

    async def test_observed_deceleration_release_and_completion_require_stationary_feedback(self):
        await self.driver.read_parameters()
        await self.driver.read_force_inputs()
        self.ctrl.mode='home'; self.ctrl.state='HOMING'
        self.ctrl.home_triggered=True
        self.ctrl.home_connection=self.sim.epoch
        self.ctrl.started=time.monotonic()-5
        self.ctrl.last_motion=self.ctrl.started
        # Search speed / acceleration gives a 2s deceleration interval.
        self.driver.parameters.update(home_speed=20000,home_acceleration=10000)
        baseline=await self.driver.sample()
        baseline.update(di3_logic=1)
        trajectory=[(-1035,-108,True,False),(-12391,-69,True,False),
                    (-17734,-11,True,False),(-17700,6,True,False),
                    (28160,35,False,False),(0,2,False,True),
                    (0,0,False,True),(0,0,False,True)]
        for i,(position,speed,lower,done) in enumerate(trajectory):
            data=dict(baseline,position_feedback=position,speed_rpm=speed,
                      lower_limit=lower,upper_limit=False,home_attained=done,
                      status_word=11 if done else 3,alarm_code=0,alarm_raw=0)
            async def sample(): return data
            with patch.object(self.driver,'sample',side_effect=sample):
                current=await self.ctrl.observe()
            await self.ctrl._monitor_home(current)
            if i<len(trajectory)-1:
                self.assertFalse(self.ctrl.trusted)
                self.assertFalse(self.ctrl.pending_stop,self.ctrl.fault)
        self.assertTrue(self.ctrl.trusted)
        self.assertEqual(self.ctrl.home_phase,'complete')

    async def test_stop_ack_sent_once_while_waiting_for_motion_to_settle(self):
        self.driver.jog_context=False
        self.ctrl.mode='home'
        self.ctrl._stop_intent()
        data=await self.driver.sample()
        data.update(speed_rpm=20)
        async def moving(): return data
        with patch.object(self.ctrl,'observe',side_effect=moving):
            self.assertFalse(await self.ctrl._confirm_stop())
            self.assertFalse(await self.ctrl._confirm_stop())
        self.assertEqual(self.writes().count((0x0D08,256)),1)


    async def test_polarity_readback_alone_does_not_release_son_or_restore_jog(self):
        await self.driver.wire.write_register(0x0307,1)
        await self.driver.wire.write_register(0x0307,0)
        self.assertEqual(await self.driver._read_words(0x0307,1),[0])
        await self.driver.wire.write_register(0x0D0B,1)
        await self.driver.wire.write_register(0x0D0B,17)
        self.assertTrue(self.sim.enabled)
        self.assertIsNone(self.sim.motion)
        self.driver.home_enable_owned=True
        await self.driver.release_home_enable()
        expected=[(0x0D08,0),(0x0307,0),(0x0D11,1),(0x0D12,507),(0x0D12,511),(0x0D11,0)]
        self.assertEqual(self.writes()[-6:],expected)
        self.assertFalse(self.sim.enabled)
        self.assertFalse(self.sim.home_son_latched)
        self.assertFalse(self.driver.home_enable_owned)
        self.assertEqual(self.driver.force_status()['selection'],0)
        for value in (17,33,17):
            await self.driver.wire.write_register(0x0D0B,1)
            await self.driver.wire.write_register(0x0D0B,value)
            self.assertIsNotNone(self.sim.motion)
            await self.driver.wire.write_register(0x0D0B,1)

    async def test_moving_feedback_prevents_cleanup_force_transition(self):
        self.driver.home_enable_owned=True
        sample=await self.driver.sample()
        sample['speed_rpm']=20
        with patch.object(self.driver,'sample',return_value=sample):
            with self.assertRaises(LabError):
                await self.driver.release_home_enable()
        self.assertFalse(any(a in (0x0D11,0x0D12) for a,v in self.writes()))
        self.assertTrue(self.driver.home_enable_owned)

    async def test_cleanup_lost_ack_at_each_step_reissues_stop_and_never_starts(self):
        for failed in ((0x0307,0),(0x0D11,1),(0x0D12,507),(0x0D12,511),(0x0D11,0)):
            with self.subTest(failed=failed):
                await self.driver.wire.write_register(0x0307,1)
                self.driver.home_enable_owned=True
                self.driver.jog_context=False
                self.ctrl.mode='home'
                self.ctrl._stop_intent()
                original=self.driver.wire.write_register
                async def lost_ack(address,value):
                    await original(address,value)
                    if (address,value)==failed:
                        raise TimeoutError('applied but reply lost')
                with patch.object(self.driver.wire,'write_register',side_effect=lost_ack):
                    with self.assertRaises(TimeoutError):
                        await self.ctrl._confirm_stop()
                self.assertTrue(self.ctrl.pending_stop)
                self.assertTrue(self.driver.home_enable_owned)
                self.assertIsNone(self.ctrl._stop_sent_epoch)
                self.trace.clear()
                self.assertTrue(await self.ctrl._confirm_stop())
                self.assertEqual(self.writes()[0],(0x0D08,256))
                self.assertFalse(any((a==0x0D0B and v in (17,33)) or (a==0x0D08 and v==128) for a,v in self.writes()))
                self.assertFalse(self.sim.enabled)
                self.assertFalse(self.driver.home_enable_owned)
                self.assertEqual(self.sim.regs[0x0D11],0)


    async def test_cleanup_reads_each_transition_before_next_write(self):
        self.driver.home_enable_owned=True
        await self.driver.wire.write_register(0x0307,1)
        self.trace.clear()
        await self.driver.release_home_enable()
        frames=[bytes.fromhex(x['frame']) for x in self.trace.items if x['kind']=='tx']
        expected=[(0x0D08,0),(0x0307,0),(0x0D11,1),(0x0D12,507),(0x0D12,511),(0x0D11,0)]
        indices=[i for i,f in enumerate(frames) if f[1]==6]
        self.assertEqual([(int.from_bytes(frames[i][2:4],'big'),int.from_bytes(frames[i][4:6],'big')) for i in indices],expected)
        for i,(address,value) in zip(indices,expected):
            self.assertEqual(frames[i+1][1],3)
            self.assertEqual(int.from_bytes(frames[i+1][2:4],'big'),address)
            self.assertEqual(int.from_bytes(frames[i+1][4:6],'big'),1)

    async def test_cleanup_bad_or_failed_readback_never_advances_sequence(self):
        sequence=[(0x0D08,0),(0x0307,0),(0x0D11,1),(0x0D12,507),(0x0D12,511),(0x0D11,0)]
        for target in sequence:
            for timeout in (False,True):
                with self.subTest(target=target,timeout=timeout):
                    self.driver.home_enable_owned=True
                    await self.driver.wire.write_register(0x0307,1)
                    self.trace.clear()
                    original_write=self.driver.wire.write_register
                    original_read=self.driver._read_words
                    last=None
                    async def write(address,value):
                        nonlocal last
                        await original_write(address,value)
                        last=(address,value)
                    async def read(address,count):
                        if last==target and address==target[0] and count==1:
                            if timeout:
                                raise TimeoutError('readback lost')
                            return [target[1]^1]
                        return await original_read(address,count)
                    with patch.object(self.driver.wire,'write_register',side_effect=write),patch.object(self.driver,'_read_words',side_effect=read):
                        with self.assertRaises((LabError,TimeoutError)):
                            await self.driver.release_home_enable()
                    self.assertEqual(self.writes(),sequence[:sequence.index(target)+1])
                    self.assertTrue(self.driver.home_enable_owned)


    async def test_cleanup_waits_for_both_di3_transitions_before_next_write(self):
        self.driver.home_enable_owned=True
        await self.driver.wire.write_register(0x0307,1)
        original=self.driver._read_words
        remaining={507:2,511:3}
        observed=[]
        async def delayed(address,count):
            values=await original(address,count)
            given=self.sim.regs[0x0D12]
            if address==0x0B03 and self.sim.regs[0x0D11]==1:
                expected=0 if given==507 else 1
                if remaining[given]:
                    remaining[given]-=1
                    return [(values[0]&~4)|((1-expected)<<2)]
                observed.append(given)
            return values
        write=self.driver.wire.write_register
        async def checked(address,value):
            if (address,value)==(0x0D12,511):self.assertIn(507,observed)
            if (address,value)==(0x0D11,0):self.assertIn(511,observed)
            await write(address,value)
        with patch.object(self.driver,'_read_words',side_effect=delayed),patch.object(self.driver.wire,'write_register',side_effect=checked):
            await self.driver.release_home_enable()
        self.assertFalse(self.driver.home_enable_owned)
        events=[r for r in self.trace.items if r['kind']=='home_di3_observed']
        self.assertEqual([r['observed'] for r in events],[0,1])
        self.assertTrue(all(r['completion_confirmed'] is False for r in events))

    async def test_di3_stuck_times_out_without_advancing_and_retries_stop(self):
        self.config.stop_timeout_s=.04
        for stuck_given in (507,511):
            self.driver.home_enable_owned=True
            await self.driver.wire.write_register(0x0307,1)
            self.ctrl.mode='home'
            self.ctrl._stop_intent()
            self.trace.clear()
            original=self.driver._read_words
            async def stuck(address,count):
                values=await original(address,count)
                if address==0x0B03 and self.sim.regs[0x0D11]==1 and self.sim.regs[0x0D12]==stuck_given:
                    return [(values[0]&~4)|(4 if stuck_given==507 else 0)]
                return values
            with patch.object(self.driver,'_read_words',side_effect=stuck):
                with self.assertRaisesRegex(LabError,'DI3=.*超时'):
                    await self.ctrl._confirm_stop()
            self.assertTrue(self.ctrl.pending_stop)
            self.assertTrue(self.driver.home_enable_owned)
            self.assertIsNone(self.ctrl._stop_sent_epoch)
            self.assertNotIn((0x0D11,0),self.writes())
            if stuck_given==507:self.assertNotIn((0x0D12,511),self.writes())
            self.trace.clear()
            await self.ctrl._confirm_stop()
            self.assertEqual(self.writes()[0],(0x0D08,256))
            self.assertFalse(self.ctrl.pending_stop)

    async def test_di3_read_hang_is_bounded_and_does_not_advance(self):
        self.config.stop_timeout_s=.03
        self.driver.home_enable_owned=True
        original=self.driver._read_words
        async def hang(address,count):
            if address==0x0B03 and self.sim.regs[0x0D11]==1:
                await asyncio.Event().wait()
            return await original(address,count)
        with patch.object(self.driver,'_read_words',side_effect=hang):
            with self.assertRaisesRegex(LabError,'读取DI3超时'):
                await asyncio.wait_for(self.driver.release_home_enable(),.5)
        self.assertNotIn((0x0D12,511),self.writes())
        self.assertTrue(self.driver.home_enable_owned)

    async def test_cancellation_during_di3_wait_never_advances(self):
        self.driver.home_enable_owned=True
        original=self.driver._read_words
        cancelled=False
        async def observe(address,count):
            nonlocal cancelled
            values=await original(address,count)
            if address==0x0B03 and self.sim.regs[0x0D11]==1:
                cancelled=True
            return values
        def checkpoint():
            if cancelled:raise MotionCancelled('manual takeover')
        with patch.object(self.driver,'_read_words',side_effect=observe):
            with self.assertRaises(MotionCancelled):
                await self.driver.release_home_enable(checkpoint)
        self.assertNotIn((0x0D12,511),self.writes())
        self.assertTrue(self.driver.home_enable_owned)

    async def test_already_matching_di3_has_no_settling_sleep(self):
        self.driver.home_enable_owned=True
        # Transport yields with sleep(0); positive sleeps would be fixed delays.
        sleep=asyncio.sleep
        async def checked(delay,*args,**kwargs):
            self.assertEqual(delay,0)
            return await sleep(delay,*args,**kwargs)
        with patch('lab.driver.asyncio.sleep',side_effect=checked):
            await self.driver.release_home_enable()


    async def test_configured_hold_observes_feedback_and_respects_minimum(self):
        self.config.home_enable_hold_s=.03
        self.config.home_disable_hold_s=.02
        self.driver.home_enable_owned=True
        began=time.monotonic()
        await self.driver.release_home_enable()
        self.assertGreaterEqual(time.monotonic()-began,.05)
        events=[r for r in self.trace.items if r['kind']=='home_di3_hold_elapsed']
        self.assertEqual([r['given'] for r in events],[507,511])
        self.assertTrue(all(not r['completion_confirmed'] for r in events))

    async def test_motion_during_hold_aborts_before_next_given(self):
        self.config.home_enable_hold_s=.03
        self.driver.home_enable_owned=True
        original=self.driver._read_words
        async def moving(address,count):
            if address==0x0B00 and self.sim.regs[0x0D11]==1:
                return [20]
            return await original(address,count)
        with patch.object(self.driver,'_read_words',side_effect=moving):
            with self.assertRaisesRegex(LabError,'保持期间检测到运动'):
                await self.driver.release_home_enable()
        self.assertNotIn((0x0D12,511),self.writes())


    async def test_601_seen_in_cleanup_feedback_defers_without_writes(self):
        self.driver.home_enable_owned=True
        self.sim.regs[0x0307]=1
        self.sim.regs[0x0B22]=0xE601
        self.trace.clear()
        self.assertIs(await self.driver.release_home_enable(),False)
        self.assertEqual(self.writes(),[])
        self.assertTrue(self.driver.home_enable_owned)


    async def test_601_uses_stationary_feedback_even_after_jog_context(self):
        self.driver.jog_context=True
        self.ctrl.sample={'alarm_code':0x601}
        self.ctrl._stop_intent()
        sample={'alarm_code':0x601,'speed_rpm':20,'position_feedback':100}
        with patch.object(self.ctrl,'observe',return_value=sample):
            self.assertFalse(await self.ctrl._confirm_stop())
        self.assertTrue(self.ctrl.pending_stop)
        self.assertFalse(any(a in (0x0D11,0x0D12) for a,v in self.writes()))


    async def test_601_appearing_during_automatic_hold_defers_next_write(self):
        self.config.home_enable_hold_s=.1
        self.driver.home_enable_owned=True
        original=self.driver._read_words
        async def alarm_during_hold(address,count):
            if address==0x0B22 and self.sim.regs[0x0D11]==1 and self.sim.regs[0x0D12]==507:
                self.sim.regs[0x0B22]=0xE601
            return await original(address,count)
        with patch.object(self.driver,'_read_words',side_effect=alarm_during_hold):
            self.assertIs(await self.driver.release_home_enable(),False)
        self.assertNotIn((0x0D12,511),self.writes())
        self.assertNotIn((0x0D11,0),self.writes())
        self.assertEqual(self.sim.regs[0x0B22],0xE601)
