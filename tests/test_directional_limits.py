import unittest
from lab.alarms import blocked, jog_blocked, describe

class DirectionalLimitTests(unittest.TestCase):
    def test_pp_and_jog_follow_di_when_warning_direction_disagrees(self):
        for guard in (blocked,jog_blocked):
            for code in (0xE950,0xE952):
                for upper,lower,allowed,denied in ((True,False,'down','up'),(False,True,'up','down')):
                    with self.subTest(guard=guard.__name__,code=code,upper=upper):
                        sample=dict(di4_function=14,di5_function=15,di4_logic=0,di5_logic=0,
                                    upper_limit=upper,lower_limit=lower,alarm=describe(code),alarm_code=code)
                        self.assertEqual(guard(sample,allowed),'')
                        self.assertEqual(guard(sample,denied),'物理限位禁止该方向')

    def test_warning_without_active_di_does_not_choose_a_direction(self):
        for guard in (blocked,jog_blocked):
            for code in (0xE950,0xE952):
                sample=dict(di4_function=14,di5_function=15,di4_logic=0,di5_logic=0,
                            upper_limit=False,lower_limit=False,alarm=describe(code),alarm_code=code)
                for direction in ('up','down'):self.assertEqual(guard(sample,direction),'')
