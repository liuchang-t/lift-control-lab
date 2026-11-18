"""DS2-PR manual PDF pp27-30 (printed pp25-28).

Manual severity/reset capability and laboratory actions are separate facts.
Advice is for inspection, never an automatic parameter-writing recipe.
"""

# code, name, manual resettable, manual PDF page, inspection advice
_FAULTS = [
('101','P02及以上组参数异常',False,27,'核对参数范围及写参时供电；备份参数后由维护人员检查存储器，勿直接初始化。'),
('104','可编程逻辑中断故障',False,27,'停止使用并联系厂家检查 MCU/驱动器硬件。'),
('105','内部程序异常',False,27,'备份参数，检查存储异常；按厂家流程维修，禁止自动初始化。'),
('108','参数存储故障',False,27,'检查参数存储与供电，保留参数备份；联系厂家。'),
('111','内部故障',False,27,'记录报警及参数快照，联系厂家检查。'),
('120','产品匹配故障',False,27,'核对电机、驱动器型号及型号参数。'),
('121','伺服ON指令无效故障',True,27,'检查 DI/虚拟 DI 功能及有效电平配置。'),
('122','绝对位置模式产品匹配故障',False,27,'核对绝对值电机型号、编码器与驱动器匹配。'),
('130','DI功能重复分配',True,27,'检查 DI 和虚拟 DI 功能是否重复；确认限位与使能分配。'),
('131','DO功能分配超限',True,27,'检查 DO 功能编号与本型号支持范围。'),
('136','电机ROM数据校验错误或未存入参数',False,27,'核对型号、编码器接头、线缆和干扰，联系厂家。'),
('201','过流2',False,27,'停机检查 UVW 接线、短路、编码器、负载和加减速；电气检查由维护人员执行。'),
('207','D/Q轴电流溢出故障',True,27,'电流反馈异常，联系厂家检查驱动器；复位不能代替维修。'),
('208','系统采样运算超时',False,27,'检查编码器连接和电磁干扰，联系厂家检查采样硬件。'),
('210','输出对地短路',False,28,'停机隔离供电后检查动力线和电机绝缘，联系厂家。'),
('220','相序错误',False,28,'检查 UVW 相序、编码器和电机参数；不自动执行自学习。'),
('234','飞车',False,28,'立即现场处置；检查反馈方向、UVW、编码器及驱动器，禁止直接重启。'),
('400','主回路电过压',True,28,'检查供电、制动电阻和减速设置；不自动提高减速度或改电阻参数。'),
('410','主回路电欠压',True,28,'检查供电与掉电情况，确认位置参考是否丢失。'),
('420','主回路电缺相',True,28,'检查供电并联系厂家检查驱动器。'),
('430','控制电欠压',True,28,'检查控制供电和驱动器，重新确认位置参考。'),
('500','超速报警',True,28,'检查速度指令、UVW、反馈与增益；禁止未经排查直接复位重启。'),
('510','脉冲输出过速',True,28,'核对编码器分频输出配置 P05-17 与允许频率。'),
('602','角度辨识失败',True,28,'检查编码器线缆与类型；自学习须按维护流程执行。'),
('610','驱动器过载',True,28,'检查负载、卡滞、型号、UVW 和加减速，排除原因后恢复。'),
('620','电机过载',True,28,'检查负载、卡滞、型号、UVW 和加减速，排除原因后恢复。'),
('625','抱闸非正常关闭',True,28,'本设备无电机抱闸：核查驱动器抱闸保护配置与设备型号，不要寻找不存在的抱闸继电器。'),
('626','抱闸非正常打开',True,28,'本设备无电机抱闸：核查驱动器抱闸保护配置；机械自锁不能证明反馈或配置正确。'),
('630','电机堵转',True,28,'排查机械卡滞、UVW 缺相/相序和电机参数；禁止反复带故障启动。'),
('650','散热器过热',True,29,'停止运行，检查散热、环境和负载；必要时联系厂家。'),
('731','编码器电池失效',True,29,'检查编码器连接与电池；涉及多圈清除的步骤交维护人员，随后必须回零。'),
('733','编码器多圈计数错误',True,29,'检查编码器及参数；按厂家流程处理多圈信息，随后重新回零。'),
('735','编码器多圈计数溢出',True,29,'核实多圈范围与配置；不自动清计数，维护后重新回零。'),
('740','编码器干扰',False,29,'检查编码器接头、布线与接地，维护后重新回零。'),
('834','AD采样过压',True,29,'检查模拟输入电压和接线；由维护人员检查输入电路。'),
('835','AD采样故障',True,29,'检查模拟量线缆屏蔽、接地及干扰。'),
('A33','编码器数据异常',False,29,'检查串行编码器线缆与内部参数，维护后重新回零。'),
('A34','编码器回送校验异常',False,29,'核对电机类型及编码器连接，维护后重新回零。'),
('A35','Z信号丢失',False,29,'检查编码器信号、接头及电机，维护后重新回零。'),
('B00','位置偏差过大',True,29,'检查卡滞、UVW、转矩限制、速度及增益；不自动对齐后复位，不自动放宽误差阈值。'),
('B01','脉冲输入异常',True,29,'检查输入频率、屏蔽接地及干扰。'),
('B03','电子齿轮比设定超限',True,29,'检查电子齿轮比与换算配置，修正后重新确认坐标。'),
('D09','软限位异常',True,29,'检查驱动器软上限是否小于下限；对照平台期望值，禁止自动放宽。'),
('D10','原点偏置异常',True,29,'检查原点偏移与软限位关系；维护后重新回零。'),
]
_WARNINGS = [
('110','分频脉冲输出设定故障',True,29,'核对 P05-17 与编码器规格和输出功能。'),
('601','回原点失败',True,30,'检查原点/限位信号、方向和回零时限。点击人工故障复位，工具停止、撤销HOME使能并核对DI3后清警；清除及检查通过后可重新点击一键回零，不自动启动。手册P05-34/35引用有歧义，不自动写参。'),
('730','编码器电池警告',True,30,'安排检查编码器电池与连接，按厂家流程维护；重新核实坐标。'),
('831','AI零漂过大',True,30,'检查模拟输入接线、屏蔽及零输入条件。'),
('900','DI紧急刹车',True,30,'检查物理/虚拟 DI 急停来源；确认现场安全后人工解除，不通过复位绕过输入。'),
('909','电机过载警告',True,30,'检查负载与堵转，避免继续积累热负荷。'),
('920','制动电阻过载',True,30,'检查母线电压、减速及电阻选型；不自动改变制动参数。'),
('922','外接制动电阻过小',True,30,'由维护人员核对实际电阻与驱动器允许值。'),
('939','电机动力线断线',True,30,'停机检查电机动力线、接头与相电流；禁止直接重启。'),
('941','变更参数需重新上电生效',True,30,'记录参数差异，按现场规程重新上电后重新预检；工具不自动断电。'),
('942','参数存储频繁',True,30,'读取 P0C-13，核查频繁写参来源；工具不自动修改 EEPROM 更新设置。'),
('950','正向超程警告',True,30,'核对实际上下限位DI；现场下限位也可能报950。基础JOG方向按当前DI：无限位时双向可用，单侧有效时只许反向；残留950/952不要求重新检查，可人工复位清除。'),
('952','反向超程警告',True,30,'核对实际上下限位DI；基础JOG方向按当前DI：无限位时双向可用，单侧有效时只许反向；不以报警名称猜方向，残留警告可人工复位清除。'),
('980','编码器内部故障',True,30,'联系厂家检查编码器，维护后重新回零。'),
('A40','内部故障',True,30,'检查电机自学习、编码器线缆及类型；不自动自学习。'),
]
REFERENCE_LOSS = {int(x,16) for x in ['122','136','208','234','410','430','601','730','731','733','735','740','A33','A34','A35','B00','B03','D10','980','A40']}
CATALOG = {}
for category, rows in [('fault', _FAULTS), ('warning', _WARNINGS)]:
    for code, name, reset, page, advice in rows:
        number = int(code,16)
        CATALOG[number] = dict(code=number, text='Er.'+code, name=name, manual_category=category,
            manual_resettable=reset, source_pdf_page=page, advice=advice,
            reset_allowed=True,
            reference_lost=number in REFERENCE_LOSS,
            policy='operator_decides')


