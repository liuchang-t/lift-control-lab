import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from lab.model import Config, load_config
from lab.service import Service


class HomeHoldConfigTests(unittest.TestCase):
    def test_defaults_are_explicit_experimental_holds(self):
        config = Config().validate()
        self.assertEqual(config.home_enable_hold_s, 4.0)
        self.assertEqual(config.home_disable_hold_s, 0.2)

    def test_zero_disables_optional_holding(self):
        config = Config(home_enable_hold_s=0, home_disable_hold_s=0).validate()
        self.assertEqual(config.home_enable_hold_s, 0)
        self.assertEqual(config.home_disable_hold_s, 0)

    def test_invalid_or_unbounded_hold_times_are_rejected(self):
        for field in ('home_enable_hold_s', 'home_disable_hold_s'):
            for value in (-0.1, float('nan'), float('inf'), float('-inf'), 10.01):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        Config(**{field: value}).validate()


class HomeHoldPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_hold_settings_override_startup_file_without_connecting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            example = root / 'example.json'
            example.write_text(json.dumps({
                'home_enable_hold_s': 4.0,
                'home_disable_hold_s': 0.2,
            }))
            with patch('lab.service.ROOT', root), patch('lab.model.ROOT', root), \
                    patch('asyncio.open_connection', new_callable=AsyncMock) as connect:
                service = Service(Config(mode='sim', state_file=str(root / 'reference.json')))
                try:
                    settings = {'home_enable_hold_s': 2.5, 'home_disable_hold_s': 0.4,
                                'home_speed': 30000, 'home_creep_speed': 5000, 'home_acceleration': 20000}
                    result = await service.update_config(settings)
                    for name, value in settings.items():
                        self.assertEqual(result[name], value)
                    saved = json.loads((root / '.runtime' / 'config.json').read_text())
                    for startup in (None, str(example)):
                        loaded = load_config(startup)
                        for name, value in settings.items():
                            self.assertEqual(saved[name], value)
                            self.assertEqual(getattr(loaded, name), value)
                    self.assertEqual(json.loads(example.read_text())['home_enable_hold_s'], 4.0)
                    self.assertFalse(service.connected)
                    self.assertEqual(service.controller._tasks, [])
                    self.assertEqual(list(service.trace.items), [])
                    connect.assert_not_awaited()
                finally:
                    await service.close()
                connect.assert_not_awaited()


class HomeMotionParameterValidationTests(unittest.TestCase):
    def test_unset_preserves_actual_and_override_does_not_mutate_observation(self):
        actual=dict(home_speed=20000,home_creep_speed=10000,home_acceleration=10000)
        self.assertEqual(Config().home_parameters(actual),actual)
        desired=Config(home_speed=30000).home_parameters(actual)
        self.assertEqual(desired['home_speed'],30000)
        self.assertEqual(desired['home_creep_speed'],10000)
        self.assertEqual(actual['home_speed'],20000)

    def test_integer_positive_wire_range(self):
        for key in ('home_speed','home_creep_speed','home_acceleration'):
            for value in (0,-1,1.2,True,'20000',float('inf'),2147483648):
                with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                    Config(**{key:value}).validate()
            Config(**{key:None}).validate()
            Config(**{key:2147483647}).validate()
