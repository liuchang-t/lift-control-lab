"""Observed register values, shared by automatic and manual bus transactions."""
import struct
import time
from .units import signed_word

from baseline.lift_modbus import ModbusRtuError, validate_crc, registers_to_int32_le_word


# Access describes the manual-defined register, not tool write policy.
# Parameter suffixes are decimal; Modbus addresses below are hexadecimal.
CATALOG = [
    (0x001F, 2, 'P00-31/32', '编码器线数 L/H', 'R', '单位核验/只读'),
    (0x0200, 1, 'P02-00', '控制方式', 'RW', '预检/模式准备'),
    (0x0202, 1, 'P02-02', '电机方向', 'RW', '方向参考监视'),
    (0x1016, 1, 'P10-22', '总线运动极性', 'RW', '方向参考监视'),
    (0x0306, 1, 'P03-06', 'DI3 功能', 'RW', '使能预检'),
    (0x0307, 1, 'P03-07', 'DI3 极性', 'RW', '使能预检'),
    (0x0308, 1, 'P03-08', 'DI4 功能', 'RW', '上限位'),
    (0x0309, 1, 'P03-09', 'DI4 极性', 'RW', '上限位'),
    (0x030A, 1, 'P03-10', 'DI5 功能', 'RW', '下限位'),
    (0x030B, 1, 'P03-11', 'DI5 极性', 'RW', '下限位'),
    (0x0502, 2, 'P05-02', '每转指令数（双字推定）', 'RW', '单位核验/位宽待实机核验'),
    (0x0507, 2, 'P05-07', '电子齿轮分子', 'RW', '单位核验'),
    (0x0509, 2, 'P05-09', '电子齿轮分母', 'RW', '单位核验'),
    (0x0A28, 1, 'P0A-40', '驱动器软限位设置', 'RW', '只读/行为待实机验证'),
    (0x0A29, 2, 'P0A-41', '驱动器正向软限位', 'RW', '只读/行为待实机验证'),
    (0x0A2B, 2, 'P0A-43', '驱动器负向软限位', 'RW', '只读/行为待实机验证'),
    (0x0B00, 1, 'P0B-00', '反馈转速', 'R', '实时监视/停止确认'),
    (0x0B02, 1, 'P0B-02', '负载/转矩原码', 'R', '实时监视'),
    (0x0B03, 1, 'P0B-03', 'DI 电平', 'R', '限位保护'),
    (0x0B04, 1, 'P0B-04', '状态字', 'R', '回零/到位'),
    (0x0B07, 2, 'P0B-07', '位置反馈', 'R', '位置保护/回零确认'),
    (0x0B22, 1, 'P0B-34', '当前报警', 'R', '报警保护/复位检查'),
    (0x0C0D, 1, 'P0C-13', '通信 EEPROM 更新', 'RW', '只读/写参前检查'),
    (0x0C1A, 1, 'P0C-26', '32 位字序', 'RW', '协议预检'),
    (0x0D01, 1, 'P0D-01', '故障复位', 'RW', '人工故障复位'),
    (0x0D08, 1, 'P0D-08', '控制字', 'RW', '启动/停止/急停/回零'),
    (0x0D0B, 1, 'P0D-11', 'JOG试运行指令', 'RW', '实测1保持/17升/33降/65失能；ACK非使能完成'),
    (0x4604, 1, '0x4604', 'JOG速度 rpm', 'RW', '抓包实测；设备范围/掉电保持未确认'),
    (0x0D11, 1, 'P0D-17', 'DI 强制选择给定', 'RW', '给定读回/不是使能完成反馈'),
    (0x0D12, 1, 'P0D-18', 'DI 强制电平给定', 'RW', '给定读回/不是使能完成反馈'),
    (0x1003, 1, 'P10-03', '总线运动模式', 'RW', 'PP/PV/HOME'),
    (0x100E, 2, 'P10-14', '目标位置', 'RW', 'PP/目标对齐'),
    (0x1010, 2, 'P10-16', '回零偏移', 'RW', 'HOME 参考检查'),
    (0x1012, 2, 'P10-18', 'P10 负向软限位', 'RW', '只读/行为待实机验证'),
    (0x1014, 2, 'P10-20', 'P10 正向软限位', 'RW', '只读/行为待实机验证'),
    (0x1017, 2, 'P10-23', '最大速度', 'RW', '速度上限检查'),
    (0x1019, 2, 'P10-25', 'PP 速度', 'RW', 'PP/独立设速'),
    (0x101B, 2, 'P10-27', '加速度', 'RW', '运行参数检查'),
    (0x101D, 2, 'P10-29', '减速度', 'RW', '停止距离检查'),
    (0x101F, 2, 'P10-31', '急停减速度', 'RW', '急停参数检查'),
    (0x1023, 1, 'P10-35', '回零模式', 'RW', '模式 17 回零'),
    (0x1024, 2, 'P10-36', '回零搜索速度', 'RW', '回零检查'),
    (0x1026, 2, 'P10-38', '回零爬行速度', 'RW', '回零/参考内返回'),
    (0x1028, 2, 'P10-40', '回零加速度', 'RW', '回零检查'),
    (0x102A, 2, 'P10-42', 'PV 速度', 'RW', 'PV/独立设速'),
]

