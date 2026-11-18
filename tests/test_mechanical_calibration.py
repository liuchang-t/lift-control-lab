import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.model import Config, SpeedStore, atomic_json_write, load_config

class MechanicalCalibrationTests(unittest.TestCase):
    def test_saved_ratio_restores_shared_height_and_speed_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            values=Config(mode='sim', cm_per_rev=.35921486,state_file=str(root/'position.json')).public()
            atomic_json_write(root/'.runtime/config.json',values)
            cfg=Config(**values)
            SpeedStore(cfg).save({'pp':1.05,'pv':3.0})
            with patch('lab.model.ROOT',root): restored=load_config()
            self.assertEqual(restored.cm_per_rev,.35921486)
            self.assertEqual(SpeedStore(restored).load(restored),{'pp':1.05,'pv':3.0})
            self.assertAlmostEqual(restored.cm(10000),.35921486)
            self.assertAlmostEqual(restored.units.rpm_to_cm_s(60),.35921486)
            self.assertEqual(restored.speed_pr(1.05,'pp'),29230)
            self.assertEqual(restored.units.jog_rpm(3.0),501)
            self.assertEqual(restored.units.target(10),restored.pulse(10))
            self.assertAlmostEqual(restored.cm(5113931),183.7,places=5)

    def test_limit_parameters_can_be_saved_with_matching_travel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=Config(mode='sim', cm_per_rev=.35921486,travel_cm=183.7,soft_lower_cm=1,soft_upper_cm=182.156).validate()
            atomic_json_write(root/'.runtime/config.json',cfg.public())
            with patch('lab.model.ROOT',root): restored=load_config()
            self.assertEqual(restored.soft_upper_cm,182.156)
            self.assertEqual(restored.soft_lower_cm,1)
            with self.assertRaises(ValueError): Config(mode='sim', travel_cm=180,soft_upper_cm=182.156).validate()
