from __future__ import annotations

import json
import math
import os
import tempfile
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from .units import Units, finite, integer

ROOT = Path(__file__).resolve().parents[1]
HOME_DISPLAY_FIELDS = {
    'home_speed_cm_s':'home_speed',
    'home_creep_speed_cm_s':'home_creep_speed',
    'home_acceleration_cm_s2':'home_acceleration',
}


class LabError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


@dataclass
class Config:
    mode: str = "real"
    implementation: str = "candidate"
    host: str = "10.21.31.15"
    port: int = 8899
    slave_id: int = 1
    timeout: float = 0.5
    travel_cm: float = 180.0
    base_cm: float = 0.0
    pulses_per_rev: int = 10000
    cm_per_rev: float = 0.35
    max_pp_speed: int = 100000
    max_pv_speed: int = 100000
    min_speed: int = 3000
    min_pp_speed: int = None
    min_pv_speed: int = None
    soft_lower_cm: float = 1.0
    soft_upper_cm: float = None
    home_reference_pulse: int = 0
    pp_acceleration_cm_s2: float = None
    pp_deceleration_cm_s2: float = None
    pp_emergency_deceleration_cm_s2: float = None
    pp_tolerance_cm: float = 0.05
    home_tolerance_cm: float = 0.05
    home_timeout_s: float = 600.0
    home_start_timeout_s: float = 3.0
    home_speed: int = None
    home_creep_speed: int = None
    home_acceleration: int = None
    home_enable_hold_s: float = 4.0
    home_disable_hold_s: float = 0.2
    verified_deceleration_cm_s2: float = 2.0
    control_latency_s: float = 1.0
    limit_margin_cm: float = 0.1
    motion_profile_verified: bool = False
    max_move_pulse: int = 2860000
    deadman_s: float = 1.0
    poll_s: float = 0.2
    startup_grace_s: float = 2.0
    stall_s: float = 0.8
    stop_timeout_s: float = 3.0
    lease_s: float = 180.0
    release_enable_s: float = 0.0
    bridge_enabled: bool = False
    bridge_token: str = ""
    low_word_setting: int = 1
    state_file: str = ".runtime/calibration.json"
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_prefix: str = "patrol/lab"
    mqtt_interval_s: float = 1.0
    mqtt_username: str = ""
    mqtt_password: str = ""

    def __post_init__(self):
        if not Path(self.state_file).is_absolute():
            self.state_file=str(ROOT/self.state_file)
        if self.min_pp_speed is None:
            self.min_pp_speed=self.min_speed
        if self.min_pv_speed is None:
            self.min_pv_speed=self.min_speed
        if self.soft_upper_cm is None:
            self.soft_upper_cm=self.travel_cm

    def validate(self):
        if type(self.cm_per_rev) not in (int,float) or not math.isfinite(self.cm_per_rev) or self.cm_per_rev<=0:
            raise ValueError('cm_per_rev 必须是有限正数，且需现场核实每转位移')
        for name in ('port','slave_id','pulses_per_rev','max_pp_speed','max_pv_speed','min_speed','min_pp_speed','min_pv_speed','home_reference_pulse','max_move_pulse','low_word_setting','mqtt_port'):
            if type(getattr(self,name)) is not int:
                raise ValueError(name+' must be an integer')
        if self.mode not in {"sim", "real"} or self.implementation not in {"candidate", "baseline"}:
            raise ValueError("invalid mode/implementation")
        if self.mode == "real" and self.implementation != "candidate":
            raise ValueError("baseline is simulation-only")
        for name in ("travel_cm", "pulses_per_rev", "cm_per_rev", "max_pp_speed", "max_pv_speed", "min_speed", "max_move_pulse", "deadman_s", "poll_s", "startup_grace_s", "stall_s", "stop_timeout_s", "lease_s", "mqtt_interval_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(name + " must be finite and positive")
        if not 0.05 <= self.timeout <= 2 or not 1 <= self.slave_id <= 247 or not 1 <= self.port <= 65535:
            raise ValueError("invalid transport parameters")
        for mode in ('pp','pv'):
            if not 0 < getattr(self,'min_'+mode+'_speed') <= getattr(self,'max_'+mode+'_speed') <= 2147483647:
                raise ValueError('每种模式必须满足 0 < 最低速度 <= 最高速度 <= int32')
        if not 0 <= finite(self.soft_lower_cm) < finite(self.soft_upper_cm) <= self.travel_cm:
            raise ValueError('必须满足 0 <= 软件下限 < 软件上限 <= 行程')
        for name in ('pp_tolerance_cm','home_tolerance_cm','home_timeout_s','home_start_timeout_s','verified_deceleration_cm_s2','control_latency_s','limit_margin_cm'):
            if finite(getattr(self,name)) <= 0:
                raise ValueError(name+' 必须为有限正数')
        for name in ('pp_acceleration_cm_s2','pp_deceleration_cm_s2','pp_emergency_deceleration_cm_s2'):
            value=getattr(self,name)
            if value is not None:
                self.acceleration_pr(value)
        for name in ('home_speed','home_creep_speed','home_acceleration'):
            value=getattr(self,name)
            if value is not None and (type(value) is not int or not 1 <= value <= 2147483647):
                raise ValueError(name+' 必须为空或1～2147483647的整数')
        for name in ('home_enable_hold_s','home_disable_hold_s'):
            if not 0 <= finite(getattr(self,name)) <= 10:
                raise ValueError(name+' 必须为0～10秒，实验性最短保持时间')
        if type(self.motion_profile_verified) is not bool:
            raise ValueError('motion_profile_verified 必须为布尔值')
        self.units.target(self.soft_upper_cm,self.home_reference_pulse)
        self.units.target(self.soft_lower_cm,self.home_reference_pulse)
        if not math.isfinite(self.base_cm) or self.base_cm < 0 or not math.isfinite(self.release_enable_s) or self.release_enable_s < 0:
            raise ValueError("invalid height/hold time")
        if self.mode == "real" and self.bridge_enabled and not self.bridge_token:
            raise ValueError("real Bridge requires a token")
        return self

    def home_display_input(self, value):
        if value is None:
            return None
        if finite(value)<=0:
            raise ValueError('回零速度和加减速度必须为有限正数')
        result=self.pulse(value)
        if not 1<=result<=2147483647:
            raise ValueError('回零参数换算后须为1～2147483647指令单位')
        return result

    def home_parameters(self, actual):
        # Saved intent is separate from the drive's observed values.
        result=dict(actual)
        for key in ('home_speed','home_creep_speed','home_acceleration'):
            if getattr(self,key) is not None:
                result[key]=getattr(self,key)
        return result

    def pulse(self, cm):
        return self.units.pulse(cm)

    def cm(self, pulse):
        return self.units.cm(pulse)

    @property
    def units(self):
        return Units(self.pulses_per_rev,self.cm_per_rev)

    def speed_pr(self, cm_s, mode):
        if mode not in ('pp','pv'):
            raise ValueError('请指定 pp 或 pv 模式')
        value=finite(cm_s)
        if value==0:
            return 0
        minimum,maximum=getattr(self,'min_'+mode+'_speed'),getattr(self,'max_'+mode+'_speed')
        if not self.cm(minimum) <= value <= self.cm(maximum):
            raise ValueError(mode+' 速度不在该模式上下限内')
        result=self.pulse(value)
        if not minimum <= result <= maximum:
            raise ValueError('取整后速度越界')
        return result

    def acceleration_pr(self, cm_s2):
        value=finite(cm_s2)
        if value<=0:
            raise ValueError('PP加减速度必须为有限正数')
        result=self.pulse(value)
        if not 1<=result<=2147483647:
            raise ValueError('PP加减速度换算后须为1～2147483647指令单位/s²')
        return result

    def stopping_distance(self, cm_s):
        value=abs(finite(cm_s))
        return value*self.control_latency_s+value*value/(2*self.verified_deceleration_cm_s2)+self.limit_margin_cm

    def speed(self, ratio, mode="pv"):
        if ratio == 0:
            return 0
        maximum = self.max_pp_speed if mode == "pp" else self.max_pv_speed
        return max(getattr(self,'min_'+mode+'_speed'), min(maximum, integer(ratio * maximum)))

    def public(self, include_display=False):
        result={k: v for k, v in asdict(self).items() if k not in {"bridge_token","mqtt_password"}}
        if include_display:
            result['home_display']={
                display:None if getattr(self,stored) is None else self.cm(getattr(self,stored))
                for display,stored in HOME_DISPLAY_FIELDS.items()}
        return result


class CalibrationStore:
    def __init__(self, filename):
        self.path = Path(filename)

    def load(self):
        if not self.path.exists():
            return 0
        return int(json.loads(self.path.read_text(encoding="utf-8"))["zero_pulse"])

    def save(self, zero):
        atomic_json_write(self.path,{"zero_pulse": int(zero), "trusted_after_restart": False})


class SpeedStore:
    """Device/transport-scoped user settings, never restored as motion intent."""
    def __init__(self, config):
        identity='%s|%s|%s|%s|%s'%(config.mode,config.implementation,config.host,config.port,config.slave_id)
        self.device=identity
        self.path=Path(config.state_file).parent/'profiles'/(hashlib.sha256(identity.encode()).hexdigest()[:20]+'.json')

    def load(self, config):
        if not self.path.exists():
            return {mode:config.cm(config.speed(.3,mode)) for mode in ('pp','pv')}
        value=json.loads(self.path.read_text(encoding='utf-8'))
        if value.get('version')!=1 or value.get('device')!=self.device:
            raise ValueError('速度配置版本或设备身份不匹配')
        speeds=value['speeds_cm_s']
        if set(speeds)!= {'pp','pv'}:
            raise ValueError('速度配置缺少 PP/PV')
        for mode in speeds:
            config.speed_pr(speeds[mode],mode)
        return {mode:finite(speeds[mode]) for mode in speeds}

    def save(self, speeds):
        atomic_json_write(self.path,dict(version=1,device=self.device,speeds_cm_s=speeds))


def load_config(filename=None):
    """Startup files seed defaults; persisted UI settings always take precedence."""
    paths=[]
    if filename:
        path=Path(filename)
        if not path.exists():
            raise FileNotFoundError('指定启动配置不存在：'+str(path))
        paths.append(path)
    paths.extend((ROOT/'.runtime/config.json',ROOT/'.runtime/connection.json'))
    values={}
    for path in paths:
        if not path.exists():
            continue
        saved=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(saved,dict):
            raise ValueError('配置必须是JSON对象：'+str(path))
        values.update(saved)
    return Config(**values).validate()


def atomic_json_write(filename, value):
    path=Path(filename)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=str(path.parent),prefix='.save-')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name,str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)
