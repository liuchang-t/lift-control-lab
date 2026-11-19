import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.model import Config, LabError
from lab.service import Service

class OperatorRegisterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.svc=Service(Config(mode='sim',state_file=str(Path(self.tmp.name)/'state.json')))
        self.ctrl=self.svc.controller
        self.sim=self.svc.transport

    def tearDown(self):
        self.tmp.cleanup()

    def frames(self):
        return [bytes.fromhex(x['frame']) for x in self.svc.trace.items if x['kind']=='tx']

    async def test_each_button_writes_once_then_reads_exact_register(self):
        for action,address,value,read in [('reset_fault',0x0d01,1,0x0b22),('servo_p03_enable',0x0307,1,0x0307),('servo_p03_disable',0x0307,0,0x0307)]:
            self.svc.trace.clear()
            await self.ctrl.command({'action':action})
            frames=self.frames()
            self.assertEqual([(f[1],int.from_bytes(f[2:4],'big'),int.from_bytes(f[4:6],'big')) for f in frames],[(6,address,value),(3,read,1)])
            self.assertTrue(self.ctrl.operator_result['write_ack'])

    async def test_stale_fault_pending_stop_and_manual_do_not_block_direct_write(self):
        for action in ('reset_fault','servo_p03_enable','servo_p03_disable'):
            self.ctrl.pending_stop=True
            self.ctrl.emergency=True
            self.ctrl.manual_active=True
            self.ctrl.sample_at=0
            self.ctrl.fault='old fault'
            self.sim.regs[0x0b22]=0xffff
            await self.ctrl.command({'action':action})
            self.assertTrue(self.ctrl.operator_result['write_ack'])
            self.assertTrue(self.ctrl.emergency)
            self.assertFalse(self.ctrl.pending_stop)

    async def test_read_failure_keeps_write_ack_without_claiming_cleared(self):
        with patch.object(self.ctrl.driver,'_read_words',side_effect=TimeoutError('read timeout')):
            await self.ctrl.command({'action':'reset_fault'})
        result=self.ctrl.operator_result
        self.assertTrue(result['write_ack'])
        self.assertEqual(result['read_error'],'read timeout')
        self.assertNotIn('cleared',result)
        self.assertEqual(len(self.frames()),1)

    async def test_write_failure_does_not_read_or_run_recovery(self):
        with patch.object(self.ctrl.driver.wire,'write_register',side_effect=TimeoutError('write timeout')), patch.object(self.ctrl.driver,'_read_words',side_effect=AssertionError('read not expected')):
            with self.assertRaises(LabError):
                await self.ctrl.command({'action':'reset_fault'})
        self.assertFalse(self.ctrl.operator_result['write_ack'])
        self.assertEqual(self.frames(),[])

    async def test_p03_readback_is_not_actual_disable(self):
        await self.ctrl.command({'action':'servo_p03_enable'})
        await self.ctrl.command({'action':'servo_p03_disable'})
        self.assertEqual(self.ctrl.operator_result['read_value'],0)
        self.assertTrue(self.ctrl.operator_result['matched'])
        self.assertTrue(self.sim.enabled)

    async def test_readback_mismatch_reports_actual_value(self):
        with patch.object(self.ctrl.driver,'_read_words',return_value=[0]):
            await self.ctrl.command({'action':'servo_p03_enable'})
        self.assertEqual(self.ctrl.operator_result['read_value'],0)
        self.assertFalse(self.ctrl.operator_result['matched'])

    async def test_later_stop_cancels_queued_direct_write(self):
        await self.ctrl._sequence.acquire()
        task=asyncio.create_task(self.ctrl.command({'action':'reset_fault'}))
        await asyncio.sleep(0)
        self.ctrl._stop_intent()
        self.ctrl._sequence.release()
        with self.assertRaises(LabError):
            await task
        self.assertEqual(self.frames(),[])

    async def test_idle_reset_preserves_reference_and_readiness(self):
        self.ctrl.armed=self.ctrl.trusted=True
        self.ctrl.home_status='complete'
        await self.ctrl.command({'action':'reset_fault'})
        self.assertTrue(self.ctrl.armed)
        self.assertTrue(self.ctrl.trusted)

    async def test_service_allows_direct_commands_during_manual_and_stop_pending(self):
        self.svc.connected=True
        self.svc.connection_state='stop_pending'
        self.svc.manual_active={'client':'test'}
        self.ctrl.manual_active=True
        await self.svc.request('POST','/api/control',{'action':'reset_fault'},{'X-Patrol-Client-Id':'test'})
        self.assertTrue(self.ctrl.operator_result['write_ack'])

if __name__=='__main__':
    unittest.main()