# Trigger registers may be readable without being useful persistent state.
# Preserve the existing refresh policy independently of hardware access labels.
REFRESH_EXCLUDED = {0x0D01, 0x0D08}
HIDDEN_ADDRESSES = set(range(0x0306, 0x030C))

# Each background turn reads one bounded block after fresh protection feedback.
# Addresses are contiguous only where the manual defines all covered words.
STATIC_GROUPS = [(0x0C1A,1),(0x001F,2),(0x0502,2),(0x0507,4),
                 (0x0200,1),(0x0202,1),(0x0306,6),(0x1010,2),(0x1016,1)]
MONITOR_GROUPS = [(0x1003,1),(0x100E,2),(0x1017,10),(0x1023,9),(0x4604,1)]
PREFLIGHT_GROUPS = STATIC_GROUPS + MONITOR_GROUPS
MONITOR_INTERVAL = .5
MONITOR_MAX_AGE = 12.0
MONITOR_ADDRESSES = {address+i for address,width in MONITOR_GROUPS for i in range(width)}
REFERENCE_ADDRESSES = {0x0202,0x1016,0x0C1A,0x001F,0x0020,0x0502,0x0503,0x0507,0x0508,0x0509,0x050A,
                       0x1010,0x1011,0x1023}


def polling_policy(address):
    if address in (0x0B00,0x0B02,0x0B03,0x0B04,0x0B07,0x0B22,0x0C0D,0x0D0B,0x0D11,0x0D12):
        return dict(polling_kind='fast',polling_label='保护轮询',interval_s=None,
                    polling_notice='每个保护采样周期读取；周期由poll_s与总线响应决定，失鲜会停机。')
    if address in (0x1003,0x100E,0x1019,0x102A,0x4604):
        return dict(polling_kind='active',polling_label='运动快核对 / 空闲轮转',interval_s=None,
                    polling_notice='运动时每个保护周期核对模式、活动PP目标及当前速度；其它时候低频轮转。')
    if address in (0x1023,0x1024,0x1026,0x1028):
        return dict(polling_kind='active',polling_label='HOME快核对 / 空闲轮转',interval_s=None,
                    polling_notice='HOME期间每个保护周期核对回零模式、速度和加速度；其它时候低频轮转。')
    if address in MONITOR_ADDRESSES:
        return dict(polling_kind='slow',polling_label='低频轮转',interval_s=MONITOR_INTERVAL*len(MONITOR_GROUPS),
                    polling_notice='每0.5秒最多一块，名义全轮%s秒；控制序列和总线忙时让行，PP/HOME运动监视超过12秒未更新则停止，JOG不依赖整轮参数刷新。'%(MONITOR_INTERVAL*len(MONITOR_GROUPS)))
    if 0x0D00<=address<0x0E00:
        return dict(polling_kind='command',polling_label='命令写应答',interval_s=None,
                    polling_notice='触发寄存器可能自清零，只记录发送及ACK；不是持续驱动状态，副作用由反馈监测。')
    return dict(polling_kind='on_demand',polling_label='动作检查 / 手动刷新',interval_s=None,
                polling_notice='连接预检或显式刷新时读取，不声称实时轮询；驱动软限位仍只读且行为待实机验证。')


