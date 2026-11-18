import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from lab.model import Config
from lab.service import Service
from lab.units import Units, signed_word


class LoadTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_word_is_shared_by_status_and_register_monitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Service(Config(mode='sim', state_file=str(Path(tmp)/'state.json')))
            original_tick=s.transport.tick
            def tick():
                original_tick()
                s.transport.regs[0x0B02]=raw
            try:
                for raw,expected in ((0,0),(100,100),(0xFFFF,-1),(0xFF9C,-100),(0x8000,-32768),(0x7FFF,32767)):
                    with patch.object(s.transport,'tick',side_effect=tick):
                        value=await s.controller.observe()
                    self.assertEqual(value['load_raw'],raw)
                    self.assertEqual(value['load_signed'],expected)
                    self.assertIsNone(value['load_percent'])
                    row=next(x for x in s.register_snapshot()['registers'] if x['address']==0x0B02)
                    self.assertEqual(row['value'],expected)
                    self.assertEqual(row['raw_words'],[raw])
            finally:
                await s.close()


class ConversionEvidenceTests(unittest.TestCase):
    def test_signed_word_rejects_non_words(self):
        for value in (-1,65536,True,1.5):
            with self.assertRaises(ValueError): signed_word(value)

    def test_two_point_calibration_preserves_reference_and_roundtrips(self):
        counts=5112707-(-1224)
        travel=270-86.3
        ratio=travel*10000/counts
        old=Units(10000,.35)
        calibrated=Units(10000,ratio)
        self.assertAlmostEqual(old.cm(counts),178.987585)
        self.assertAlmostEqual(calibrated.cm(counts),183.7)
        self.assertEqual(calibrated.pulse(travel),counts)
        self.assertAlmostEqual(calibrated.cm(-1224),-.04396789866738522)
        self.assertAlmostEqual(calibrated.rpm_to_cm_s(60),ratio)
