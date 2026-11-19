import unittest
from unittest.mock import patch
from lab.driver import Driver
from lab.model import Config, LabError
from lab.transport import Simulator, Trace

class PPAccelerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg=Config(mode='sim',cm_per_rev=.35921486,home_enable_hold_s=0,home_disable_hold_s=0)
        self.trace=Trace();self.sim=Simulator(self.cfg,self.trace)
        self.sim.inject('physical_cm',5)
        self.driver=Driver(self.cfg,self.sim)

    def frames(self):
        return [bytes.fromhex(x['frame']) for x in self.trace.items if x['kind']=='tx']

    async def start(self):
        await self.driver.start_pp(20000,self.cfg.pulse(8),'up',lambda:None)

    async def test_desired_accelerations_are_converted_and_read_back_before_enable(self):
        self.cfg.pp_acceleration_cm_s2=1.2
        self.cfg.pp_deceleration_cm_s2=.8
        self.cfg.pp_emergency_deceleration_cm_s2=2.4
        await self.start()
        frames=self.frames()
        enable=next(i for i,f in enumerate(frames) if f[1]==6 and f[2:6]==bytes.fromhex('03 07 00 01'))
        for address,value,key in ((0x101b,1.2,'acceleration'),(0x101d,.8,'deceleration'),(0x101f,2.4,'emergency_deceleration')):
            expected=self.cfg.pulse(value)
            self.assertEqual(self.sim._i32(address),expected)
            self.assertEqual(self.driver.parameters[key],expected)
            index=next(i for i,f in enumerate(frames) if f[1]==16 and int.from_bytes(f[2:4],'big')==address)
            self.assertEqual(frames[index+1][1],3)
            self.assertEqual(int.from_bytes(frames[index+1][2:4],'big'),address)
            self.assertLess(index+1,enable)
        await self.driver.check_active_parameters('pp',lambda:None)
        self.assertFalse(any(f[1] in (6,16) and int.from_bytes(f[2:4],'big') in (0x0c0d,0x0502,0x0507,0x0509) for f in frames))

    async def test_none_preserves_actual_accelerations_without_writes(self):
        before={a:self.sim._i32(a) for a in (0x101b,0x101d,0x101f)}
        await self.start()
        for a,value in before.items():self.assertEqual(self.sim._i32(a),value)
        self.assertFalse(any(f[1] in (6,16) and int.from_bytes(f[2:4],'big') in before for f in self.frames()))

    async def test_readback_failure_prevents_enable_and_start(self):
        self.cfg.pp_acceleration_cm_s2=1
        original=self.driver._read_words
        async def mismatch(address,count):
            data=await original(address,count)
            return [0,0] if address==0x101b and count==2 else data
        with patch.object(self.driver,'_read_words',side_effect=mismatch):
            with self.assertRaises(LabError):await self.start()
        writes=[f[2:6] for f in self.frames() if f[1]==6]
        self.assertNotIn(bytes.fromhex('03 07 00 01'),writes)
        self.assertNotIn(bytes.fromhex('0d 08 00 03'),writes)

    def test_conversion_uses_position_rounding_without_speed_caps(self):
        self.assertEqual(self.cfg.acceleration_pr(.35921486),10000)
        self.assertEqual(self.cfg.acceleration_pr(.35921486*1.5/10000),2)
        self.assertGreater(self.cfg.acceleration_pr(100),self.cfg.max_pp_speed)
        self.assertEqual(self.cfg.acceleration_pr(100),self.cfg.pulse(100))

    def test_optional_settings_validate_finite_positive_int32_conversion(self):
        for name in ('pp_acceleration_cm_s2','pp_deceleration_cm_s2','pp_emergency_deceleration_cm_s2'):
            for value in (0,-1,True,float('nan'),float('inf'),1e-12,1e20):
                with self.subTest(name=name,value=value):
                    cfg=Config(**{name:value})
                    with self.assertRaises(ValueError):cfg.validate()
            Config(**{name:None}).validate()
            Config(**{name:1.2}).validate()
