from __future__ import annotations

import argparse
import asyncio
import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .model import Config, LabError, load_config
from .service import Service
from .mqtt import publish_loop


def make_handler(service,loop,web_root):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def send(self,status,payload,mime='application/json; charset=utf-8'):
            data=payload if isinstance(payload,bytes) else json.dumps(payload,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',mime)
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.handle_request('GET')

        def do_POST(self):
            self.handle_request('POST')

        def handle_request(self,method):
            path=urlsplit(self.path).path
            try:
                if method=='GET' and path in ('/','/app.js','/style.css','/log-buffer.js'):
                    name={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/log-buffer.js':'log-buffer.js'}[path]
                    mime={'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8','/log-buffer.js':'text/javascript; charset=utf-8'}[path]
                    return self.send(200,(web_root/name).read_bytes(),mime)
                if method=='GET' and path=='/api/events':
                    return self.events()
                if method=='POST':
                    origin=self.headers.get('Origin')
                    if origin and origin!='http://'+self.headers.get('Host',''):
                        raise LabError('Cross-origin write refused',403)
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<=size<=16384:
                        raise LabError('请求过大',413)
                    body=json.loads(self.rfile.read(size).decode('utf-8') or '{}')
                    if not isinstance(body,dict):
                        raise LabError('请求必须是对象',400)
                else:
                    body={}
                future=asyncio.run_coroutine_threadsafe(service.request(method,path,body,dict(self.headers)),loop)
                result=future.result(timeout=30)
                self.send(200,result)
            except LabError as exc:
                self.send(exc.status,{'detail':str(exc)})
            except (ValueError,TypeError,KeyError) as exc:
                self.send(400,{'detail':str(exc)})
            except (BrokenPipeError,ConnectionResetError):
                pass
            except Exception as exc:
                self.send(503,{'detail':str(exc) or type(exc).__name__})

        def events(self):
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            async def snapshot():
                return dict(status=service.controller.snapshot(),lease=service.lease_view(),mqtt=service.mqtt_status,
                            connection=service.connection_status())
            try:
                while loop.is_running():
                    value=asyncio.run_coroutine_threadsafe(snapshot(),loop).result(timeout=3)
                    self.wfile.write(('data: '+json.dumps(value,ensure_ascii=False)+'\n\n').encode('utf-8'))
                    self.wfile.flush()
                    time.sleep(.3)
            except (BrokenPipeError,ConnectionResetError,TimeoutError):
                pass
    return Handler


async def create_service(config):
    """Restore settings without connecting to a remembered real device."""
    service=Service(config)
    if config.mode == 'sim':
        await service.start()
    return service


def main():
    parser=argparse.ArgumentParser(description='Independent lift laboratory (default: real device, disconnected)')
    parser.add_argument('--config')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--implementation',choices=['candidate','baseline'])
    parser.add_argument('--allow-real',action='store_true',help='兼容旧命令；实机始终需页面确认连接')
    args=parser.parse_args()
    cfg=load_config(args.config)
    if args.implementation:
        cfg.implementation=args.implementation
    cfg.validate()
    # Bind before touching hardware, so a busy HTTP port cannot leave an invisible controller.
    server=ThreadingHTTPServer(('127.0.0.1',args.port),BaseHTTPRequestHandler)
    server.daemon_threads=True
    loop=asyncio.new_event_loop()
    thread=threading.Thread(target=loop.run_forever,daemon=True)
    thread.start()
    async def setup():
        service=await create_service(cfg)
        mqtt=asyncio.create_task(publish_loop(service))
        return service,mqtt
    service,mqtt=asyncio.run_coroutine_threadsafe(setup(),loop).result(timeout=5)
    server.RequestHandlerClass=make_handler(service,loop,Path(__file__).resolve().parents[1]/'web')
    def halt(*unused):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,halt)
    print('Lift laboratory: http://127.0.0.1:%s | %s | %s' % (server.server_port,cfg.mode,cfg.implementation),flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        async def finish():
            mqtt.cancel()
            await asyncio.gather(mqtt,return_exceptions=True)
            await service.close()
        try:
            asyncio.run_coroutine_threadsafe(finish(),loop).result(timeout=cfg.stop_timeout_s+5)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)


if __name__=='__main__':
    main()
