"""Strict Modbus RTU validation and a bounded manual maintenance surface."""
import re
import struct

from baseline.lift_modbus import validate_crc, ModbusRtuError
from .model import LabError

def parse_frame(text, slave):
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise LabError('请输入完整 RTU 十六进制报文（包含 CRC）', 400)
    compact = re.sub(r'\s+', '', text)
    if not re.fullmatch(r'[0-9a-fA-F]+', compact) or len(compact) % 2:
        raise LabError('报文只能含十六进制字节和空白，且必须为完整字节', 400)
    request = bytes.fromhex(compact)
    if not 8 <= len(request) <= 255:
        raise LabError('RTU 报文长度不合法', 400)
    try:
        validate_crc(request)
    except (ValueError, ModbusRtuError):
        raise LabError('CRC 错误：请检查报文，工具不会自动改写', 400)
    if request[0] != slave or request[0] == 0:
        raise LabError('只允许当前配置的从站，不允许广播', 400)
    fn, address, operand = request[1], int.from_bytes(request[2:4],'big'), int.from_bytes(request[4:6],'big')
    if fn == 3:
        count = operand
        valid = len(request) == 8 and 1 <= count <= 125
        values = None
    elif fn == 6:
        count, values = 1, [operand]
        valid = len(request) == 8
    elif fn == 16:
        count = operand
        valid = 1 <= count <= 123 and len(request) == 9+count*2 and request[6] == count*2
        values = list(struct.unpack('>'+'H'*count, request[7:-2])) if valid else None
    else:
        raise LabError('仅支持功能码 03、06、10', 400)
    if not valid or address+count > 65536:
        raise LabError('报文长度、寄存器数量或地址范围不合法', 400)
    return dict(request=request,function=fn,address=address,count=count,values=values,
                write=fn!=3,expected_length=5+count*2 if fn==3 else 8)


def validate_response(parsed, response):
    try:
        validate_crc(response)
    except (ValueError, IndexError, ModbusRtuError):
        raise LabError('响应 CRC 不正确，结果未确认', 503)
    request = parsed['request']
    if len(response) == 5 and response[0] == request[0] and response[1] == (request[1]|128):
        raise LabError('驱动器 Modbus 异常响应 0x%02X'%response[2], 503)
    if response[:2] != request[:2] or len(response) != parsed['expected_length']:
        raise LabError('响应从站、功能码或长度不匹配', 503)
    if parsed['write']:
        if response[:6] != request[:6]:
            raise LabError('写回显与请求不匹配，结果未确认', 503)
    elif response[2] != parsed['count']*2:
        raise LabError('响应字节数不匹配', 503)

