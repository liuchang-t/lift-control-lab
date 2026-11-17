import unittest
from unittest.mock import patch
from lab.driver import Driver
from lab.model import Config, LabError
from lab.transport import Simulator, Trace

class ForceInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.transport=Simulator(Config(),Trace())
        self.driver=Driver(self.transport.config,self.transport)

    async def test_givens_are_read_independently_without_claiming_completion(self):
        await self.driver.write_force_input(0x0D11,1)
        await self.driver.write_force_input(0x0D12,507)
        status=self.driver.force_status()
        self.assertEqual((status['selection'],status['given']),(1,507))
        self.assertEqual(status['di3'],1) # Simulator models physical DI separately, not force completion.
        self.assertFalse(status['completion_confirmed'])
        rows=self.transport.registers.snapshot(1)['registers']
        self.assertEqual(next(r for r in rows if r['address']==0x0D12)['source'],'read')
        await self.driver.disable()
        self.assertEqual(self.driver.force_status()['given'],511)
        events=[x for x in self.transport.trace.items if x['kind']=='force_input_readback']
        self.assertEqual([x['requested'] for x in events],[1,507,511])

    async def test_readback_mismatch_prevents_start(self):
        original=self.driver.wire.read_registers
        async def wrong(address,count):
            values=await original(address,count)
            if address==0x0D11 and self.transport.regs.get(0x0D12)==507:
                return [values[0],511]
            return values
        with patch.object(self.driver.wire,'read_registers',side_effect=wrong):
            with self.assertRaises(LabError):
                await self.driver.home(lambda:None,lambda:None)
        self.assertIsNone(self.transport.motion)

    async def test_readback_failure_is_not_success(self):
        original=self.driver.wire.read_registers
        async def failed(address,count):
            if address==0x0D11:
                raise TimeoutError('readback timeout')
            return await original(address,count)
        with patch.object(self.driver.wire,'read_registers',side_effect=failed):
            with self.assertRaises(TimeoutError):
                await self.driver.write_force_input(0x0D12,507)
        status=self.driver.force_status()
        self.assertFalse(status['fresh'])
        self.assertIsNone(status['given'])
        self.assertIn('timeout',status['error'])

    async def test_epoch_and_age_invalidate_observations(self):
        await self.driver.write_force_input(0x0D12,511)
        self.driver.force_observed['at']-=10
        self.assertIsNone(self.driver.force_status()['given'])
        await self.driver.read_force_inputs()
        self.transport.epoch+=1
        self.assertIsNone(self.driver.force_status()['given'])
        self.assertEqual(self.driver.force_status()['requested'],{})

    async def test_periodic_sampling_reads_external_givens_without_writes(self):
        self.transport.regs[0x0D11]=3
        self.transport.regs[0x0D12]=123
        await self.driver.sample()
        self.assertEqual(self.driver.force_status()['given'],123)
        tx=[bytes.fromhex(x['frame']) for x in self.transport.trace.items if x['kind']=='tx']
        self.assertTrue(all(frame[1]==3 for frame in tx))
