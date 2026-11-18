import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from lab.model import Config, atomic_json_write, load_config
from lab.service import Service
from lab.server import create_service

class ConfigPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_save_survives_restart_with_old_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            example=root/'example.json'
            example.write_text(json.dumps({'cm_per_rev':.35,'travel_cm':180,'soft_upper_cm':180,'host':'192.0.2.1'}))
            service=Service(Config(mode='sim', state_file=str(root/'state.json')))
            try:
                with patch('lab.service.ROOT',root),patch('lab.model.ROOT',root):
                    await service.update_config({'cm_per_rev':.35921486,'travel_cm':183.7,'soft_lower_cm':1,'soft_upper_cm':182})
                    await service.save_connection({'mode':'real','host':'192.0.2.2','port':8899,'slave_id':1,'timeout':.5})
                    for startup in (None,str(example)):
                        cfg=load_config(startup)
                        self.assertEqual((cfg.cm_per_rev,cfg.travel_cm,cfg.soft_lower_cm,cfg.soft_upper_cm),(.35921486,183.7,1,182))
                        self.assertEqual((cfg.mode,cfg.host),('real','192.0.2.2'))
                    self.assertEqual(json.loads(example.read_text())['soft_upper_cm'],180)
                self.assertEqual(list(service.trace.items),[])
            finally:
                await service.close()

    async def test_restored_real_startup_never_connects_or_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Config(mode='real',state_file=str(Path(tmp)/'state.json'))
            with patch('asyncio.open_connection',new_callable=AsyncMock) as connect:
                service=await create_service(cfg)
                try:
                    self.assertFalse(service.connected)
                    self.assertEqual(service.connection_state,'disconnected')
                    self.assertEqual(service.controller._tasks,[])
                    self.assertEqual(list(service.trace.items),[])
                    connect.assert_not_awaited()
                finally:
                    await service.close()
                connect.assert_not_awaited()

    async def test_simulated_startup_still_runs_without_real_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('asyncio.open_connection',new_callable=AsyncMock) as connect:
                service=await create_service(Config(mode='sim', state_file=str(Path(tmp)/'state.json')))
                try:
                    self.assertTrue(service.connected)
                    connect.assert_not_awaited()
                finally:
                    await service.close()

    async def test_invalid_saved_file_does_not_fall_back_to_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);example=root/'example.json';example.write_text('{}')
            saved=root/'.runtime/config.json';saved.parent.mkdir()
            with patch('lab.model.ROOT',root):
                for text in ('{bad','[]','{"soft_upper_cm":-1}'):
                    saved.write_text(text)
                    with self.assertRaises(ValueError):load_config(str(example))
                saved.unlink()
                example.write_text('{"soft_upper_cm":170}')
                self.assertEqual(load_config(str(example)).soft_upper_cm,170)

    async def test_default_real_configuration_starts_disconnected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch('lab.model.ROOT',root):
                cfg=load_config()
            self.assertEqual(cfg.mode,'real')
            self.assertEqual(Config().mode,'real')
            cfg.state_file=str(root/'state.json')
            with patch('asyncio.open_connection',new_callable=AsyncMock) as connect:
                service=await create_service(cfg)
                try:
                    self.assertFalse(service.connected)
                    self.assertFalse(service.controller.armed)
                    self.assertEqual(service.controller._tasks,[])
                    self.assertEqual(list(service.trace.items),[])
                finally:
                    await service.close()
                connect.assert_not_awaited()

    async def test_save_after_jog_ack_checks_feedback_without_control_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            service=Service(Config(mode='sim',state_file=str(root/'state.json')))
            service.connected=True
            service.connection_state='connected'
            ctrl=service.controller
            service.transport.inject('physical_cm',5)
            try:
                await ctrl.arm()
                await ctrl.command({'action':'up','intent_id':'save-test'})
                await ctrl.stop()
                self.assertEqual(ctrl.snapshot()['details']['motion'],'stop_acknowledged')
                service.trace.clear()
                with patch('lab.service.ROOT',root):
                    await service.update_config({'soft_upper_cm':175})
                saved=json.loads((root/'.runtime/config.json').read_text())
                self.assertEqual(saved['soft_upper_cm'],175)
                self.assertEqual(ctrl.stop_confirmation,'ack')
                requests=[bytes.fromhex(x['frame']) for x in service.trace.items if x['kind']=='tx']
                self.assertTrue(requests)
                self.assertTrue(all(frame[1]==3 for frame in requests))
            finally:
                await service.close()

    async def test_save_after_stop_ack_rejects_motion_and_changed_control_state(self):
        from lab.model import LabError
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            service=Service(Config(mode='sim',state_file=str(root/'state.json')))
            service.connected=True
            service.connection_state='connected'
            ctrl=service.controller
            ctrl.stop_confirmation='ack'
            try:
                cases=[
                    [{'speed_rpm':8,'position_feedback':100}]*2,
                    [{'speed_rpm':0,'position_feedback':100},{'speed_rpm':0,'position_feedback':130}],
                ]
                with patch('lab.service.ROOT',root):
                    for readings in cases:
                        with patch.object(ctrl,'observe',new_callable=AsyncMock,side_effect=readings):
                            with self.assertRaisesRegex(LabError,'尚未确认静止'):
                                await service.update_config({'soft_upper_cm':175})
                        self.assertFalse((root/'.runtime/config.json').exists())
                        self.assertEqual(service.config.soft_upper_cm,180)
                    async def changed():
                        ctrl.epoch+=1
                        return {'speed_rpm':0,'position_feedback':100}
                    with patch.object(ctrl,'observe',new_callable=AsyncMock,side_effect=changed):
                        with self.assertRaisesRegex(LabError,'控制状态已变化'):
                            await service.update_config({'soft_upper_cm':175})
                    self.assertFalse((root/'.runtime/config.json').exists())
                self.assertEqual(list(service.trace.items),[])
            finally:
                service.connected=False
                await service.close()
