from __future__ import annotations
import asyncio
import unittest
from lab.model import Config
from lab.driver import Driver
from lab.transport import Simulator,TcpTransport,Trace
from lab.mqtt import packet,field
from baseline.lift_modbus import build_write_multiple_registers,build_write_single_register,int32_to_registers_le_word,append_crc,validate_crc


class ProtocolTests(unittest.TestCase):
    def test_pdf_vectors_and_corrected_typos(self):
        self.assertEqual(build_write_multiple_registers(1,0x100E,int32_to_registers_le_word(50000)).hex(),'0110100e000204c350000083b6')
        self.assertEqual(build_write_multiple_registers(1,0x100E,int32_to_registers_le_word(-20000)).hex(),'0110100e000204b1e0ffff9899')
        self.assertEqual(build_write_single_register(1,0x0200,9).hex(),'0106020000094874')
        self.assertEqual(build_write_single_register(1,0x0D08,64).hex(),'01060d0800400b54')

    def test_mqtt_packet(self):
        self.assertEqual(packet(0x30,field('a')+b'{}'),b'\x30\x05\x00\x01a{}')

    def test_invalid_settings(self):
        for kwargs in [dict(deadman_s=0),dict(poll_s=0),dict(timeout=10),dict(mode='real',implementation='baseline')]:
            with self.assertRaises(ValueError):
                Config(**kwargs).validate()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_rtu_over_tcp_and_write_echo(self):
        cfg=Config()
        sim=Simulator(cfg,Trace())
        async def handle(reader,writer):
            try:
                while True:
                    header=await reader.readexactly(6)
                    if header[1]==16:
                        count=await reader.readexactly(1)
                        tail=count+await reader.readexactly(count[0]+2)
                    else:
                        tail=await reader.readexactly(2)
                    response=await sim.transaction(header+tail,8)
                    for byte in response:
                        writer.write(bytes([byte]))
                        await writer.drain()
                        await asyncio.sleep(.001)
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()
                await writer.wait_closed()
        server=await asyncio.start_server(handle,'127.0.0.1',0)
        cfg.host='127.0.0.1';cfg.port=server.sockets[0].getsockname()[1]
        transport=TcpTransport(cfg,Trace())
        driver=Driver(cfg,transport)
        try:
            self.assertEqual((await driver.preflight())['failures'],[])
            await driver.wire.write_int32(0x100E,-20000)
            self.assertEqual(sim._i32(0x100E),-20000)
        finally:
            await transport.close()
            server.close();await server.wait_closed()

    async def test_timeout_closes_connection(self):
        async def handle(reader,writer):
            await reader.read()
            writer.close()
            await writer.wait_closed()
        server=await asyncio.start_server(handle,'127.0.0.1',0)
        cfg=Config(host='127.0.0.1',port=server.sockets[0].getsockname()[1],timeout=.05)
        tr=TcpTransport(cfg,Trace())
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await tr.transaction(bytes.fromhex('01030b000001862e'),7)
            self.assertIsNone(tr.writer)
        finally:
            await tr.close();server.close();await server.wait_closed()
