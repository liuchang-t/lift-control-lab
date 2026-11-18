import asyncio
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from baseline.lift_modbus import append_crc
from lab.manual_bus import parse_frame, validate_response
from lab.model import Config, LabError
from lab.registers import CATALOG, RegisterMonitor
from lab.service import Service


def frame(fn,address,value,values=None):
    payload=struct.pack('>BBHH',1,fn,address,value)
    if values is not None:
        payload+=bytes([len(values)*2])+struct.pack('>'+'H'*len(values),*values)
    return append_crc(payload).hex(' ')


class FrameTests(unittest.TestCase):
    def test_strict_crc_slave_function_and_shape(self):
        good=frame(3,0x0B07,2)
        self.assertEqual(parse_frame(good,1)['count'],2)
        bad=[good[:-2]+'00',good+' 00','GG','1',frame(3,0,0),frame(3,65535,2),
             frame(4,0,1),frame(16,0,2,[1]),frame(3,0,126)]
        for item in bad:
            with self.subTest(item=item),self.assertRaises(LabError):
                parse_frame(item,1)
        with self.assertRaises(LabError):
            parse_frame(good,2)

    def test_write_ack_and_exception_validation(self):
        parsed=parse_frame(frame(6,0x0D08,16),1)
        validate_response(parsed,parsed['request'])
        for response in [bytes.fromhex(frame(6,0x0D08,32)),append_crc(bytes([1,0x86,2]))]:
            with self.assertRaises(LabError):
                validate_response(parsed,response)

    def test_monitor_ack_is_not_read_and_epoch_is_stale(self):
        monitor=RegisterMonitor()
        request=bytes.fromhex(frame(16,0x1019,2,[123,0]))
        monitor.observe(request,append_crc(request[:6]),1)
        row=next(r for r in monitor.snapshot(1)['registers'] if r['address']==0x1019)
        self.assertEqual((row['value'],row['source'],row['stale']),(None,'unknown',True))
        self.assertEqual(row['last_write_value'],123)
        row=next(r for r in monitor.snapshot(2)['registers'] if r['address']==0x1019)
        self.assertTrue(row['stale'])
        # A partial write cannot synthesize a coherent 32-bit value.
        partial=bytes.fromhex(frame(6,0x1019,99))
        monitor.observe(partial,partial,1)
        row=next(r for r in monitor.snapshot(1)['registers'] if r['address']==0x1019)
        self.assertIsNone(row['value'])
        self.assertTrue(row['stale'])


class ManualBusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.service=Service(Config(mode='sim',home_enable_hold_s=0,home_disable_hold_s=0, state_file=str(Path(self.tmp.name)/'zero.json'),poll_s=.02,stop_timeout_s=.5))
        self.ctrl=self.service.controller
        self.sim=self.service.transport
        self.headers={'X-Patrol-Client-Id':'manual-test'}
        await self.service.start()
        await self.until(lambda:bool(self.ctrl.sample))
        await self.service.acquire('manual-test','local','tester')

    async def asyncTearDown(self):
        self.sim.faults['offline']=False
        await self.service.close()
        self.tmp.cleanup()

    async def until(self,predicate):
        deadline=time.monotonic()+3
        while not predicate():
            if time.monotonic()>deadline:
                self.fail('condition timed out')
            await asyncio.sleep(.02)

    async def send(self,fn,address,value,values=None,confirm=True):
        return await self.service.request('POST','/api/manual/transaction',
            {'frame':frame(fn,address,value,values),'confirm_write':confirm},self.headers)

    def writes_since(self,mark):
        return [x['frame'] for x in self.service.trace.items
                if x['id']>mark and x['kind']=='tx' and bytes.fromhex(x['frame'])[1] in (6,16)]

    async def test_manual_takeover_retires_automatic_jog_without_stop_or_renewal(self):
        self.sim.physical=self.ctrl.config.pulse(10)
        await self.ctrl.observe()
        await self.ctrl.arm()
        await self.ctrl.command({'action':'up','intent_id':'automatic-before-manual'})
        mark=self.service.trace.serial
        await self.send(6,0x1003,6)
        # Longer than the normal JOG renewal/deadman interval.
        await asyncio.sleep(1.2)
        self.assertEqual(self.writes_since(mark),[frame(6,0x1003,6)])
        self.assertIsNone(self.ctrl.mode)
        self.assertIsNone(self.ctrl.intent)
        self.assertFalse(self.ctrl.pending_stop)
        self.assertFalse(self.ctrl.armed)

    async def test_manual_takeover_cancels_a_stop_still_waiting_for_bus(self):
        self.sim.physical=self.ctrl.config.pulse(10)
        await self.ctrl.observe()
        await self.ctrl.arm()
        await self.ctrl.command({'action':'up','intent_id':'jog-before-queued-stop'})
        mark=self.service.trace.serial
        await self.sim.lock.acquire()
        stopping=asyncio.create_task(self.ctrl.stop())
        try:
            await self.until(lambda:self.ctrl.pending_stop)
            writing=asyncio.create_task(self.send(6,0x1003,6))
            await self.until(lambda:bool(self.service.manual_active))
        finally:
            self.sim.lock.release()
        await asyncio.gather(stopping,writing)
        await asyncio.sleep(.1)
        self.assertEqual(self.writes_since(mark),[frame(6,0x1003,6)])
        self.assertFalse(self.ctrl.pending_stop)
        self.assertIsNone(self.ctrl.mode)

    async def test_manual_home_limit_warnings_do_not_insert_stop_frames(self):
        self.sim.physical=self.ctrl.config.pulse(10)
        await self.send(6,0x0D0B,1)
        await self.send(6,0x1003,6)
        await self.send(6,0x1023,17)
        mark=self.service.trace.serial
        await self.send(6,0x0D08,128)
        for warning in (0x950,0x952):
            self.sim.inject('alarm',warning)
            await self.ctrl.observe()
            await asyncio.sleep(.15)
        self.assertEqual(self.writes_since(mark),[frame(6,0x0D08,128)])
        self.assertFalse(self.ctrl.pending_stop)
        # Explicit ordinary Stop must use the latest P10 command channel,
        # even though P0D-11 was used for enabling earlier in this sequence.
        mark=self.service.trace.serial
        await self.service.request('POST','/api/control',{'action':'stop'},self.headers)
        self.assertIn(frame(6,0x0D08,256),self.writes_since(mark))
        self.assertNotIn(frame(6,0x0D0B,1),self.writes_since(mark))
        self.assertIsNone(self.sim.motion)

    async def test_poll_failure_does_not_stop_or_lock_manual_channel(self):
        await self.send(6,0x0D0B,1)
        mark=self.service.trace.serial
        with patch.object(self.ctrl.driver,'sample',side_effect=TimeoutError('feedback unavailable')):
            await asyncio.sleep(.2)
            self.ctrl.sample_at=0
            self.service._monitor_manual()
            await self.send(6,0x1003,6)
        self.assertEqual(self.writes_since(mark),[frame(6,0x1003,6)])
        self.assertFalse(self.ctrl.pending_stop)
        await self.send(6,0x0D0B,65)

    async def test_read_does_not_take_over_or_stop(self):
        await self.ctrl.arm()
        epoch=self.ctrl.epoch
        result=await self.send(3,0x0B07,2)
        self.assertFalse(result['write'])
        self.assertEqual(self.ctrl.epoch,epoch)
        self.assertTrue(self.ctrl.armed)
        self.assertFalse(self.service.manual_active)

    async def test_confirm_required_but_lease_not_required(self):
        with self.assertRaises(LabError):
            await self.send(6,0x0D08,16,confirm=False)
        self.service.lease=None
        result=await self.send(3,0x0B00,1)
        self.assertFalse(result['write'])
        self.assertFalse(self.service.manual_active)

    async def test_manual_sequence_and_exit_require_recheck_and_home(self):
        await self.ctrl.arm()
        self.ctrl.trusted=True
        await self.send(6,0x0D12,507)
        epoch=self.ctrl.epoch
        await self.send(6,0x0D08,16)
        self.assertEqual(self.sim.motion,'up')
        self.assertEqual(self.ctrl.epoch,epoch)
        self.assertFalse(self.ctrl.armed)
        self.assertFalse(self.ctrl.trusted)
        with self.assertRaises(LabError):
            await self.service.request('POST','/api/control',{'action':'up','intent_id':'bad'},self.headers)
        await self.service.request('POST','/api/manual/exit',{},self.headers)
        self.assertIsNone(self.sim.motion)
        self.assertFalse(self.service.manual_active)
        self.assertFalse(self.ctrl.armed)
        self.assertFalse(self.ctrl.trusted)

    async def test_manual_eeprom_write_only_when_explicit(self):
        await self.send(6,0x0C0D,1)
        self.assertEqual(self.sim.regs[0x0C0D],1)
        await self.service.exit_manual()
        with self.assertRaises(LabError):
            await self.ctrl.arm()

    async def test_new_manual_write_allowed_after_estop_without_clearing_normal_latch(self):
        await self.send(6,0x0D12,507)
        await self.service.request('POST','/api/control',{'action':'estop'},self.headers)
        self.assertTrue(self.ctrl.emergency)
        await self.send(6,0x0D08,16)
        self.assertEqual(self.sim.motion,'up')
        self.assertTrue(self.ctrl.emergency)
        self.assertFalse(self.ctrl.armed)

    async def test_alarm_limits_eeprom_and_reference_do_not_gate_manual_writes(self):
        await self.send(6,0x0D12,507)
        self.ctrl.armed=self.ctrl.trusted=False
        self.ctrl.emergency=True
        self.sim.physical=0
        self.sim.regs[0x0C0D]=1
        for alarm in (0x950,0x952,0xB00,0xFFFF):
            with self.subTest(alarm=alarm):
                self.sim.inject('alarm',alarm)
                await self.ctrl.observe()
                mark=self.service.trace.serial
                await self.send(6,0x1003,6)
                await asyncio.sleep(.15)
                self.assertEqual(self.writes_since(mark),[frame(6,0x1003,6)])
                self.assertFalse(self.ctrl.pending_stop)
        self.assertTrue(self.ctrl.emergency)
        self.assertFalse(self.ctrl.armed)

    async def test_lost_write_ack_is_unknown_not_retried_or_followed_by_control_writes(self):
        await self.send(6,0x0D12,507)
        mark=self.service.trace.serial
        self.sim.faults['lost_start_ack']=True
        with self.assertRaises(LabError):
            await self.send(6,0x0D08,16)
        await asyncio.sleep(.2)
        self.assertEqual(self.writes_since(mark),[frame(6,0x0D08,16)])
        self.assertFalse(self.ctrl.pending_stop)
        await self.send(6,0x0D08,256)
        self.assertIsNone(self.sim.motion)

    async def test_queued_manual_start_cannot_follow_stop_intent(self):
        await self.send(6,0x0D12,507)
        await self.sim.lock.acquire()
        task=asyncio.create_task(self.send(6,0x0D08,16))
        await asyncio.sleep(.02)
        self.ctrl._stop_intent(True)
        self.sim.lock.release()
        with self.assertRaises(LabError):
            await task
        self.assertFalse(any(x['kind']=='tx' and x['frame']==frame(6,0x0D08,16) for x in self.service.trace.items))

    async def test_stop_during_first_takeover_cancels_raw_write(self):
        await self.sim.lock.acquire()
        task=asyncio.create_task(self.send(6,0x0D08,16))
        await asyncio.sleep(.03)
        self.ctrl._stop_intent(True)
        self.sim.lock.release()
        with self.assertRaises(LabError):
            await task
        self.assertFalse(any(x['kind']=='tx' and x['frame']==frame(6,0x0D08,16) for x in self.service.trace.items))

    async def test_explicit_single_reset_can_clear_existing_alarm(self):
        self.sim.inject('alarm',0xB00)
        await self.ctrl.observe()
        await self.send(6,0x0D01,1)
        self.assertEqual(self.sim.regs[0x0B22],0)
        self.assertFalse(self.service.manual_status()['failed'])

    async def test_refresh_catalog_and_manual_mode_reads_actual_values(self):
        result=await self.service.refresh_registers()
        self.assertEqual(len(result['registers']),len(CATALOG))
        self.assertTrue(all(r['value'] is not None and r['source']=='read' for r in result['registers']))
        await self.send(6,0x0D12,507)
        self.sim.regs[0x1019]=5432
        result=await self.service.refresh_registers()
        self.assertFalse(result['cached'])
        row=next(r for r in result['registers'] if r['address']==0x1019)
        self.assertEqual(row['value'],5432)
        self.assertFalse(row['stale'])

    async def test_client_idle_and_lease_release_do_not_stop_manual_motion(self):
        await self.send(6,0x0D12,507)
        self.headers={'X-Patrol-Client-Id':'another-private-client'}
        await self.send(6,0x0D08,16)
        await self.service.request('POST','/api/devices/lift_pole_1/lease/release',{},self.headers)
        await asyncio.sleep(.2)
        self.assertTrue(self.service.manual_active)
        self.assertEqual(self.sim.motion,'up')


if __name__=='__main__':
    unittest.main()
