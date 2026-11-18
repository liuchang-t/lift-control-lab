import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from baseline.lift_modbus import int32_to_registers_le_word
from lab.driver import Driver, ParameterChanged
from lab.model import Config, LabError
from lab.service import Service
from lab.transport import Simulator, Trace
from lab.units import command_units_per_revolution


class UnitArithmeticTests(unittest.TestCase):
    def test_direct_and_ratio_use_exact_arithmetic(self):
        self.assertEqual(command_units_per_revolution(1000,131072,131072,10000),Fraction(1000))
        self.assertEqual(command_units_per_revolution(0,131072,131072,10000),Fraction(10000))
        self.assertEqual(command_units_per_revolution(0,131072,3,1),Fraction(131072,3))

    def test_invalid_parameter_ranges_and_mechanical_types(self):
        for args in [(1048577,131072,1,1),(0,0,1,1),(0,131072,0,1),(0,131072,1,2**30+1)]:
            with self.assertRaises(ValueError):
                command_units_per_revolution(*args)
        for value in (True,'0.35',float('inf'),float('nan'),0,-1):
            with self.assertRaises(ValueError):
                Config(mode='sim', cm_per_rev=value).validate()


class UnitReadbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg=Config(mode='sim')
        self.sim=Simulator(self.cfg,Trace())
        self.driver=Driver(self.cfg,self.sim)

    def put32(self,address,value):
        low,high=int32_to_registers_le_word(value)
        self.sim.regs[address],self.sim.regs[address+1]=low,high

    async def test_direct_high_word_is_retained_and_not_silently_truncated(self):
        self.put32(0x0502,65536+10000)
        result=await self.driver.read_unit_parameters()
        self.assertEqual(result['pulses_setting_words'],[10000,1])
        self.assertEqual(result['pulses_setting'],75536)
        self.assertEqual(result['status'],'mismatch')
        with self.assertRaises(ParameterChanged):
            await self.driver.require_units()

    async def test_zero_direct_uses_ratio_and_never_rounds_noninteger(self):
        self.put32(0x0502,0)
        result=await self.driver.require_units()
        self.assertEqual(result['path'],'ratio')
        self.assertEqual(result['effective_fraction'],'10000/1')
        self.assertEqual(result['status'],'simulated_consistent')
        self.put32(0x0507,3)
        self.put32(0x0509,1)
        result=await self.driver.read_unit_parameters()
        self.assertEqual(result['effective_fraction'],'131072/3')
        self.assertIsNone(result['effective_command_per_rev'])
        self.assertEqual(result['status'],'mismatch')

    async def test_real_consistent_is_not_claimed_physically_verified(self):
        self.cfg.mode='real'
        result=await self.driver.require_units()
        self.assertEqual(result['status'],'consistent')
        self.assertFalse(result['physical_verified'])
        self.assertFalse(result['mechanical_verified'])
        self.assertIn('再次上电',result['notice'])
        self.assertIn('案例单字',result['notice'])

    async def test_word_order_mismatch_and_read_error_fail_closed(self):
        self.sim.regs[0x0C1A]=0
        result=await self.driver.read_unit_parameters()
        self.assertEqual(result['status'],'mismatch')
        self.sim.regs[0x0C1A]=1
        with patch.object(self.driver.wire,'read_registers',side_effect=TimeoutError('unit timeout')):
            result=await self.driver.read_unit_parameters()
        self.assertEqual(result['status'],'unknown')
        self.assertIsNone(result['consistent'])

    async def test_preflight_and_motion_preparation_reject_changed_units(self):
        self.assertFalse((await self.driver.preflight())['failures'])
        self.put32(0x0502,20000)
        with self.assertRaises(ParameterChanged):
            await self.driver.verify_before_motion()
        self.assertTrue((await self.driver.preflight())['failures'])


class MechanicalConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_ratio_change_preserves_cm_s_and_never_writes_drive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch('lab.service.ROOT',root):
                service=Service(Config(mode='sim', state_file=str(root/'zero.json')))
                await service.start()
                try:
                    await service.controller.arm()
                    service.controller.trusted=True
                    speeds=dict(service.controller.speeds)
                    old_pulse=service.config.pulse(10)
                    service.trace.clear()
                    await service.update_config({'cm_per_rev':.7})
                    self.assertEqual(service.controller.speeds,speeds)
                    self.assertFalse(service.controller.armed)
                    self.assertFalse(service.controller.trusted)
                    self.assertEqual(service.config.pulse(10),round(old_pulse/2))
                    self.assertEqual(service.config.speed_pr(speeds['pp'],'pp'),15000)
                    self.assertEqual(service.controller.driver.unit_status()['mechanical_cm_per_rev'],.7)
                    self.assertEqual(service.controller.driver.unit_status()['status'],'unknown')
                    requests=[bytes.fromhex(x['frame']) for x in service.trace.items if x['kind']=='tx']
                    self.assertTrue(all(request[1]==3 for request in requests))
                    self.assertTrue((root/'.runtime/config.json').exists())
                finally:
                    await service.close()

    async def test_ratio_out_of_speed_range_rejects_without_clamping(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch('lab.service.ROOT',root):
                service=Service(Config(mode='sim', state_file=str(root/'zero.json')))
                await service.start()
                try:
                    speeds=dict(service.controller.speeds)
                    service.trace.clear()
                    with self.assertRaises(ValueError):
                        await service.update_config({'cm_per_rev':.001})
                    self.assertEqual(service.config.cm_per_rev,.35)
                    self.assertEqual(service.controller.speeds,speeds)
                    requests=[bytes.fromhex(x['frame']) for x in service.trace.items if x['kind']=='tx']
                    self.assertTrue(all(request[1]==3 for request in requests))
                    self.assertFalse((root/'.runtime/config.json').exists())
                finally:
                    await service.close()


if __name__=='__main__':
    unittest.main()
