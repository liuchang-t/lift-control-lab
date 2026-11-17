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
        self.assertEqual((row['value'],row['source'],row['stale']),(123,'write_ack',False))
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
        self.service=Service(Config(state_file=str(Path(self.tmp.name)/'zero.json'),poll_s=.02,stop_timeout_s=.5))
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

    async def test_stop_locks_further_manual_writes(self):
        await self.send(6,0x0D12,507)
        await self.service.request('POST','/api/control',{'action':'estop'},self.headers)
        with self.assertRaises(LabError):
            await self.send(6,0x0D08,16)
        self.assertTrue(self.service.manual_status()['failed'])
        self.assertIsNone(self.sim.motion)

    async def test_alarm_stops_manual_motion_and_locks_writes(self):
        await self.send(6,0x0D12,507)
        await self.send(6,0x0D08,16)
        self.sim.inject('alarm',0xB00)
        await self.until(lambda:self.service.manual_status()['failed'] and self.sim.motion is None)
        with self.assertRaises(LabError):
            await self.send(6,0x0D08,16)

    async def test_lost_write_ack_is_not_retried_and_requests_stop(self):
        await self.send(6,0x0D12,507)
        self.sim.faults['lost_start_ack']=True
        with self.assertRaises(LabError):
            await self.send(6,0x0D08,16)
        await self.until(lambda:self.sim.motion is None)
        starts=[x for x in self.service.trace.items if x['kind']=='tx' and x['frame']==frame(6,0x0D08,16)]
        self.assertEqual(len(starts),1)
        self.assertTrue(self.service.manual_status()['failed'])

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
        entered,release=asyncio.Event(),asyncio.Event()
        original=self.ctrl.stop
        async def delayed_stop(*args,**kwargs):
            result=await original(*args,**kwargs)
            entered.set()
            await release.wait()
            return result
        with patch.object(self.ctrl,'stop',side_effect=delayed_stop):
            task=asyncio.create_task(self.send(6,0x0D08,16))
            await entered.wait()
            self.ctrl._stop_intent(True)
            release.set()
            with self.assertRaises(LabError):
                await task
        self.assertFalse(self.service.manual_active)
        self.assertFalse(any(x['kind']=='tx' and x['frame']==frame(6,0x0D08,16) for x in self.service.trace.items))

    async def test_explicit_single_reset_can_clear_existing_alarm(self):
        self.sim.inject('alarm',0xB00)
        await self.ctrl.observe()
        await self.send(6,0x0D01,1)
        self.assertEqual(self.sim.regs[0x0B22],0)
        self.assertFalse(self.service.manual_status()['failed'])

    async def test_refresh_catalog_and_manual_cache_only(self):
        result=await self.service.refresh_registers()
        self.assertEqual(len(result['registers']),len(CATALOG))
        self.assertTrue(all(r['value'] is not None for r in result['registers'] if r['address'] not in (0x0D01,0x0D08)))
        await self.send(6,0x0D12,507)
        with patch.object(self.ctrl.driver.wire,'read_registers',side_effect=AssertionError('refresh must not issue reads')):
            result=await self.service.refresh_registers()
        self.assertTrue(result['cached'])

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