class RegisterMonitor:
    def __init__(self):
        self.words = {}
        self.read_words = {}
        self.write_words = {}
        self.serial = 0

    def observe(self, request, response, epoch):
        """Cache only matching successful exchanges; ACK is not a readback."""
        try:
            validate_crc(request)
            validate_crc(response)
            if len(request) < 8 or response[:2] != request[:2]:
                return
            fn, address, count = request[1], int.from_bytes(request[2:4], 'big'), int.from_bytes(request[4:6], 'big')
            if fn == 3:
                if len(response) != 5 + count*2 or response[2] != count*2:
                    return
                values = struct.unpack('>'+'H'*count, response[3:-2])
                source = 'read'
            elif fn in (6, 16):
                if len(response) != 8 or response[:6] != request[:6]:
                    return
                values = [count] if fn == 6 else struct.unpack('>'+'H'*count, request[7:-2])
                source = 'write_ack'
            else:
                return
            self.serial += 1
            now = time.monotonic()
            for offset, value in enumerate(values):
                item = dict(value=value, at=now, epoch=epoch, source=source, serial=self.serial)
                self.words[address+offset] = item
                (self.read_words if source == 'read' else self.write_words)[address+offset] = item
        except (ValueError, IndexError, struct.error, ModbusRtuError):
            return

    def snapshot(self, epoch, stale_after=2.0, active_mode=None):
        rows = []
        now = time.monotonic()
        for address, width, parameter, name, access, operations in CATALOG:
            cache = self.read_words if address == 0x0D0B else self.words
            words = [cache.get(address+i) for i in range(width)]
            complete = all(words)
            coherent = complete and len({(w['epoch'],w['serial']) for w in words}) == 1
            age = max(now-w['at'] for w in words) if complete else None
            policy = polling_policy(address)
            active_addresses = {0x1003}
            if active_mode == 'pp':
                active_addresses.update((0x100E,0x1019))
            elif active_mode == 'pv':
                active_addresses={0x4604}
            elif active_mode == 'home':
                active_addresses.update((0x1023,0x1024,0x1026,0x1028))
            if policy['polling_kind'] == 'fast' or (active_mode and address in active_addresses):
                threshold = stale_after
            elif policy['polling_kind'] in ('slow','active'):
                threshold = MONITOR_MAX_AGE
            else:
                threshold = None  # On-demand readings and command ACKs have no refresh schedule.
            stale = (not coherent or any(w['epoch'] != epoch for w in words)
                     or (threshold is not None and age > threshold))
            raw = [w['value'] for w in words] if complete else None
            value = None
            if coherent:
                value = registers_to_int32_le_word(raw) if width == 2 else raw[0]
                if address in (0x0B00, 0x0B02):
                    value = signed_word(value)
            rows.append(dict(parameter=parameter,address=address,address_hex='0x%04X'%address,name=name,
                             width=width,access=access,operations=operations,value=value,raw_words=raw,
                             visible=address not in HIDDEN_ADDRESSES,
                             last_write_value=self.write_words.get(address,{}).get('value') if self.write_words.get(address,{}).get('epoch')==epoch else None,
                             source=words[0]['source'] if coherent else 'unknown',age_s=age,stale=stale,
                             epoch=words[0]['epoch'] if coherent else None,stale_after_s=threshold,**policy))
        return dict(registers=rows,connection_epoch=epoch,
                    notice='读写为本工具正常流程用途；写回包不是读回。过期值仅供参考，连接变化后须重新读取。')
