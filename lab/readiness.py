"""Read-only operator guidance; execution guards remain authoritative."""
from .alarms import blocked, jog_blocked, home_blocked


def build_readiness(service, client):
    connection_state = getattr(service, 'connection_state', 'connected')
    if connection_state in ('disconnected', 'connecting', 'disconnecting'):
        busy = connection_state != 'disconnected'
        summary = {'disconnected':'设备未连接', 'connecting':'正在连接', 'disconnecting':'正在停止并断开'}[connection_state]
        steps = ['等待连接操作完成；此时不会接受新运动。'] if busy else ['选择模拟或实机，检查IP、端口和从站地址，然后点击“连接”。']
        error = getattr(service, 'connection_error', '')
        if error:
            steps.append(error)
        return dict(summary=summary, checks=[dict(id='connection', label='连接已建立', ok=False, detail='选择或修改参数不会自动连接；参数在下一次连接时生效。')],
                    actions=[] if busy else [dict(id='connect',label='连接',enabled=True,reason='')],
                    steps=steps, can_move=False, manual=False,
                    last_rejection=getattr(service,'last_rejection',None))
    ctrl = service.controller
    snapshot = ctrl.snapshot()
    data = snapshot['details']
    manual = bool(getattr(ctrl, 'manual_active', False))
    online = bool(snapshot.get('online'))
    pending = bool(data.get('pending_stop'))
    moving = bool(getattr(ctrl, 'mode', None))
    armed = bool(data.get('armed'))
    trusted = bool(data.get('position_trusted'))
    alarm = data.get('alarm', {})
    code = alarm.get('code', 0)
    upper, lower = data.get('upper_limit'), data.get('lower_limit')
    eeprom_ok = data.get('eeprom_check', {}).get('value') == 0
    storage_ok = not getattr(ctrl, 'storage_error', '')
    unit_check = data.get('unit_verification', {})
    unit_status = unit_check.get('status', 'unknown')
    unit_ok = unit_status in ('consistent', 'simulated_consistent')
    unit_mismatch = unit_status == 'mismatch'
    valid_di = (data.get('di4_function') == 14 and data.get('di5_function') == 15
                and data.get('di4_logic') in (0, 1) and data.get('di5_logic') in (0, 1)
                and upper is not None and lower is not None and not (upper and lower))
    directional = bool(valid_di and code in (0x950,0x952) and (upper != lower))
    alarm_ok = jog_alarm_ok = True  # Alarm interpretation does not gate operator commands.
    height = data.get('height_cm')
    lo, hi = service.config.soft_lower_cm, service.config.soft_upper_cm
    in_range = height is not None and lo - .001 <= height <= hi + .001
    speeds = data.get('speeds', {})
    pp_ok = speeds.get('pp', {}).get('requested_cm_s', 0) > 0
    pv_ok = speeds.get('pv', {}).get('requested_cm_s', 0) > 0
    params = service.config.home_parameters(data.get('parameters', {}))
    home_issue = ''
    if params and not params.get('stale', True):
        if params.get('home_offset') != service.config.home_reference_pulse:
            home_issue = '驱动原点偏移与配置参考不一致，请核实 P10-16；不自动改写。'
        elif any(params.get(k, 0) <= 0 for k in ('home_speed', 'home_creep_speed', 'home_acceleration')):
            home_issue = 'HOME速度或加速度无效，请核实 P10-36/38/40。'
        elif max(params.get('home_speed', 0), params.get('home_creep_speed', 0)) > params.get('max_velocity', 0):
            home_issue = 'HOME速度超过驱动最大速度，请核实 P10-23/36/38。'
    checks = []

    def check(key, label, ok, detail):
        checks.append(dict(id=key, label=label, ok=bool(ok), detail=detail))

    check('online', '反馈在线', online, '反馈必须新鲜；失联先检查通信，再等待停止确认。')
    check('stopped', '停止指令已应答' if data.get('stop_confirmation')=='ack' else '停止已确认', not pending, '停止未确认时不要重新启动；现场需使用独立硬急停。')
    check('manual', '正常流程控制', not manual, '这些条件用于普通运动，不限制手动发送；结束手动并重新检查后可恢复基础点动。')
    check('eeprom', 'P0C-13 = 0', eeprom_ok, '请人工核实 EEPROM 设置；故障复位不能代替此项。')
    check('limits', '限位信号有效', valid_di, '检查 DI4/DI5 功能、极性和实际开关；不能靠复位绕过限位。')
    check('alarm', '报警仅提示', True, alarm.get('advice', '等待报警状态。'))
    check('storage', '配置有效', storage_ok, getattr(ctrl, 'storage_error', '') or '速度与配置已加载。')
    check('units', '驱动/配置换算一致', unit_ok,
          '连接后和运动前读取每转指令数、编码器及电子齿轮；数值一致不等于已核实机械导程或实际生效。')
    check('armed', '设备检查通过', armed, '点击“检查 / 恢复”；通过预检后才允许启动。')
    check('reference', '位置参考可信', trusted, '仅PP定位需要可信参考；基础点动无需回零。')
    check('pp_speed', 'PP速度非零', pp_ok, 'PP速度为0时不启动定位；修改PP速度并保存。')
    check('pv_speed', '点动速度非零', pv_ok, '点动速度以cm/s保存，按机械比例换算整数rpm写0x4604。')
    check('range', '定位目标范围', trusted,
          '目标须在 %.2f–%.2f cm 内；可信参考下可从范围外定位返回。' % (lo, hi))

    actions, steps = [], []

    def action(key, label, enabled=True, reason=''):
        actions.append(dict(id=key, label=label, enabled=bool(enabled), reason=reason))

    idle = online and not pending and not moving
    base = idle and not manual and eeprom_ok and valid_di and storage_ok and unit_ok
    if manual:
        summary = '手动总线直接收发，普通运动条件不拦截手动指令'
        steps.append('不自动插入停止或复位；实际效果以驱动器反馈为准。')
        if pending:
            steps.append('用户请求的停止尚未确认；新手动写可接管并取消旧停止重试。')
        if not online:
            steps.append('反馈离线或已过期，请核对通信；手动发送仍可尝试，失败会报告结果未知。')
        reason = (getattr(service, 'manual_active', None) or {}).get('reason', '')
        if reason:
            steps.append(reason)
        steps.append('恢复基础点动：结束手动 → 检查 / 恢复；定位另需回零。')
        action('stop', '停止')
        action('exit_manual', '结束手动')
    elif pending:
        home_stopping = getattr(ctrl, 'mode', None) == 'home' or getattr(ctrl.driver, 'home_enable_owned', False)
        summary = '回零停止处理中：等待静止反馈与撤销使能给定' if home_stopping else '停止命令尚未确认应答，请稍后重新按住'
        steps.append('HOME停止不仅确认命令应答，还需核验实际转速/位置静止，再清除控制字并撤销P03-07的S-ON给定；完成前不能重新启动。通信中断时使用现场停机措施。' if home_stopping else '等待停止命令应答；应答不代表实际静止，实际运动以反馈为准。通信中断时使用现场停机措施。')
        action('stop', '再次请求停止')
    elif not online:
        summary = '反馈离线或已过期'
        steps.append('检查驱动器、串口服务器及通信；恢复后重新检查；基础点动无需回零。')
    elif moving:
        home_phases = {'preparing':'回零：准备参数与使能给定', 'seeking_lower':'回零：寻找下限位', 'leaving_lower':'回零：脱离下限位', 'settling':'回零：核对完成位、位置和静止'}
        summary = data.get('motion_notice') or (home_phases.get(data.get('home_phase'), '回零进行中') if getattr(ctrl, 'mode', None) == 'home' else '运动进行中')
        steps.append(data.get('motion_advice') or ('此提示不锁定点动；松开即发送停止。' if data.get('motion_notice') else '等待完成；需要中断时点击“停止”或“软件急停”。'))
        action('stop', '停止')
    elif data.get('untracked_motion') and data.get('stop_confirmation')!='ack':
        summary = '设备正在运动（非本后端任务）'
        steps.append('当前仅观察反馈，不自动停止或撤销位置参考；需要控制时由你明确发起操作。')
    else:
        if not eeprom_ok:
            steps.append('人工核实 P0C-13；值为0后再重新检查，不要反复点击故障复位。')
        if not valid_di:
            steps.append('检查上下限位接线、功能与极性；当前信号不具备运动条件。')
        if not storage_ok:
            steps.append('修复配置/速度保存错误后重启工具，不以默认值掩盖错误。')
        if unit_mismatch:
            steps.append('驱动器读回的每转指令关系与配置不一致；查看换算依据，核实 P05-02、电子齿轮和字序，不能仅复位报警继续。')
        elif not unit_ok:
            steps.append('换算读回未齐或已失效；点击“检查 / 恢复”重新核对，不能用默认比例代替。')
        if not pp_ok:
            steps.append('PP速度为0，定位前设置非零PP速度并保存。')
        if not pv_ok:
            steps.append('JOG速度为0，点动前设置非零JOG速度并保存。')
        if code:
            steps.append('%s %s：%s' % (alarm.get('text', ''), alarm.get('name', ''), alarm.get('advice', '')))
            action('reset_fault', '人工故障复位', idle)
            if directional:
                steps.append('仅按物理限位允许的反方向点动脱离；不要求回零，不按950/952文字猜测方向。')
            else:
                steps.append('限位警告保留显示；当前两侧均未触发，可双向点动，无需因警告重新检查；人工故障复位可选。' if code in (0x950,0x952) else ('点击“人工故障复位”，完成失能、清警及检查；通过后可再次点击“一键回零”，不会自动运动。' if code==0x601 else '可按需点击“人工故障复位”，实际是否清除以读回为准。'))
            steps.append('报警不单独阻止操作；由操作者判断，驱动器决定是否接受。人工复位不保证能清除。')
        if not armed and jog_alarm_ok and eeprom_ok and valid_di and storage_ok:
            steps.append('点击“检查 / 恢复”，解除软件锁定并核对驱动条件。')
            action('arm', '检查 / 恢复', idle)
        if not trusted and not home_blocked(data):
            if upper:
                steps.append('上限位有效，先现场检查；当前不能开始负向回零。')
            else:
                steps.append('基础点动不需要回零；仅自动定位需要建立位置参考。')
                action('home', '模式17一键回零', base and armed and not home_issue and not home_blocked(data), home_issue)
        elif trusted and not in_range and height is not None:
            key, label = ('min', '全收') if height < lo else ('max', '全升')
            steps.append('当前位置在工作范围外，可设置 %.2f–%.2f cm 内的目标进行PP定位，也可点击“%s”。' % (lo, hi, label))
            action(key, label, base and armed and pp_ok and not blocked(data, 'up' if key == 'min' else 'down'))
        if not steps:
            steps.append('可输入目标高度定位，或按住方向按钮点动；边界处只允许向范围内运动。')
        summary = ('仅允许条件明确的反向脱离' if directional else
                   '可以进行正常控制' if base and armed and trusted and not code and (pp_ok or pv_ok) else
                   '操作条件未齐，请按以下步骤恢复')
    rejection = getattr(service, 'last_rejection', None)
    if rejection and online and valid_di and not upper and not lower and '物理限位禁止该方向' in rejection.get('message',''):
        rejection = None
    if rejection:
        message = rejection.get('message', '')
        hint = None
        if '单次位移' in message:
            hint = '本次位移过大：分段设置目标，每次不超过 %.2f cm。' % service.config.cm(service.config.max_move_pulse)
        elif '速度为0' in message or '安全点动' in message:
            hint = '检查对应模式速度是否为0；距端点太近时，改用PP定位到端点。'
        elif '目标超出' in message:
            hint = '将目标高度设在 %.2f–%.2f cm 范围内。' % (lo, hi)
        elif '软件行程边界' in message:
            hint = '已到软件端点，只能向软件范围内运动。'
        elif '速度超过' in message or '最大速度' in message or '速度范围' in message:
            hint = '降低对应模式速度，核对独立上下限和驱动器最大速度。'
        if hint:
            steps.insert(0, hint)
    jog_base = base and armed and pv_ok and not getattr(ctrl,'emergency',False)
    jog_up = bool(jog_base and not jog_blocked(data,'up'))
    jog_down = bool(jog_base and not jog_blocked(data,'down'))
    if (jog_up or jog_down) and not moving and (not data.get('untracked_motion') or data.get('stop_confirmation')=='ack') and not manual:
        summary = '可以基础点动（无需回零）' if jog_up and jog_down else '仅允许向下点动脱离' if jog_down else '仅允许向上点动脱离'
    if service.config.mode=='real':
        actions=[a for a in actions if a['id'] not in ('min','max')]
        steps=[s for s in steps if '点击“全收”' not in s and '点击“全升”' not in s]
        if not moving:
            steps.append('实机PP定位暂未开放；可基础点动或执行模式17回零。')
    return dict(summary=summary, checks=checks, actions=actions, steps=steps,
                can_jog_up=jog_up,can_jog_down=jog_down,
                can_move=bool(service.config.mode != 'real' and base and armed and trusted and alarm_ok and pp_ok),
                manual=manual, last_rejection=rejection)
