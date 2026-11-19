import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from lab.model import Config, LabError
from lab.service import Service
from lab.driver import StopNotSettled, ParameterChanged

class StopProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(Config(mode='sim',state_file=str(Path(self.tmp.name)/'state.json'),stop_timeout_s=.05))
        self.c=self.s.controller
        self.c.armed=self.c.trusted=True
        self.c.mode='pp'
        self.c.position_status='moving'
        self.c.driver.stop=AsyncMock()
        self.c.driver.release_bus_enable=AsyncMock()
        self.moving=True
        async def observe():
            self.c.sample_at=time.monotonic()
            return dict(speed_rpm=100 if self.moving else 0,position_feedback=100,alarm_code=0)
        self.c.observe=observe

    async def asyncTearDown(self):
        self.c.pending_stop=False
        self.c.mode=None
        await self.s.close()
        self.tmp.cleanup()

    async def test_slow_deceleration_returns_pending_then_completes_without_recheck(self):
        result=await self.c.stop()
        self.assertTrue(result['details']['pending_stop'])
        self.assertTrue(self.c.trusted)
        self.assertTrue(self.c.armed)
        self.assertEqual(self.c.state,'STOPPING')
        self.c.driver.release_bus_enable.assert_not_awaited()
        with self.assertRaises(LabError):self.c._checkpoint(self.c.epoch)
        self.moving=False
        self.assertTrue(await self.c._confirm_stop())
        self.assertTrue(self.c.trusted)
        self.assertTrue(self.c.armed)
        self.assertEqual(self.c.state,'IDLE')
        self.assertEqual(self.c.stop_notice,'')
        self.assertEqual(self.c.driver.stop.await_count,1)

    async def test_unsettled_cleanup_keeps_reference_and_original_stop_ack(self):
        self.moving=False
        self.c.driver.release_bus_enable.side_effect=StopNotSettled('已撤销强制DI，等待位置稳定')
        result=await self.c.stop()
        self.assertTrue(result['details']['pending_stop'])
        self.assertIn('等待位置稳定',result['details']['stop_notice'])
        self.assertTrue(self.c.armed and self.c.trusted)
        self.c.driver.release_bus_enable.side_effect=None
        self.assertTrue(await self.c._confirm_stop())
        self.assertEqual(self.c.driver.stop.await_count,1)
        self.assertEqual(self.c.fault,'')

    async def test_communication_failure_still_invalidates_reference(self):
        self.c.driver.stop.side_effect=ConnectionError('offline')
        with self.assertRaises(LabError):await self.c.stop()
        self.assertFalse(self.c.trusted)
        self.assertFalse(self.c.armed)
        self.assertTrue(self.c.pending_stop)
        self.assertEqual(self.c.state,'MOTION_UNKNOWN')

    async def test_wait_condition_cannot_restore_already_lost_reference(self):
        self.c.trusted=False
        self.c.armed=False
        self.c.fault='位置计数异常'
        self.c._stop_wait_error(StopNotSettled('等待静止'))
        self.assertFalse(self.c.trusted)
        self.assertFalse(self.c.armed)
        self.assertEqual(self.c.fault,'位置计数异常')

    async def test_watchdog_uses_same_nonfault_wait_classification(self):
        self.c._stop_intent()
        self.c._confirm_stop=AsyncMock(side_effect=StopNotSettled('等待位置稳定'))
        task=asyncio.create_task(self.c._watchdog())
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertTrue(self.c.armed and self.c.trusted)
        self.assertEqual(self.c.state,'STOPPING')
        self.assertEqual(self.c.fault,'')
