# 功能、接口与行为差异

候选实现覆盖升降领域，不包含云台、视频或机器狗业务。冻结基线用于复现旧算法；候选 API、配置和网页有明确行为变化，不是原平台逐字节兼容服务。

## 功能

| 功能 | 候选行为 |
|---|---|
| 模拟/实机 | 默认模拟；页面选择模式后需明确点击连接，real另需确认；CLI直接real仍需--allow-real |
| 连接/断开 | 保存目标不自动生效；显式连接无需重启，切换和断开遇本后端活动任务时先确认停止；纯监视直接关闭；停止失败保留旧保护任务 |
| 检查 / 恢复 | 参数预检与故障后运行恢复检查，不是操作者权限授予，也不等同于回零 |
| HOME | 本地人工确认，模式17寻找负限位释放沿，要求本次完成证据与静止后确认参考 |
| 全收 | PP 到 soft_lower_cm，默认1cm，直接显示1cm |
| 全升 | PP 到 soft_upper_cm，受单次位移限制 |
| 定高 | target_cm 输入，可信参考和目标范围检查 |
| 按住升降 | PV 独立速度，intent_id/heartbeat，松手、失焦、隐藏、超时停止 |
| 限位保护 | PP目标检查；PV依据停止模型提前处理；物理DI方向拦截 |
| 停止/软件急停 | 取消旧启动，停机待确认，失败后台重试；停止不受客户端独占限制 |
| PP/PV调速 | 独立上下限与cm/s设置；只改当前运行模式寄存器，另一模式仅保存 |
| 速度恢复 | 设备/模拟身份隔离、原子保存；不恢复使能或运动意图 |
| 报警 | 手册字典、原始码、分类、人工建议及平台处置策略；未知码保守阻挡 |
| 人工故障复位 | 本地、确认原因已处理、确认停止、一次复位并读回；不自动重启或恢复参考 |
| 设备参数 | 读取P0A/P10软限位和HOME参数等，实际值与期望值分开 |
| P0C-13 | 连接后只读检查，1明确警告，不自动修改 |
| 共享客户端 | 私有客户端共享操作，不设独占、获取/释放、续期或租约超时停止；串行处理与停止优先保留 |
| 条件与恢复 | 固定提示区显示缺失条件、最近拒绝原因和下一步；不会替代执行时的保护检查 |
| 手动总线 | 页面支持自动CRC或完整RTU报文，03/06/10；确认写优先接管，停止/报警保护保留，原始运动不承诺软件限位 |
| 寄存器监视 | 按当前监视目录；快速状态、分组扫描与空闲刷新分开，读回/写应答/过期分开 |
| 动态参数 | 按风险分类监视外部变化，区分工具自写与未预期变化；公开读取策略与新鲜度 |
| 日志/状态 | 原始RTU、命令、故障及状态变更；HTTP、SSE、可选MQTT |

## HTTP

默认 <http://127.0.0.1:8765>，只监听本机。POST 要求 `X-Patrol-Client-Id`；跨站写拒绝。不应开放至公网。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/config` | 已脱敏配置 |
| GET | `/api/connection` | 当前连接、保存目标及连接状态/错误 |
| POST | `/api/connection/save` | 保存下一次连接目标，不自动连接；兼容POST `/api/connection` |
| POST | `/api/connection/connect` | 应用目标并显式连接；real须confirm_real |
| POST | `/api/connection/disconnect` | 本后端活动任务先确认停止；纯监视不发停止，关闭连接与采集 |
| POST | `/api/config` | 静止时修改允许的参数并原子保存；使参考/允许状态失效 |
| GET | `/api/status` 或 `/api/devices/lift_pole_1/lift/status` | 缓存状态及观测年龄 |
| POST | `/api/arm` | `{hardware_confirmed:true}`，实机必须确认 |
| POST | `/api/control` 或 `/api/devices/lift_pole_1/lift/control` | 升降命令 |
| GET | `/api/devices/lift_pole_1/lease` | 旧客户端兼容状态；不代表独占权限 |
| POST | `/api/devices/lift_pole_1/lease/{acquire,renew,release}` | 旧客户端兼容无操作，不续期、不抢占、不停止 |
| GET | `/api/events` | SSE，`{status,lease,mqtt}` |
| GET | `/api/logs` | 最近2000条事件/报文 |
| POST | `/api/inject` | 仅开发测试的模拟故障接口；实机拒绝，页面不显示 |
| GET | `/api/readiness` | 当前页面的条件、恢复步骤和最近拒绝原因 |
| GET | `/api/registers` | 寄存器观测缓存 |
| POST | `/api/registers/refresh` | 空闲刷新可读清单；运动/接管时返回缓存 |
| GET | `/api/manual/status` | 手动接管与锁定状态 |
| POST | `/api/manual/transaction` | 完整RTU报文；写需confirm_write，首次写优先接管 |
| POST | `/api/manual/exit` | 确认停止并结束手动接管 |

