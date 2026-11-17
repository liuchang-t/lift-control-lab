"""Optional MQTT 3.1.1 QoS 0 status publisher, independent of the control loop."""
from __future__ import annotations
import asyncio
import json
import struct
import uuid


def field(value):
    raw=value.encode('utf-8')
    return struct.pack('>H',len(raw))+raw


def packet(kind,body):
    size=len(body)
    encoded=bytearray([kind])
    while True:
        digit=size%128
        size//=128
        encoded.append(digit|(128 if size else 0))
        if not size:
            return bytes(encoded)+body


async def publish_loop(service):
    cfg=service.config
    if not cfg.mqtt_host:
        return
    while True:
        writer=None
        try:
            reader,writer=await asyncio.wait_for(asyncio.open_connection(cfg.mqtt_host,cfg.mqtt_port),2)
            flags=2 | (0xC0 if cfg.mqtt_username else 0)
            payload=field('lift-lab-'+uuid.uuid4().hex[:12])
            if cfg.mqtt_username:
                payload+=field(cfg.mqtt_username)+field(cfg.mqtt_password)
            writer.write(packet(0x10,field('MQTT')+bytes([4,flags])+struct.pack('>H',30)+payload))
            await writer.drain()
            response=await asyncio.wait_for(reader.readexactly(4),2)
            if response!=b'\x20\x02\x00\x00':
                raise ValueError('MQTT CONNACK rejected')
            service.mqtt_status='connected (QoS 0)'
            while True:
                status=service.controller.snapshot()
                snapshot=dict(lift=status,leases={'lift_pole_1':service.lease_view()},scope='lift-only laboratory')
                for suffix,value in [('lift/status',status),('snapshot',snapshot)]:
                    body=field(cfg.mqtt_prefix.rstrip('/')+'/'+suffix)+json.dumps(value,ensure_ascii=False).encode('utf-8')
                    writer.write(packet(0x30,body))
                await asyncio.wait_for(writer.drain(),2)
                if reader.at_eof():
                    raise ConnectionError('MQTT disconnected')
                await asyncio.sleep(cfg.mqtt_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            service.mqtt_status='reconnecting: '+str(exc)
            service.trace.add('mqtt_error',error=str(exc))
            await asyncio.sleep(2)
        finally:
            if writer:
                writer.close()
