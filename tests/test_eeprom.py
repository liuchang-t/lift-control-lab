import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.driver import Driver
from lab.model import Config, LabError
from lab.service import Service
from lab.transport import Simulator, Trace


class EepromTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config(poll_s=.02)
        self.trace = Trace()
        self.sim = Simulator(self.config, self.trace)
        self.driver = Driver(self.config, self.sim)

    async def test_values_are_read_only_and_first_in_sample(self):
        self.assertEqual(self.driver.eeprom_status()['status'], 'unknown')
        for value, status in [(0, 'ok'), (1, 'warning'), (7, 'warning')]:
            self.sim.regs[0x0C0D] = value
            self.trace.items.clear()
            sample = await self.driver.sample()
            check = self.driver.eeprom_status()
            self.assertEqual(sample['eeprom_write'], value)
            self.assertEqual(check['value'], value)
            self.assertEqual(check['status'], status)
            frames = [bytes.fromhex(x['frame']) for x in self.trace.items if x['kind']=='tx']
            self.assertEqual(frames[0][1:6], bytes([3, 12, 13, 0, 1]))
            self.assertTrue(all(frame[1]==3 for frame in frames))
            self.assertEqual(self.sim.regs[0x0C0D], value)
        self.sim.regs[0x0C0D] = 1
        report = await self.driver.preflight()
        self.assertTrue(any('P0C-13' in x for x in report['failures']))
        self.assertEqual(self.sim.regs[0x0C0D], 1)

    async def test_read_failure_invalidates_old_value_and_recovers(self):
        await self.driver.sample()
        with patch.object(self.driver.wire, 'read_registers', side_effect=TimeoutError('read timed out')):
            with self.assertRaises(TimeoutError):
                await self.driver.sample()
        check = self.driver.eeprom_status()
        self.assertIsNone(check['value'])
        self.assertEqual(check['status'], 'unknown')
        self.assertIn('读取失败', check['message'])
        self.sim.regs[0x0C0D] = 1
        await self.driver.sample()
        self.assertEqual(self.driver.eeprom_status()['value'], 1)

    async def test_stale_and_reconnected_observations_require_new_read(self):
        await self.driver.sample()
        self.driver._eeprom_at -= 2
        self.assertIsNone(self.driver.eeprom_status()['value'])
        await self.driver.sample()
        self.sim.epoch += 1
        self.assertIsNone(self.driver.eeprom_status()['value'])
        self.sim.regs[0x0C0D] = 1
        await self.driver.sample()
        self.assertEqual(self.driver.eeprom_status()['value'], 1)

    async def test_connection_checks_before_arm_and_status_exposes_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = Service(Config(state_file=str(Path(tmp)/'zero.json'), poll_s=.02))
            service.transport.regs[0x0C0D] = 1
            await service.start()
            try:
                for _ in range(100):
                    state = await service.request('GET', '/api/status')
                    if state['details']['eeprom_check']['value'] == 1:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(state['details']['eeprom_check']['status'], 'warning')
                self.assertIn('不会自动修改', state['details']['eeprom_check']['message'])
                self.assertFalse(state['details']['armed'])
                frames = [bytes.fromhex(x['frame']) for x in service.trace.items if x['kind']=='tx']
                self.assertTrue(frames)
                self.assertTrue(all(frame[1]==3 for frame in frames))
                with self.assertRaises(LabError):
                    await service.controller.arm()
                self.assertEqual(service.transport.regs[0x0C0D], 1)
            finally:
                await service.close()


if __name__ == '__main__':
    unittest.main()
