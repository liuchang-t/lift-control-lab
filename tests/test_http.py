from __future__ import annotations
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        cls.tmp=tempfile.TemporaryDirectory()
        sock=socket.socket();sock.bind(('127.0.0.1',0));cls.port=sock.getsockname()[1];sock.close()
        config=Path(cls.tmp.name)/'config.json'
        config.write_text(json.dumps(dict(mode='sim',state_file=str(Path(cls.tmp.name)/'zero.json'),bridge_enabled=True,bridge_token='http-test',poll_s=.03)),encoding='utf-8')
        # Isolate persisted settings too: startup files no longer override them.
        bootstrap=("import sys; from pathlib import Path; import lab.model,lab.service; "
                   "lab.model.ROOT=lab.service.ROOT=Path(sys.argv.pop(1)); "
                   "from lab.server import main; main()")
        cls.process=subprocess.Popen([sys.executable,'-B','-c',bootstrap,cls.tmp.name,'--port',str(cls.port),'--config',str(config)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        cls.base='http://127.0.0.1:'+str(cls.port)
        for _ in range(80):
            try:
                cls.opener.open(cls.base+'/api/status',timeout=.2).close()
                return
            except OSError:
                if cls.process.poll() is not None:
                    raise AssertionError(cls.process.stderr.read().decode())
                time.sleep(.05)
        cls.process.terminate()
        stdout,stderr=cls.process.communicate(timeout=5)
        raise AssertionError('server did not start: '+stdout.decode()+stderr.decode())

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        try:
            cls.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill();cls.process.communicate()
        cls.tmp.cleanup()

    def call(self,path,body=None,headers=None):
        h={'X-Patrol-Client-Id':'http-client'}
        h.update(headers or {})
        if body is not None:
            h['Content-Type']='application/json'
        request=urllib.request.Request(self.base+path,data=None if body is None else json.dumps(body).encode(),headers=h)
        with self.opener.open(request,timeout=5) as response:
            return json.load(response)

    def test_dashboard_static_and_state_event_stream(self):
        with self.opener.open(self.base) as response:
            self.assertIn('升降杆调试台',response.read().decode())
        with self.opener.open(self.base+'/api/events',timeout=2) as response:
            line=response.readline().decode()
            self.assertTrue(line.startswith('data: '))
            self.assertIn('status',json.loads(line[6:]))

    def test_local_operations_and_bridge_contract(self):
        self.call('/api/arm',{})
        self.call('/api/devices/lift_pole_1/lease/acquire',{'holder_name':'HTTP test'})
        self.call('/api/control',{'action':'home','confirm_homing':True})
        for _ in range(350):
            details=self.call('/api/status')['details']
            if details['position_trusted'] and not details['pending_stop']:
                break
            time.sleep(.03)
        self.assertTrue(details['position_trusted'])
        self.assertFalse(details['pending_stop'],'HOME cleanup must finish before positioning')
        self.call('/api/control',{'action':'min'})
        for _ in range(200):
            if self.call('/api/status')['details']['state']=='IDLE':
                break
            time.sleep(.03)
        self.assertAlmostEqual(self.call('/api/status')['details']['height_cm'],1,places=2)
        self.call('/api/control',{'action':'set_speed','mode':'pv','speed_cm_s':.35})
        result=self.call('/api/control',{'action':'up','intent_id':'http-jog'})
        self.assertTrue(result['ok'])
        self.call('/api/control',{'action':'heartbeat','intent_id':'http-jog'})
        self.call('/api/control',{'action':'stop','intent_id':'http-jog'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call('/api/control',{'action':'heartbeat','intent_id':'http-jog'})
        self.assertEqual(ctx.exception.code,409)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call('/api/bridge/v1/health')
        self.assertEqual(ctx.exception.code,401)
        health=self.call('/api/bridge/v1/health',headers={'X-Patrol-Bridge-Token':'http-test'})
        self.assertFalse(health['robot_control_via_bridge'])
        snapshot=self.call('/api/bridge/v1/snapshot',headers={'X-Patrol-Bridge-Token':'http-test'})
        self.assertIn('lift',snapshot)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call('/api/bridge/v1/lift/control',{'action':'reset_fault','confirm_reset':True},headers={'X-Patrol-Bridge-Token':'http-test'})
        self.assertIn(ctx.exception.code,(400,403))
        self.assertTrue(any(x['kind']=='tx' for x in self.call('/api/logs')))

    def test_external_origin_cannot_write(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call('/api/control',{'action':'up','intent_id':'bad'},headers={'Origin':'http://other.example'})
        self.assertEqual(ctx.exception.code,403)
