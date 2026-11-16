# 随机导览试点 · 离线派号与关场分析

馆方随机导览试点配套模块：按展区预生成**等量 A/B 随机号包**，签名后分发到设备
**完全离线派号**；联网回传时做幂等合并与冲突隔离；全部设备交回未用号后才能关场；
关场且无未决争议后生成可批准分发的 ITT 分析。**仅依赖 Python 3.11+ 标准库与 SQLite**，
现场断网、无 Postgres/Redis 也能运行。

## 需求口径与实现对应

| 业务要求 | 实现位置与机制 |
|---|---|
| 按展区预生成等量两方案随机号包 | `services.create_batch`：等量 A/B + Fisher–Yates 洗牌，种子留痕可复现 |
| 分给设备离线使用 | `services.allocate_segment` 生成号段与 HMAC-SHA256 签名号包 JSON；`device.DeviceClient` 验签后本地 SQLite 派号 |
| 已领号段不再转给别机 | 号段与设备一对一，号码行锁定 `segment_id`；同批次同设备重复领段、号段已存在时载入新段均拒绝 |
| 号位绑定到访编号及完成情况 | `number_pool.visit_id`、`state`、`arm_used`；设备端完成事件必须与派号到访编号一致 |
| 跨设备重复领号 / 同号内容冲突隔离 | `merge_upload` 写入 `disputes`，原数据不被覆盖；未解决前整场不出分析 |
| 回传合并幂等、重试不重复派号 | 服务端按 `upload_id` 去重、按 `(device_id,event_id)` 去重；设备端按本地 `seq` 生成稳定 event_id 并有回传游标 |
| 设备交回未用号后才可关场 | `return_segment` 只回收 `unused`（已派号保留分母）；`close_batch` 校验所有号段 `returned` |
| 关场与回传并发不漏人 | 关键写区共用进程锁 + `BEGIN IMMEDIATE` + SQLite busy_timeout，串行化合并/交回/关场 |
| 中断恢复不重复派号或建分析 | 设备本地库持久化游标与事件；分析按批次唯一，重复调用返回同一快照 |
| 完成者换方案按原分配统计 | ITT：完成计入号码 `arm`，实际方案记入 `arm_switchers` 仅作披露 |
| 未完成者保留分母 | 分母 = `issued + completed`（已派未完成不剔除） |
| 关场后生成可批准分发的分析 | `create_analysis` → `approve_analysis` → `mark_distributed`，快照不可变 |
| 旧批次照常核验 | 分析快照独立持久化，新批次不影响旧批次，页面/接口随时可查 |
| 页面可查发号、回收、争议、关场结果 | `web/` 原生 HTML/JS 五标签页，由同一 HTTP 服务托管 |

## 目录

```
pilot/
  pilot/
    storage.py   SQLite 表结构与连接（WAL、外键、busy_timeout）
    config.py    数据目录与号包签名密钥
    stats.py     2×2 Pearson χ²（无连续性校正）、Cramer V、期望频数告警
    services.py  批次/号段/回传/争议/关场/分析领域服务（核心）
    device.py    离线设备端：验签、本地派号状态机、回传游标
    server.py    标准库 ThreadingHTTPServer，REST API + 静态页面
    cli.py       管理端与设备端命令行
  web/           index.html / app.js / styles.css
  tests/         14 个端到端验收测试
```

## 快速开始

```bash
cd pilot
export PILOT_HOME=./pilot_data          # 服务端数据目录（含 SQLite、号包、密钥）

# 1. 建批次：展区"水世界"，A/B 各 20 个随机号
python -m pilot.cli create-batch 水世界 20 --seed 1005

# 2. 分配号段给两台设备（签名号包写入 $PILOT_HOME/packages/）
python -m pilot.cli assign <batch_id> D1 --size 20
python -m pilot.cli assign <batch_id> D2 --size 20

# 3. 导出验签密钥，随号包通过可信通道预置到设备
python -m pilot.cli export-key ./device_d1/package.key
python -m pilot.cli device-init D1 ./device_d1 \
    --package $PILOT_HOME/packages/<segment_d1>.json

# 4. 设备完全离线派号 / 报完成（中断后重开同一目录，状态不丢、不重号）
python -m pilot.cli issue D1 ./device_d1 V1001
python -m pilot.cli complete D1 ./device_d1 <code> V1001 --arm-used B
python -m pilot.cli device-status D1 ./device_d1

# 5. 联网回传（可反复重试，服务端幂等）
python -m pilot.cli upload D1 ./device_d1 --base http://127.0.0.1:18304

# 6. 清点未用号并交回，全部设备交回后关场
python -m pilot.cli device-return D1 ./device_d1
python -m pilot.cli return <batch_id> <segment_id>
python -m pilot.cli close <batch_id>

# 7. 争议处理 → 分析 → 批准 → 分发
python -m pilot.cli disputes --open
python -m pilot.cli resolve <dispute_id> "保留首次派号"
python -m pilot.cli analyze <batch_id>
python -m pilot.cli approve <batch_id> --note "复核通过"
python -m pilot.cli distribute <batch_id>

# 工作台页面：http://127.0.0.1:18304
python -m pilot.cli serve --port 18304
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/batches` | 建批次 `{zone, qty_per_arm, seed?}` |
| GET | `/api/batches` / `/api/batches/{id}` | 总览（发号/回收/争议/关场）/ 详情 |
| POST | `/api/batches/{id}/segments` | 分配号段，返回带 `signature` 的号包 |
| POST | `/api/uploads` | 回传合并，幂等；冲突进 `disputes` |
| POST | `/api/segments/{sid}/return` | 交回号段（仅回收未用号） |
| POST | `/api/batches/{id}/close` | 关场（所有号段交回后） |
| GET | `/api/disputes?open=1` | 争议列表 |
| POST | `/api/disputes/{id}/resolve` | 标记争议解决 |
| POST | `/api/batches/{id}/analysis` | 生成/取回分析（幂等） |
| POST | `/api/batches/{id}/approve` `/distribute` | 批准、标记分发 |
| GET | `/api/audit` | 审计轨迹 |

## 状态机与统计口径

- 号码状态：`unused → issued → completed`；交回时未用号转 `reclaimed`，已派号不动。
- 号段状态：`assigned → returned`；批次状态：`open → closed`（关场后拒绝回传与再分配）。
- ITT 列联表：行=号码原分配 A/B，列=完成/未完成；分母只含实际派出的号码。
- χ² 无连续性校正，`p = erfc(√(χ²/2))`（df=1 精确生存函数）；最小期望频数 < 5 给告警；
  零合计等退化情形返回 `stats_error`，不伪造统计结果。

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：等量随机化、号段不转拨、号包篡改拒绝、离线派号/恢复幂等、回传重试幂等、
跨设备重复领号与同号冲突隔离、交回前置条件、关场与回传并发不漏人、
ITT 换方案与未完成分母、分析幂等、旧批次可核验、HTTP 全链路。

## 安全与限制

- 演示用 HMAC 密钥与设备库均为本地文件；生产部署应更换密钥管理方式并启用传输加密。
- 争议只做隔离与留痕，不自动裁决；解决结论需人工录入。
- 单机单进程服务靠进程锁串行化关键写区；多实例部署需改用数据库事务/队列协调。
