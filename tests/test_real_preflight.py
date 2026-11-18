import tempfile
import unittest
from pathlib import Path
from lab.model import Config, LabError
from lab.controller import Controller
from lab.driver import Driver
from lab.transport import Simulator, Trace


class RealPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(mode='real', state_file=str(Path(self.temp.name)/'state.json'))
        self.trace = Trace()
        # Real configuration with an explicitly constructed simulator: no TCP.
        self.transport = Simulator(self.config, self.trace)
        self.ctrl = Controller(self.config, Driver(self.config, self.transport), self.trace)

    def tearDown(self):
        self.temp.cleanup()

    async def test_unverified_profile_allows_read_only_preflight(self):
        await self.ctrl.arm(hardware_confirmed=True)
        self.assertTrue(self.ctrl.armed)
        self.assertFalse(self.ctrl.trusted)
        self.assertFalse(self.config.motion_profile_verified)
        self.assertIsNone(self.transport.motion)
        frames = [bytes.fromhex(x['frame']) for x in self.trace.items if x['kind']=='tx']
        self.assertTrue(frames)
        self.assertTrue(all(frame[1] == 3 for frame in frames))

    async def test_eeprom_guard_still_blocks(self):
        self.transport.regs[0x0C0D] = 1
        with self.assertRaisesRegex(LabError, 'P0C-13'):
            await self.ctrl.arm(hardware_confirmed=True)
        self.assertFalse(self.ctrl.armed)
        self.assertIsNone(self.transport.motion)

    async def test_unreferenced_pp_still_blocks(self):
        await self.ctrl.arm(hardware_confirmed=True)
        with self.assertRaisesRegex(LabError, '位置参考未可信'):
            await self.ctrl.command(dict(action='goto', target_cm=2))
        self.assertIsNone(self.transport.motion)

    async def test_invalid_limit_mapping_still_blocks(self):
        self.transport.regs[0x0308] = 0
        with self.assertRaisesRegex(LabError, 'DI4/DI5'):
            await self.ctrl.arm(hardware_confirmed=True)
        self.assertIsNone(self.transport.motion)
