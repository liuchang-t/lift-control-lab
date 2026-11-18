import unittest
from lab.driver import Driver
from lab.model import Config
from lab.transport import Simulator, Trace

class BusPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config=Config(mode='sim',home_enable_hold_s=0,home_disable_hold_s=0)
        self.transport=Simulator(self.config,Trace())
        self.driver=Driver(self.config,self.transport)

    async def test_home_skips_already_selected_bus_mode(self):
        self.transport.faults['write_failure']=0x0200
        await self.driver.home(lambda:None,lambda:None)
        self.assertEqual(self.transport.motion,'home')
        self.assertEqual(self.transport.faults['write_failure'],0x0200)

    async def test_different_bus_mode_is_written_and_verified(self):
        self.transport.regs[0x0200]=0
        await self.driver._ensure_bus_mode()
        self.assertEqual(self.transport.regs[0x0200],9)
        self.assertEqual(self.driver.expected_words[0x0200],9)

    async def test_failed_bus_mode_write_cannot_start_home(self):
        self.transport.regs[0x0200]=0
        self.transport.faults['write_failure']=0x0200
        with self.assertRaises(TimeoutError):
            await self.driver.home(lambda:None,lambda:None)
        self.assertIsNone(self.transport.motion)
