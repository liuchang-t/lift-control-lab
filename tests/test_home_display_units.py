import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from lab.model import Config, LabError, load_config
from lab.service import Service

class HomeDisplayUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_display_is_derived_and_not_persisted(self):
        cfg=Config(cm_per_rev=.35921486,home_speed=30000,home_creep_speed=None,home_acceleration=20000)
        display=cfg.public(include_display=True)['home_display']
        self.assertEqual(display['home_speed_cm_s'],cfg.cm(30000))
        self.assertIsNone(display['home_creep_speed_cm_s'])
        self.assertEqual(display['home_acceleration_cm_s2'],cfg.cm(20000))
        self.assertNotIn('home_display',asdict(cfg))
        self.assertEqual(cfg.home_speed,30000)

    async def test_cm_input_persists_legacy_units_and_reloads_without_device_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch('lab.service.ROOT',root),patch('lab.model.ROOT',root):
                cfg=Config(mode='sim',cm_per_rev=.35921486,state_file=str(root/'state.json'))
                service=Service(cfg)
                result=await service.update_config({'home_speed_cm_s':1.2,'home_creep_speed_cm_s':.2,'home_acceleration_cm_s2':2.4})
                saved=json.loads((root/'.runtime/config.json').read_text())
                for display,stored,value in [('home_speed_cm_s','home_speed',1.2),('home_creep_speed_cm_s','home_creep_speed',.2),('home_acceleration_cm_s2','home_acceleration',2.4)]:
                    self.assertEqual(saved[stored],cfg.pulse(value))
                    self.assertNotIn(display,saved)
                    self.assertEqual(getattr(load_config(None),stored),cfg.pulse(value))
                    self.assertEqual(result['home_display'][display],cfg.cm(cfg.pulse(value)))
                self.assertEqual(list(service.trace.items),[])
                self.assertFalse(service.connected)
                await service.close()

    async def test_none_and_unedited_legacy_value_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp,patch('lab.service.ROOT',Path(tmp)):
            cfg=Config(mode='sim',home_speed=30001,home_creep_speed=5001,home_acceleration=20001)
            service=Service(cfg)
            result=await service.update_config({'home_creep_speed_cm_s':None})
            self.assertEqual(cfg.home_speed,30001)
            self.assertEqual(cfg.home_acceleration,20001)
            self.assertIsNone(cfg.home_creep_speed)
            self.assertIsNone(result['home_display']['home_creep_speed_cm_s'])
            await service.close()

    async def test_ambiguous_units_and_invalid_values_rejected_without_io(self):
        service=Service(Config(mode='sim'))
        for body in ({'home_speed':10000,'home_speed_cm_s':.3},
                     {'home_speed_cm_s':0},{'home_creep_speed_cm_s':float('nan')},
                     {'home_acceleration_cm_s2':True},{'home_speed_cm_s':1e20}):
            with self.subTest(body=body):
                with self.assertRaises(LabError):await service.update_config(body)
        self.assertEqual(list(service.trace.items),[])
        await service.close()

    def test_display_round_trip_keeps_legacy_integer_and_same_rounding(self):
        cfg=Config(cm_per_rev=.35921486)
        for value in (1,9999,30001,2147483647):
            self.assertEqual(cfg.home_display_input(cfg.cm(value)),value)
        self.assertEqual(cfg.home_display_input(.35921486*1.5/10000),2)
        self.assertGreater(cfg.home_display_input(100),cfg.max_pp_speed)

    def test_default_public_remains_config_constructor_and_file_compatible(self):
        cfg=Config(mode='sim',cm_per_rev=.35921486,home_speed=30001)
        self.assertNotIn('home_display',cfg.public())
        self.assertEqual(Config(**cfg.public()).home_speed,30001)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'config.json'
            path.write_text(json.dumps(cfg.public()))
            with patch('lab.model.ROOT',root):
                loaded=load_config(str(path))
            self.assertEqual(loaded.home_speed,30001)
            self.assertEqual(loaded.cm_per_rev,cfg.cm_per_rev)

    async def test_config_get_includes_display_without_device_io(self):
        service=Service(Config(mode='sim',home_speed=30001))
        result=await service.request('GET','/api/config',{}, {})
        self.assertEqual(result['home_display']['home_speed_cm_s'],service.config.cm(30001))
        self.assertEqual(list(service.trace.items),[])
        await service.close()
