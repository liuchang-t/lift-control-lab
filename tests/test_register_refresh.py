import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baseline.lift_modbus import int32_to_registers_le_word
from lab.model import Config, LabError
from lab.service import Service
from lab.registers import CATALOG, REFRESH_GROUPS


class RegisterRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(Config(mode='sim',state_file=str(Path(self.tmp.name)/'state.json')))
        self.s.connected=True
        self.s.connection_state='connected'
        self.c=self.s.controller
        self.t=self.s.transport

    async def asyncTearDown(self):
        self.s.connected=False
        await self.s.close()
        self.tmp.cleanup()

    def row(self,result,address):
        return next(row for row in result['registers'] if row['address']==address)

    async def write(self,address,value):
        await self.c.driver.wire.write_register(address,value)

    async def test_sorted_readback_catalog_and_visible_servo_polarity(self):
        result=await self.s.refresh_registers()
        addresses=[row['address'] for row in result['registers']]
        self.assertEqual(addresses,sorted(addresses))
        self.assertTrue(self.row(result,0x0307)['visible'])
        self.assertTrue(self.row(result,0x0306)['visible'])
        self.assertEqual(self.row(result,0x0307)['polling_kind'],'fast')
        self.assertFalse(self.row(result,0x0308)['visible'])
        self.assertFalse(result['cached'])
        self.assertEqual(result['refreshed_count'],len(CATALOG))
        self.assertFalse(result['partial'])
        self.assertTrue(all(row['source']=='read' for row in result['registers']))
        tx=[bytes.fromhex(x['frame']) for x in self.s.trace.items if x['kind']=='tx']
        self.assertTrue(all(raw[1]==3 for raw in tx))
        self.assertTrue(any(int.from_bytes(raw[2:4],'big')==0x0D01 for raw in tx))

    async def test_manual_and_active_preparation_refresh_is_real_read_without_baseline_change(self):
        self.c.take_manual_control()
        self.s.manual_active={'epoch':self.c.epoch}
        self.c.driver.expected_words={0x1003:1}
        self.c.driver.parameter_words={0x1003:dict(value=1,at=1,epoch=self.t.epoch)}
        self.t.regs[0x1003]=6
        self.t.regs[0x0307]=1
        await self.c._sequence.acquire()
        try:
            result=await asyncio.wait_for(self.s.refresh_registers(),2)
        finally:
            self.c._sequence.release()
        self.assertEqual(self.row(result,0x1003)['value'],6)
        self.assertEqual(self.row(result,0x0307)['value'],1)
        self.assertFalse(self.row(result,0x1003)['stale'])
        self.assertEqual(self.c.driver.expected_words,{0x1003:1})
        self.assertEqual(self.c.driver.parameter_words[0x1003]['value'],1)
        self.assertTrue(self.c.manual_active)
        self.assertFalse(self.c.pending_stop)
        self.assertTrue(all(bytes.fromhex(x['frame'])[1]==3 for x in self.s.trace.items if x['kind']=='tx'))

    async def test_write_ack_keeps_old_read_and_reports_pending_until_refresh(self):
        await self.s.refresh_registers()
        old=self.row(self.s.register_snapshot(),0x1019)['value']
        await self.c.driver.wire.write_registers(0x1019,int32_to_registers_le_word(70000))
        row=self.row(self.s.register_snapshot(),0x1019)
        self.assertEqual(row['value'],old)
        self.assertEqual(row['source'],'read')
        self.assertEqual(row['last_write_value'],70000)
        self.assertTrue(row['readback_pending'])
        self.assertTrue(row['stale'])
        row=self.row(await self.s.refresh_registers(),0x1019)
        self.assertEqual(row['value'],70000)
        self.assertFalse(row['readback_pending'])
        self.assertFalse(row['stale'])

    async def test_partial_read_failure_is_explicit_and_cleared_by_next_read(self):
        await self.s.refresh_registers()
        failed_start=next(a for a,w in REFRESH_GROUPS if a<=0x1019<a+w)
        original=self.t.transaction
        async def fail(request,*args,**kwargs):
            if int.from_bytes(request[2:4],'big')==failed_start:
                raise TimeoutError('test read timeout')
            return await original(request,*args,**kwargs)
        with patch.object(self.t,'transaction',side_effect=fail):
            result=await self.s.refresh_registers()
        self.assertTrue(result['partial'])
        self.assertEqual(len(result['errors']),1)
        self.assertLess(result['refreshed_count'],len(CATALOG))
        row=self.row(result,0x1019)
        self.assertTrue(row['stale'])
        self.assertIn('test read timeout',row['read_error'])
        self.assertEqual(row['source'],'read')
        result=await self.s.refresh_registers()
        self.assertFalse(result['partial'])
        self.assertFalse(self.row(result,0x1019)['read_error'])

    async def test_connection_replacement_cancels_refresh_before_next_request(self):
        original=self.t.transaction
        calls=0
        async def replace(request,*args,**kwargs):
            nonlocal calls
            calls+=1
            result=await original(request,*args,**kwargs)
            self.s.controller=object()
            return result
        try:
            with patch.object(self.t,'transaction',side_effect=replace):
                with self.assertRaises(LabError):
                    await self.s.refresh_registers()
        finally:
            self.s.controller=self.c
        self.assertEqual(calls,1)

    async def test_active_motion_refresh_neither_stops_nor_acquires_sequence(self):
        self.c.mode='home'
        self.c.state='HOMING'
        self.c.armed=True
        result=await self.s.refresh_registers()
        self.assertEqual(result['refreshed_count'],len(CATALOG))
        self.assertEqual(self.c.mode,'home')
        self.assertTrue(self.c.armed)
        self.assertFalse(self.c.pending_stop)
        self.c.mode=None


if __name__=='__main__':
    unittest.main()
