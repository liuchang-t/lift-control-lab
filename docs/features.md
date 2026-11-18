# 功能、接口与行为差异

候选实现只覆盖升降杆，不包含云台、视频或机器狗业务。冻结原平台基线用于模拟对照；本工具的 API、配置和网页不承诺原平台逐字节兼容。

## 功能边界

| 功能 | 当前行为 |
|---|---|
| 启动 | 默认真实设备且断开，不创建真实通信；显式保存的模拟选择仍可恢复并自动运行模拟器 |
| 连接设置 | 页面保存模式、主机、端口、从站、超时；下一次显式连接生效，无需重启；真实连接需确认 |
| 检查 / 恢复 | 设备条件预检及故障后恢复，不是权限获取，不自动运动或回零 |
| 基础JOG | P0D-11=1/17/33/65，速度0x4604整数rpm；无回零/位置可信/软件高度范围门槛 |
| 使能/失能 | 独立命令，不启动运动；命令应答与实际功率级状态分开 |
| 连续点动 | 每次读取P0D-11，非1才使能；停止以正确应答完成，不等待静止，不循环失能/使能 |
| 点动保护 | 实际DI限制方向；950/952单独不锁定；其他故障、松手、动作心跳和通信保护保留 |
| 点动无运动 | 仅诊断，不锁定或自动停止；同时存在950时建议人工复位，952不作同等推断 |
| PP与HOME | 仅模拟开放；实机旧强制DI路径暂禁用 |
| 模拟定位 | 可信参考、目标范围和单次位移检查；允许从软件范围外返回范围内目标 |
| 模拟回零 | 模式17，确认本次启动/完成及最终参考；全收默认参考上方1cm |
| 速度与参数 | PP/JOG独立cm/s速度；统一换算，原子保存；重启不恢复运动或位置可信度 |
| 报警 | 原码、手册解释、工具策略与处置建议；人工复位，不自动重启 |
| 寄存器 | 快速保护采样、动态轮转、固定配置按需读取；硬件R/W属性与读/写应答分开 |
| 手动485 | 自动CRC，03/06/10；确认写优先处理，不自动重试原始写入 |
| 日志 | 20,000条缓存、暂停固定视图、清空和导出；重启清空内存记录 |
| 私有客户端 | 共享后端，无独占租约；停止优先和旧动作取消机制保留 |

当前交付参数为软件范围1–182cm、行程183.7cm、机械比例0.35921486cm/转。配置加载顺序为默认值、启动文件、页面参数保存、连接保存；后者覆盖前者。速度档案按设备身份保存，旧键 `pv` 对应 JOG。负载只展示原码：`load_raw`为uint16，`load_signed`为int16解释，`load_percent=null`、`load_scale_verified=false`。

## HTTP

默认地址 <http://127.0.0.1:8765>。POST要求 `X-Patrol-Client-Id`，跨站写拒绝。标识用于关联请求，不是独占控制权。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET / POST | `/api/config` | 脱敏配置 / 停机时保存允许的参数，撤销旧参考与检查状态 |
| GET | `/api/connection` | 当前连接、保存目标、连接状态和错误 |
| POST | `/api/connection/save` | 保存下一次目标；兼容POST `/api/connection` |
| POST | `/api/connection/connect` | 显式连接；真实设备要求 `confirm_real` |
| POST | `/api/connection/disconnect` | 本工具活动任务先停止；纯监视只断开 |
| GET | `/api/status` | 缓存状态；兼容 `/api/devices/lift_pole_1/lift/status` |
| POST | `/api/arm` | 检查 / 恢复，实机需 `hardware_confirmed:true` |
| POST | `/api/control` | 升降命令；兼容 `/api/devices/lift_pole_1/lift/control` |
| GET | `/api/readiness` | 缺失条件、当前诊断、恢复步骤与拒绝原因 |
| GET | `/api/registers`、`/api/parameters` | 观测缓存/参数；断开时不隐式连接 |
| POST | `/api/registers/refresh` | 空闲读回；运动和接管期间返回缓存 |
| GET | `/api/logs` | 当前保留的事件与报文 |
| POST | `/api/logs/tail`、`/api/logs/clear` | 增量读取 / 清空缓存 |
| GET | `/api/events` | SSE状态更新 |
| GET | `/api/manual/status` | 手动写接管状态 |
| POST | `/api/manual/transaction` | 完整RTU帧；写需 `confirm_write` |
| POST | `/api/manual/exit` | 停止并结束手动接管 |
| POST | `/api/inject` | 仅模拟开发测试，实机拒绝、页面不展示 |

旧 `/api/devices/lift_pole_1/lease` 及其 `acquire/renew/release` 接口只提供兼容状态/无操作，不抢占、不续期、不因租约停止。

常用控制请求：

```json
{"action":"up","intent_id":"每次按下的新UUID"}
{"action":"heartbeat","intent_id":"本次按下的UUID"}
{"action":"stop","intent_id":"本次松开的UUID"}
{"action":"set_speed","mode":"pv","speed_cm_s":0.35}
{"action":"set_speed","mode":"pp","speed_cm_s":1.05}
{"action":"estop"}
{"action":"reset_fault","confirm_reset":true}
{"action":"home","confirm_homing":true}
{"action":"goto","target_cm":10}
{"action":"min"}
{"action":"max"}
```

HOME、goto、min/max目前仅模拟。心跳只延续已有动作，不能启动；迟到的旧意图停止不能打断新点动。无模式的旧 `set_speed` 拒绝；`goto.height` 仅为相对 `travel_cm` 的百分比兼容输入，仍验证软件范围。候选不接受旧 `set_retracted_zero`。

状态包含 `online/control_mode/implementation/details`；details提供高度、速度、期望速度、报警建议、可信参考、参数观测和停止状态。`motion_notice/motion_advice` 是诊断，不等价于故障锁定。950无运动建议随运动恢复、动作结束或950消失清除；物理限位停止提示在有效新反馈确认脱离后清除，历史日志保留。

## Bridge、MQTT与冻结基线

Bridge默认关闭，启用后使用 `X-Patrol-Bridge-Token` 或 Bearer token，支持health、snapshot、lift/status、lift/lease、lift/control。不向Bridge开放HOME、人工故障复位、max/min和旧设零操作；不伪造升降以外设备数据。共享客户端不取消Bridge的启用和token边界。MQTT为可选状态发布，MQTT3.1.1 QoS0、无TLS。

冻结基线仅模拟，保留原比例速度、人工零点及已知缺陷；不支持候选HOME、复位和独立速度。原UI证据不运行。模拟模型不能证明实机惯性、开关响应、自锁承载、驱动器软限位或断网停机能力。实机PP/HOME边界与未验证事项见[验证说明](validation.md)。