命令示例：

```json
{"action":"home","confirm_homing":true}
{"action":"min"}
{"action":"max"}
{"action":"goto","target_cm":10}
{"action":"set_speed","mode":"pp","speed_cm_s":1.05}
{"action":"set_speed","mode":"pv","speed_cm_s":0.35}
{"action":"up","intent_id":"每次按下的新UUID"}
{"action":"heartbeat","intent_id":"同一次按下的UUID"}
{"action":"stop","intent_id":"松开的UUID"}
{"action":"estop"}
{"action":"reset_fault","confirm_reset":true}
```

`goto.height` 保留为百分比兼容输入，百分比相对于 `travel_cm`，仍须通过软件上下限检查；新客户端优先使用 `target_cm`。无模式的旧 `set_speed` 在候选实现中拒绝，避免含糊地修改两套速度。候选拒绝旧 `set_retracted_zero`，不能用任意位置人工设零代替HOME。

心跳只延续已有运动，不能启动新运动。开始/停止使用相同 intent_id，使迟到启动在停止后仍被拒绝。HOME 和 PP 不以页面保活作为完成条件；仍受停止、故障和自身保护约束。客户端共享不取消PV点动的动作心跳和迟到启动拦截。

状态包含 `online/control_mode/implementation/details`。details 提供高度、转速、速度设置与已下发速度、报警/建议/历史、HOME 状态、位置可信度、参数快照、软件范围、限位保护说明、观测年龄和停机待确认。控制响应包含 `ok/operation/payload.state/status`。

连接目标包含`mode/host/port/slave_id/timeout`；选择或保存目标不会改变当前通信。断开后状态、寄存器及参数读取不得隐式连接，主动设备操作需先显式连接。

界面采用固定状态栏与内部滚动面板；开发用集中平台对照与故障注入不呈现在操作页面。手动总线规则和完整寄存器清单见[寄存器说明](registers.md)。

## Bridge

启用后使用 `X-Patrol-Bridge-Token` 或 `Authorization: Bearer ...`。支持 health、snapshot、lift/status、lift/lease 及 lift/control。不实施客户端独占租约。HOME、人工故障复位、max/min 和旧设零操作不向 Bridge 开放；既有Bridge启用开关与token校验保留，不因共享客户端而自动放宽网络边界。升降以外数据保持为空，不伪造其他设备。

## 冻结基线

只允许模拟。基线保留旧比例速度、人工零点和原控制缺陷，用于对照；不支持候选HOME/复位/独立速度。原 UI 代码作为证据保存但不运行。新工具网页及HTTP外壳不属于冻结原算法。

## 观察边界

驱动器软限位的两套参数关系、PV/JOG适用性、HOME例外和触限停止方式待实机验证，仅展示读取值。模拟器的机械/开关位置、回差和HOME结束过程是测试模型，不能作为现场事实。模拟器不证明惯性、自锁承载、停机距离或真实断网保护有效。