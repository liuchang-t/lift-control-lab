"""Load the frozen adapter unchanged in a private namespace, simulation only."""
from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from baseline import lift_modbus
from .model import LabError


class Legacy:
    def __init__(self, config, transport, trace):
        if config.mode != 'sim':
            raise ValueError('frozen baseline is simulation-only')
        self.config,self.transport,self.trace=config,transport,trace
        self.settings=SimpleNamespace(
            lift_dry_run=False,lift_host=config.host,lift_port=config.port,lift_slave_id=config.slave_id,
            lift_timeout_s=config.timeout,lift_poll_interval_ms=round(config.poll_s*1000),
            lift_travel_height=config.travel_cm,lift_base_height_cm=config.base_cm,
            lift_jog_deadman_s=config.deadman_s,lift_position_zero_pulse=0,lift_position_trusted=False,
            lift_last_raw_pulse=None,lift_max_pp_speed_pr_s=config.max_pp_speed,lift_max_pv_speed_pr_s=config.max_pv_speed,
            lift_min_pp_speed_pr_s=config.min_speed,lift_min_pv_speed_pr_s=config.min_speed,
            lift_max_single_move_pulse=config.max_move_pulse,lift_position_tolerance_pulse=20,
            save=lambda:None)
        state=dict(online=True,height=0.,speed=.3,motion='stopped',target_height=None,faults=[],limits={'min':False,'max':False})
        class HTTPException(LabError):
            def __init__(self,status_code,detail):
                super().__init__(str(detail),status_code)
                self.detail=detail
        ns=dict(vars(lift_modbus))
        ns.update(asyncio=asyncio,contextlib=contextlib,json=json,time=time,datetime=datetime,timezone=timezone,
                  settings=self.settings,lift_state=state,HTTPException=HTTPException,LIFT_ENABLE_HOLD_S=5.,
                  __name__='frozen_adapter',__builtins__=__builtins__)
        source=Path(__file__).resolve().parents[1]/'baseline/main.py.txt'
        tree=ast.parse(source.read_text(encoding='utf-8'))
        names={'LiftRealAdapter','lift_jog_deadman_should_stop'}
        selected=[node for node in tree.body if getattr(node,'name',None) in names]
        assert len(selected)==2
        future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations',asname=None)],level=0)
        module=ast.fix_missing_locations(ast.Module(body=[future]+selected,type_ignores=[]))
        exec(compile(module,str(source),'exec'),ns)
        self.adapter=ns['LiftRealAdapter'](self.settings)
        self.adapter._client.transport=transport
        self.state=state
        self.intent=None

    async def start(self):
        await self.adapter.start_status_monitor()

    async def arm(self,hardware_confirmed=False):
        return {'failures':[], 'notice':'冻结原逻辑：仅模拟，已知缺陷保持用于复现'}

    async def command(self,command):
        value=dict(command)
        if value.get('action')=='heartbeat':
            value['action']=self.intent or 'stop'
        if value.get('action') in ('up','down'):
            self.intent=value['action']
        elif value.get('action') in ('stop','estop'):
            self.intent=None
        obj=SimpleNamespace(action=value.get('action'),height=value.get('height'),speed=value.get('speed'))
        await self.adapter.control(obj)
        return self.snapshot()

    async def stop(self,emergency=False):
        return await self.command({'action':'estop' if emergency else 'stop'})

    def snapshot(self):
        data=dict(self.state)
        data.update(state='LEGACY',armed=True,pending_stop=False)
        return dict(online=bool(data.get('online')),control_mode='sim',implementation='baseline',details=data)

    async def close(self):
        await self.adapter.stop()
