import unittest
from unittest.mock import patch
from lab.registers import RegisterMonitor

class FreshnessTests(unittest.TestCase):
    def row(self, address, age, mode=None, epoch=1):
        monitor=RegisterMonitor()
        for offset in range(2):
            monitor.read_words[address+offset]=dict(value=0,at=100-age,epoch=1,source='read',serial=1)
        with patch('lab.registers.time.monotonic',return_value=100):
            return next(r for r in monitor.snapshot(epoch,stale_after=1,active_mode=mode)['registers'] if r['address']==address)

    def test_low_frequency_uses_full_scan_budget(self):
        self.assertFalse(self.row(0x101B,6)['stale'])
        self.assertFalse(self.row(0x101B,12)['stale'])
        self.assertTrue(self.row(0x101B,12.01)['stale'])

    def test_active_mode_selects_only_actively_read_parameters(self):
        for mode,address in [('pp',0x1019),('pv',0x4604),('home',0x1024)]:
            self.assertFalse(self.row(address,6)['stale'])
            self.assertTrue(self.row(address,2,mode)['stale'])
        self.assertFalse(self.row(0x1019,6,'pv')['stale'])
        self.assertFalse(self.row(0x102A,6,'pp')['stale'])

    def test_fast_feedback_retains_fast_expiry(self):
        self.assertFalse(self.row(0x0B00,.9)['stale'])
        self.assertTrue(self.row(0x0B00,1.1)['stale'])

    def test_unscheduled_observations_age_without_scheduled_expiry(self):
        for address in (0x0A28,0x0D08,0x0502,0x0202):
            row=self.row(address,1000)
            self.assertFalse(row['stale'])
            self.assertIsNone(row['stale_after_s'])
            self.assertEqual(row['age_s'],1000)
            self.assertTrue(self.row(address,0,epoch=2)['stale'])

    def test_incomplete_multiword_remains_invalid(self):
        monitor=RegisterMonitor()
        monitor.read_words[0x1019]=dict(value=1,at=100,epoch=1,source='read',serial=1)
        row=next(r for r in monitor.snapshot(1)['registers'] if r['address']==0x1019)
        self.assertTrue(row['stale'])
