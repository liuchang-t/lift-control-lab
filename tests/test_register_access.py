import unittest
from lab.registers import CATALOG, REFRESH_EXCLUDED

class RegisterAccessTests(unittest.TestCase):
    def test_readonly_feedback_and_fixed_encoder_parameters(self):
        actual={row[0] for row in CATALOG if row[4]=='R'}
        self.assertEqual(actual,{0x001F,0x0B00,0x0B02,0x0B03,0x0B04,0x0B07,0x0B22})

    def test_configurable_parameters_are_read_write_even_if_tool_only_reads(self):
        rows={row[0]:row for row in CATALOG}
        for address in (0x0C0D,0x0C1A,0x0502,0x0507,0x0509,0x0202,0x0306,0x030B,0x0A28,0x0A29,0x0A2B,0x1010,0x1012,0x1014,0x1016,0x101B,0x101D,0x101F,0x1024,0x1026,0x1028):
            self.assertEqual(rows[address][4],'RW',rows[address][2])
        self.assertEqual(sum(row[4]=='RW' for row in CATALOG),38)

    def test_trigger_readability_does_not_change_refresh_policy(self):
        rows={row[0]:row for row in CATALOG}
        self.assertEqual(REFRESH_EXCLUDED,{0x0D01,0x0D08})
        for address in (0x0D01,0x0D08,0x0D11,0x0D12):
            self.assertEqual(rows[address][4],'RW')
        self.assertNotIn(0x0D11,REFRESH_EXCLUDED)
        self.assertNotIn(0x0D12,REFRESH_EXCLUDED)
