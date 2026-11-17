"""Deterministic baseline/candidate contrast with no device connections."""
from __future__ import annotations
import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from .model import Config,LabError,atomic_json_write
from .service import Service


async def compare():
    results={}
    with tempfile.TemporaryDirectory() as temp:
        for implementation in ('baseline','candidate'):
            cfg=Config(implementation=implementation,state_file=str(Path(temp)/(implementation+'.json')),poll_s=.02,stop_timeout_s=.4,soft_lower_cm=0)
            svc=Service(cfg)
            ctl=svc.controller
            try:
                if implementation=='candidate':
                    await svc.start()
                    await ctl.observe();await ctl.arm()
                    await ctl.command(dict(action='home',confirm_homing=True))
                    for _ in range(200):
                        if ctl.trusted and not ctl.pending_stop:
                            break
                        await asyncio.sleep(.02)
                    if not ctl.trusted:
                        raise AssertionError('candidate HOME did not complete')
                else:
                    await ctl.adapter.refresh_status()
                await ctl.command(dict(action='set_speed',speed=0) if implementation=='baseline' else dict(action='set_speed',mode='pv',speed_cm_s=0))
                zero_speed=ctl.adapter._pv_speed_pr_s() if implementation=='baseline' else cfg.speed_pr(ctl.speeds['pv'],'pv')
                await ctl.command(dict(action='set_speed',speed=.3) if implementation=='baseline' else dict(action='set_speed',mode='pv',speed_cm_s=cfg.cm(cfg.speed(.3,'pv'))))
                svc.transport.inject('lost_start_ack')
                try:
                    await ctl.command(dict(action='up',intent_id='lost-ack'))
                except LabError:
                    pass
                if implementation=='candidate':
                    await ctl._confirm_stop()
                else:
                    await ctl.adapter.maybe_deadman_stop()
                results[implementation]=dict(zero_ratio_command_pr_s=zero_speed,
                                              motion_after_lost_start_ack=svc.transport.motion,
                                              automatic_stop_tracking=(ctl.pending_stop if implementation=='candidate' else ctl.adapter._pv_active),
                                              requires_rearm=(not ctl.armed if implementation=='candidate' else False))
            finally:
                await svc.close()
    return results


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output')
    args=parser.parse_args()
    results=asyncio.run(compare())
    print(json.dumps(results,ensure_ascii=False,indent=2))
    if args.output:
        atomic_json_write(args.output,results)


if __name__=='__main__':
    main()
