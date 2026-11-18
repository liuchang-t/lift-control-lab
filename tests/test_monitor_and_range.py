import tempfile
import unittest
from pathlib import Path
from lab.model import Config, LabError
from lab.service import Service
from lab.readiness import build_readiness
from lab.registers import MONITOR_ADDRESSES


class MonitorAndRangeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.s=Service(Config(mode='sim', state_file=str(Path(self.tmp.name)/'state.json')))
        self.s.connected=True
        self.s.connection_state='connected'
        self.c=self.s.controller
        self.sim=self.s.transport
        self.sim.inject('physical_cm',5)
        await self.c.arm()
        self.c.trusted=True

    async def asyncTearDown(self):
        await self.s.close()
        self.tmp.cleanup()

    def rows(self):
        return {r['address']:r for r in self.s.register_snapshot()['registers']}

    async def test_jog_readback_survives_write_ack_and_updates_on_next_sample(self):
        await self.c.driver.wire.write_register(0x0D0B,1)
        await self.c.observe()
        await self.c.driver.wire.write_register(0x0D0B,65)
        row=self.rows()[0x0D0B]
        self.assertEqual((row['value'],row['source'],row['last_write_value']),(1,'read',65))
        await self.c.observe()
        self.assertEqual(self.rows()[0x0D0B]['value'],65)

    async def test_fixed_rows_hidden_and_age_does_not_expire_motion_monitor(self):
        self.assertFalse(self.rows()[0x0308]['visible'])
        for address,item in self.c.driver.parameter_words.items():
            if address not in MONITOR_ADDRESSES: item['at']-=1000
        self.c.driver.require_monitor_fresh()
        self.assertEqual(self.rows()[0x0502]['polling_kind'],'on_demand')

    async def test_limit_rejection_clears_only_with_fresh_clear_feedback(self):
        self.s.last_rejection={'message':'物理限位禁止该方向','at':1}
        self.sim.inject('physical_cm',181)
        await self.c.observe()
        self.assertIsNotNone(build_readiness(self.s,'test')['last_rejection'])
        self.sim.inject('physical_cm',5)
        await self.c.observe()
        self.c.sample_at-=100
        self.assertIsNotNone(build_readiness(self.s,'test')['last_rejection'])
        await self.c.observe()
        self.assertIsNone(build_readiness(self.s,'test')['last_rejection'])
        self.s.last_rejection={'message':'过载'}
        self.assertIsNotNone(build_readiness(self.s,'test')['last_rejection'])

    async def test_pp_from_either_side_to_in_range_target(self):
        self.c.config.soft_lower_cm=10
        self.c.config.soft_upper_cm=20
        for start,target in ((5,15),(25,12)):
            self.sim.inject('physical_cm',start)
            await self.c.observe()
            await self.c.observe()
            await self.c.command({'action':'goto','target_cm':target})
            self.assertEqual(self.c.state,'MOVING')
            self.assertEqual(self.c.direction,'up' if start<target else 'down')
            await self.c.stop()
            self.assertTrue(self.c.trusted)

    async def test_pp_still_rejects_unknown_reference_and_outside_target(self):
        self.c.config.soft_lower_cm=10
        self.c.config.soft_upper_cm=20
        for target in (5,25):
            with self.assertRaises(LabError): await self.c.command({'action':'goto','target_cm':target})
        self.c.trusted=False
        with self.assertRaises(LabError): await self.c.command({'action':'goto','target_cm':15})
        self.assertIsNone(self.sim.motion)
