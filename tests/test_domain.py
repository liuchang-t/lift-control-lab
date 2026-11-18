"""Persistent settings, units and manual-driven policy without hardware."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab.alarms import CATALOG, blocked, describe
from lab.model import Config, SpeedStore, load_config
from lab.units import Units


class UnitsTests(unittest.TestCase):
    def test_rounding_is_half_away_from_zero_and_not_display_precision(self):
        units=Units(10000,.35)
        self.assertEqual(units.pulse(.0000175),1)
        self.assertEqual(units.pulse(-.0000175),-1)
        self.assertAlmostEqual(units.cm(1),.000035,places=12)
        self.assertEqual(units.pulse(35),1000000)
        self.assertEqual(units.target(35,120000),1120000)
        self.assertEqual(units.rpm(100000),600)
        self.assertAlmostEqual(units.rpm_to_cm_s(600),3.5)

    def test_wire_ranges_and_invalid_numbers_are_rejected(self):
        units=Units(10000,.35)
        for value in (float('nan'),float('inf'),True):
            with self.assertRaises(ValueError):
                units.pulse(value)
        with self.assertRaises(ValueError):
            units.target(1000000)
        with self.assertRaises(ValueError):
            units.percent_to_cm(101,180)
        self.assertEqual(units.percent_to_cm(50,180),90)

    def test_pp_pv_ranges_are_independent_and_zero_is_zero(self):
        cfg=Config(mode='sim', min_pp_speed=1000,max_pp_speed=50000,min_pv_speed=5000,max_pv_speed=100000).validate()
        self.assertEqual(cfg.speed_pr(0,'pp'),0)
        self.assertEqual(cfg.speed_pr(0,'pv'),0)
        self.assertEqual(cfg.speed_pr(cfg.cm(2000),'pp'),2000)
        with self.assertRaises(ValueError):
            cfg.speed_pr(cfg.cm(2000),'pv')
        with self.assertRaises(ValueError):
            cfg.speed_pr(cfg.cm(60000),'pp')
        self.assertEqual(cfg.speed_pr(cfg.cm(60000),'pv'),60000)
        with self.assertRaises(ValueError):
            cfg.speed_pr(.5,'unselected')

    def test_legacy_minimum_migrates_and_limit_order_is_checked(self):
        cfg=Config(mode='sim', min_speed=4000).validate()
        self.assertEqual((cfg.min_pp_speed,cfg.min_pv_speed),(4000,4000))
        self.assertEqual(cfg.soft_lower_cm,1)
        for values in (dict(min_pp_speed=100001),dict(min_pv_speed=0),
                       dict(soft_lower_cm=2,soft_upper_cm=1),dict(soft_upper_cm=181)):
            with self.assertRaises(ValueError):
                Config(**values).validate()

    def test_stopping_distance_includes_delay_and_margin(self):
        cfg=Config(mode='sim', verified_deceleration_cm_s2=2,control_latency_s=1,limit_margin_cm=.1)
        self.assertAlmostEqual(cfg.stopping_distance(2),3.1)
        self.assertEqual(cfg.stopping_distance(-2),cfg.stopping_distance(2))


class SpeedStoreTests(unittest.TestCase):
    def test_restart_restores_cm_s_without_link_to_new_maximum(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config(mode='sim', state_file=str(Path(tmp)/'zero.json'))
            SpeedStore(cfg).save({'pp':1.2,'pv':.4})
            restarted=Config(mode='sim', state_file=cfg.state_file,max_pp_speed=60000,max_pv_speed=80000)
            self.assertEqual(SpeedStore(restarted).load(restarted),{'pp':1.2,'pv':.4})

    def test_sim_real_and_device_profiles_are_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config(mode='sim', state_file=str(Path(tmp)/'zero.json'))
            store=SpeedStore(cfg)
            store.save({'pp':1.2,'pv':.4})
            for changes in (dict(mode='real'),dict(host='other'),dict(slave_id=2)):
                other=Config(state_file=cfg.state_file,**{'mode':'sim',**changes})
                self.assertNotEqual(store.path,SpeedStore(other).path)
                self.assertNotEqual(SpeedStore(other).load(other),{'pp':1.2,'pv':.4})

    def test_corrupt_or_out_of_range_files_do_not_silently_restore_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config(mode='sim', state_file=str(Path(tmp)/'zero.json'))
            store=SpeedStore(cfg)
            store.save({'pp':1.2,'pv':.4})
            original=store.path.read_text()
            store.path.write_text('{bad json')
            with self.assertRaises(ValueError):
                store.load(cfg)
            store.path.write_text(original)
            restricted=Config(mode='sim', state_file=cfg.state_file,max_pp_speed=10000)
            with self.assertRaises(ValueError):
                SpeedStore(restricted).load(restricted)

    def test_failed_atomic_replace_keeps_previous_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config(mode='sim', state_file=str(Path(tmp)/'zero.json'))
            store=SpeedStore(cfg)
            store.save({'pp':1.2,'pv':.4})
            with patch('lab.model.os.replace',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    store.save({'pp':1.4,'pv':.5})
            self.assertEqual(store.load(cfg),{'pp':1.2,'pv':.4})


class ConfigLoadTests(unittest.TestCase):
    def test_saved_configuration_overrides_startup_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'.runtime').mkdir()
            (root/'.runtime/config.json').write_text(json.dumps({'soft_lower_cm':1.2,'min_pp_speed':4000}))
            explicit=root/'explicit.json'
            explicit.write_text(json.dumps({'soft_lower_cm':1.5,'min_pp_speed':5000}))
            with patch('lab.model.ROOT',root):
                restored=load_config()
                self.assertEqual(restored.soft_lower_cm,1.2)
                self.assertEqual(restored.min_pp_speed,4000)
                self.assertEqual(load_config(str(explicit)).soft_lower_cm,1.2)

    def test_missing_explicit_or_corrupt_saved_configuration_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch('lab.model.ROOT',root):
                self.assertEqual(load_config().soft_lower_cm,1)
                with self.assertRaises(FileNotFoundError):
                    load_config(str(root/'missing.json'))
                (root/'.runtime').mkdir()
                (root/'.runtime/config.json').write_text('{bad')
                with self.assertRaises(ValueError):
                    load_config()


class AlarmPolicyTests(unittest.TestCase):
    def test_catalog_covers_every_code_in_manual_tables(self):
        codes='101 104 105 108 111 120 121 122 130 131 136 201 207 208 210 220 234 400 410 420 430 500 510 602 610 620 625 626 630 650 731 733 735 740 834 835 A33 A34 A35 B00 B01 B03 D09 D10 110 601 730 831 900 909 920 922 939 941 942 950 952 980 A40'
        self.assertEqual(set(CATALOG),{int(x,16) for x in codes.split()})
        for code in CATALOG:
            alarm=describe(code)
            self.assertTrue(alarm['advice'])
            self.assertIn(alarm['source_pdf_page'],(27,28,29,30))
            self.assertIn(alarm['manual_category'],('fault','warning'))
            self.assertEqual(alarm['raw_code'],code)

    def test_raw_unknown_and_prefix_evidence_are_retained(self):
        self.assertEqual(describe(0x6B00)['code'],0xB00)
        self.assertEqual(describe(0x6B00)['raw_code'],0x6B00)
        unknown=describe(0x6121)
        self.assertEqual(unknown['manual_category'],'unknown')
        self.assertEqual(unknown['code'],0x6121)
        self.assertFalse(unknown['reset_allowed'])
        self.assertTrue(unknown['reference_lost'])

    def test_manual_reset_support_does_not_automatically_allow_reset(self):
        self.assertTrue(describe(0x731)['manual_resettable'])
        self.assertFalse(describe(0x731)['reset_allowed'])
        self.assertFalse(describe(0x104)['reset_allowed'])
        self.assertTrue(describe(0xB00)['reset_allowed'])
        self.assertIn('无电机抱闸',describe(0x625)['advice'])

    def test_directional_exceptions_require_matching_live_di(self):
        base=dict(di4_function=14,di5_function=15,di4_logic=0,di5_logic=0,
                  upper_limit=False,lower_limit=False,alarm_code=0)
        for code in CATALOG:
            if code not in (0x950,0x952):
                self.assertTrue(blocked(dict(base,alarm_code=code),'up'))
        upper=dict(base,alarm_code=0x950,upper_limit=True)
        self.assertFalse(blocked(upper,'down'))
        self.assertTrue(blocked(upper,'up'))
        self.assertTrue(blocked(dict(upper,upper_limit=False),'down'))
        lower=dict(base,alarm_code=0x952,lower_limit=True)
        self.assertFalse(blocked(lower,'up'))
        self.assertFalse(blocked(lower,homing=True))
        self.assertTrue(blocked(dict(lower,upper_limit=True),'up'))
        self.assertTrue(blocked(dict(base,di5_function=0),'up'))


if __name__=='__main__':
    unittest.main()