def describe(raw):
    raw = int(raw) & 0xFFFF
    code, encoding = raw, 'exact'
    # Preserve original evidence; never strip arbitrary leading nibbles.
    aliases = {0x6B00:0xB00, 0x6620:0x620, 0xE950:0x950, 0xE952:0x952}
    verified_aliases = {0xE601:0x601, 0x6121:0x121}
    if raw in verified_aliases:
        code, encoding = verified_aliases[raw], 'field_observed_alias'
    elif raw in aliases:
        code, encoding = aliases[raw], 'legacy_observed_alias_pending_hardware_verification'
    if code == 0:
        result = dict(code=0,text='',name='无报警',manual_category='none',manual_resettable=False,
                      reset_allowed=False,reference_lost=False,policy='none',advice='保持状态监测。')
    else:
        result = dict(CATALOG.get(code, dict(code=code,text='Er.%X'%code,name='未识别报警',
            manual_category='unknown',manual_resettable=None,reset_allowed=True,reference_lost=False,
            policy='operator_decides',advice='含义及复位能力尚未确认；保留原始码，由操作者判断。可人工尝试复位，以实际读回判断结果，不自动复位。')))
    result.update(raw_code=raw,raw_hex='0x%04X'%raw,encoding=encoding)
    return result


def blocked(sample, direction=None, homing=False):
    """Physical limits guard direction; alarm interpretation is advisory."""
    if sample.get('di4_function') != 14 or sample.get('di5_function') != 15:
        return '限位映射不符合预期'
    if sample.get('di4_logic') not in (0,1) or sample.get('di5_logic') not in (0,1):
        return '限位极性未知'
    upper, lower = sample.get('upper_limit'), sample.get('lower_limit')
    if upper is None or lower is None or upper and lower:
        return '限位未知或上下同时有效'
    if not homing and ((upper and direction=='up') or (lower and direction=='down')):
        return '物理限位禁止该方向'
    if homing and upper:
        return '上限位有效，禁止开始/继续负向回零，先人工检查'
    return ''


def jog_blocked(sample, direction):
    """Physical DI determines direction; field captures show DI5 + E950."""
    if sample.get('di4_function') != 14 or sample.get('di5_function') != 15:
        return '限位映射不符合预期'
    if sample.get('di4_logic') not in (0, 1) or sample.get('di5_logic') not in (0, 1):
        return '限位极性未知'
    upper, lower = sample.get('upper_limit'), sample.get('lower_limit')
    if type(upper) is not bool or type(lower) is not bool or (upper and lower):
        return '限位未知或上下同时有效'
    if (upper and direction == 'up') or (lower and direction == 'down'):
        return '物理限位禁止该方向'
    return ''


def home_blocked(sample):
    """HOME uses actual physical DI, never alarm names as switch feedback."""
    reason=jog_blocked(sample,'up')
    if reason and reason!='物理限位禁止该方向':
        return reason
    if sample.get('upper_limit'):
        return '上限位有效，禁止开始/继续负向回零，先人工检查'
    return ''
