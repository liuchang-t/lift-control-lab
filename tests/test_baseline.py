from __future__ import annotations
import asyncio
import tempfile
import unittest
from pathlib import Path
from lab.model import Config,LabError
from lab.transport import Simulator,Trace
from lab.legacy import Legacy


class BaselineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.cfg=Config(implementation='baseline',state_file=str(Path(self.tmp.name)/'zero.json'))
        self.sim=Simulator(self.cfg,Trace())
        self.old=Legacy(self.cfg,self.sim,Trace())
        await self.old.adapter.refresh_status()

    async def asyncTearDown(self):
        self.sim.faults['lost_stop']=0
        await self.old.close()
        self.tmp.cleanup()

    async def test_frozen_original_zero_speed_bug_is_reproducible(self):
        await self.old.command(dict(action='set_speed',speed=0))
        self.assertEqual(self.old.adapter._pv_speed_pr_s(),30000)

    async def test_frozen_original_lost_ack_bug_is_reproducible(self):
        self.sim.inject('lost_start_ack')
        with self.assertRaises(LabError):
            await self.old.command(dict(action='up'))
        self.assertEqual(self.sim.motion,'up')
        self.assertFalse(self.old.adapter._pv_active)
        self.assertFalse(await self.old.adapter.maybe_deadman_stop())

    async def test_baseline_has_same_normal_actions(self):
        await self.old.command(dict(action='set_retracted_zero'))
        self.assertTrue(self.old.settings.lift_position_trusted)
        await self.old.command(dict(action='goto',height=1))
        self.assertEqual(self.sim.motion,'pp')
        await self.old.command(dict(action='stop'))
        self.assertIsNone(self.sim.motion)

    def test_baseline_real_connection_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(mode='real',implementation='baseline').validate()
