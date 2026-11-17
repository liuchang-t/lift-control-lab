import tempfile
import time
import unittest
from pathlib import Path

from lab.model import Config
from lab.service import Service
from lab.readiness import build_readiness


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service(Config(state_file=str(Path(self.tmp.name) / 'zero.json')))
        # This fixture observes directly without starting Service lifecycle tasks.
        self.service.connected = True
        self.service.connection_state = 'connected'
        self.ctrl = self.service.controller
        self.sim = self.service.transport
        await self.ctrl.observe()
        await self.ctrl.driver.read_parameters()
        self.client = 'readiness-test'

    async def asyncTearDown(self):
        await self.sim.close()
        self.tmp.cleanup()

    def view(self):
        return build_readiness(self.service, self.client)

    async def test_disconnected_and_connecting_offer_no_motion_recovery(self):
        self.service.connected = False
        self.service.connection_state = 'disconnected'
        self.assertFalse(self.view()['can_move'])
        self.assertEqual([action['id'] for action in self.view()['actions']], ['connect'])
        self.service.connection_state = 'connecting'
        self.assertEqual(self.view()['actions'], [])

    def actions(self):
        return {action['id']: action for action in self.view()['actions']}

    async def prepared(self, height=5):
        self.sim.inject('physical_cm', height)
        await self.ctrl.observe()
        await self.ctrl.arm()
        self.ctrl.trusted = True
        self.ctrl.home_status = 'complete'

    async def test_initial_conditions_do_not_claim_motion_ready(self):
        view = self.view()
        self.assertFalse(view['can_move'])
        self.assertTrue(view['summary'])
        self.assertTrue(view['steps'])
        self.assertTrue(any(not check['ok'] for check in view['checks']))
        self.assertFalse(self.actions().get('home', {}).get('enabled', False))

    async def test_home_is_available_without_a_trusted_reference(self):
        await self.ctrl.arm()
        self.assertFalse(self.view()['can_move'])
        self.assertTrue(self.actions().get('home', {}).get('enabled', False))
        self.assertFalse(self.actions().get('up', {}).get('enabled', False))

    async def test_home_complete_below_working_range_directs_to_retracted_bound(self):
        await self.prepared(.05)
        actions = self.actions()
        self.assertTrue(actions.get('min', {}).get('enabled', False))
        self.assertFalse(actions.get('up', {}).get('enabled', False))
        self.assertFalse(actions.get('down', {}).get('enabled', False))
        self.assertTrue(self.view()['steps'])

    async def test_emergency_stop_preserves_reference_but_requires_arm(self):
        await self.prepared()
        await self.ctrl.stop(emergency=True)
        self.assertTrue(self.ctrl.trusted)
        self.assertFalse(self.view()['can_move'])
        self.assertTrue(self.actions().get('arm', {}).get('enabled', False))
        self.assertFalse(self.actions().get('up', {}).get('enabled', False))

    async def test_cancelled_homing_requires_home_again(self):
        await self.prepared()
        self.ctrl.mode = 'home'
        self.ctrl.home_status = 'homing'
        await self.ctrl.stop()
        self.assertFalse(self.ctrl.trusted)
        self.assertEqual(self.ctrl.home_status, 'cancelled')
        self.assertTrue(self.actions().get('home', {}).get('enabled', False))
        self.assertFalse(self.actions().get('up', {}).get('enabled', False))

    async def test_pending_stop_never_offers_arm_or_home(self):
        await self.prepared()
        self.ctrl._stop_intent()
        actions = self.actions()
        self.assertFalse(actions.get('arm', {}).get('enabled', False))
        self.assertFalse(actions.get('home', {}).get('enabled', False))
        self.assertFalse(actions.get('up', {}).get('enabled', False))
        self.assertFalse(self.view()['can_move'])

    async def test_stale_feedback_blocks_but_shared_clients_need_no_lease(self):
        await self.prepared()
        self.ctrl.sample_at = time.monotonic() - 10
        self.assertFalse(self.view()['can_move'])
        self.assertFalse(self.actions().get('up', {}).get('enabled', False))
        await self.ctrl.observe()
        self.assertTrue(self.view()['can_move'])
        self.assertTrue(build_readiness(self.service, 'another-client')['can_move'])
        self.assertNotIn('owner', [item['id'] for item in self.view()['checks']])

    async def test_external_motion_guidance_is_observational(self):
        await self.prepared()
        self.sim.inject('physical_cm', 6)
        await self.ctrl.observe()
        before = self.service.trace.serial
        view = self.view()
        self.assertIn('非本后端任务', view['summary'])
        self.assertNotIn('stop', [action['id'] for action in view['actions']])
        self.assertTrue(self.ctrl.trusted)
        self.assertEqual(self.service.trace.serial, before)

    async def test_eeprom_warning_is_not_resolved_by_fault_reset(self):
        await self.prepared()
        self.sim.regs[0x0C0D] = 1
        await self.ctrl.observe()
        self.assertFalse(self.view()['can_move'])
        self.assertFalse(self.actions().get('up', {}).get('enabled', False))
        self.assertFalse(self.actions().get('reset_fault', {}).get('enabled', False))

    async def test_unknown_alarm_does_not_offer_generic_reset(self):
        await self.prepared()
        self.sim.inject('alarm', 0xFFFF)
        await self.ctrl.observe()
        self.assertFalse(self.view()['can_move'])
        self.assertFalse(self.actions().get('reset_fault', {}).get('enabled', False))
        self.assertFalse(self.actions().get('home', {}).get('enabled', False))

    async def test_negative_limit_with_untrusted_reference_can_offer_home_not_jog(self):
        await self.ctrl.arm()
        self.sim.inject('alarm', 0x952)
        await self.ctrl.observe()
        actions = self.actions()
        self.assertTrue(actions.get('home', {}).get('enabled', False))
        self.assertFalse(actions.get('up', {}).get('enabled', False))
        self.assertFalse(actions.get('reset_fault', {}).get('enabled', False))

    async def test_positive_limit_with_unknown_reference_does_not_offer_escape(self):
        await self.prepared()
        self.sim.inject('physical_cm', 181)
        self.sim.inject('alarm', 0x950)
        await self.ctrl.observe()
        self.ctrl.trusted = False
        actions = self.actions()
        self.assertFalse(actions.get('home', {}).get('enabled', False))
        self.assertFalse(actions.get('down', {}).get('enabled', False))
        self.assertFalse(actions.get('reset_fault', {}).get('enabled', False))


    async def test_reset_success_requires_arm_without_unnecessarily_rehoming(self):
        await self.prepared()
        self.sim.inject('alarm', 0x620)
        await self.ctrl.observe()
        await self.ctrl.reset_fault({'confirm_reset': True})
        self.assertTrue(self.ctrl.trusted)
        self.assertFalse(self.view()['can_move'])
        self.assertTrue(self.actions().get('arm', {}).get('enabled', False))
        self.assertNotIn('home', self.actions())

    async def test_manual_takeover_explains_exit_and_does_not_offer_normal_start(self):
        await self.prepared()
        self.ctrl.manual_active = True
        self.assertTrue(self.view()['manual'])
        self.assertFalse(self.view()['can_move'])
        self.assertTrue(self.actions().get('exit_manual', {}).get('enabled', False))
        self.assertNotIn('arm', self.actions())
        self.assertNotIn('home', self.actions())
        self.ctrl.manual_failed = True
        self.assertTrue(self.view()['steps'])
        self.assertFalse(self.view()['can_move'])

    async def test_single_move_rejection_explains_the_configured_distance(self):
        await self.prepared()
        self.service.last_rejection = {'message': '目标超过配置的单次位移上限'}
        view = self.view()
        self.assertEqual(view['last_rejection'], self.service.last_rejection)
        bound = '%.2f' % self.service.config.cm(self.service.config.max_move_pulse)
        self.assertTrue(any(bound in step for step in view['steps']))

    async def test_known_upper_limit_outside_work_range_offers_only_return_bound(self):
        await self.prepared()
        self.ctrl.trusted = False
        self.sim.inject('physical_cm', 181)
        self.sim.inject('alarm', 0x950)
        await self.ctrl.observe()
        # Observe the stationary endpoint after the injected repositioning.
        await self.ctrl.observe()
        self.ctrl.trusted = True
        self.assertFalse(self.view()['can_move'])
        actions = self.actions()
        self.assertTrue(actions.get('max', {}).get('enabled', False))
        self.assertNotIn('min', actions)
        self.assertNotIn('home', actions)
        self.assertFalse(actions.get('reset_fault', {}).get('enabled', False))

    async def test_zero_speeds_do_not_claim_normal_motion_is_ready(self):
        await self.prepared()
        self.ctrl.speeds = {'pp': 0., 'pv': 0.}
        view = self.view()
        self.assertFalse(view['can_move'])
        self.assertTrue(any(not item['ok'] for item in view['checks']))

    async def test_known_invalid_home_parameters_disable_home(self):
        await self.ctrl.arm()
        self.ctrl.driver.parameters['home_method'] = 0
        self.assertFalse(self.actions().get('home', {}).get('enabled', False))
        self.ctrl.driver.parameters['home_method'] = 17
        self.ctrl.driver.parameters['home_offset'] = self.service.config.home_reference_pulse + 100
        self.assertFalse(self.actions().get('home', {}).get('enabled', False))

    async def test_zero_pp_speed_does_not_offer_return_from_home_as_ready(self):
        await self.prepared(.05)
        self.ctrl.speeds['pp'] = 0.
        self.assertFalse(self.actions().get('min', {}).get('enabled', False))
        self.assertFalse(self.view()['can_move'])

if __name__ == '__main__':
    unittest.main()

