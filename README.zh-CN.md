# GPUbk

[English](README.md) | **简体中文**

GPUbk 是面向 Linux 共享服务器的 GPU 预约工具。PyPI 包名是 `gpubk`，
日常命令使用更短的 `bk`。

它可以完全离线运行，使用本地文件保存数据，核心功能没有第三方运行时依赖。
用户可以通过普通终端会话、curses TUI、JSON 命令或可选的本地 MCP 服务预约 GPU。

## 主要功能

- shared 与 exclusive 预约粒度可配置，默认 5 分钟。
- 自动排队、实时 GPU 感知和每卡显存预算。
- 同时适配深色、浅色终端的紧凑时间轴。
- 到点执行命令并自动设置 `CUDA_VISIBLE_DEVICES`。
- 基于 NVML 的进程监测和近期负载历史。
- 稳定 JSON、MCP 工具、内置 Codex Skill 和可选外部 allocator。
- 原子文件事务、UID 权限检查、备份和只追加审计日志。

GPUbk 是协作式调度器，不代替 Linux 设备权限。拥有 `/dev/nvidia*` 直接访问权的
用户仍可绕过本工具，管理员需要另行配置设备访问策略。

## 安装

GPUbk 需要 Python 3.10 或更高版本。

```bash
python3 -m pip install gpubk          # 核心 CLI 与 TUI，无依赖
python3 -m pip install 'gpubk[gpu]'  # 增加低开销 NVML 监测
python3 -m pip install 'gpubk[mcp]'  # 增加本地 MCP 服务
python3 -m pip install 'gpubk[all]'  # 安装全部可选功能
```

检查安装：

```bash
bk --version
bk --help
```

通过 PyPI 发布的 wheel 可直接使用系统自带 installer。若从 Git 源码目录或 sdist
安装，请先升级当前环境里的 pip：

```bash
python3 -m pip install --upgrade pip
python3 -m pip install .
```

部分旧版 Debian/Ubuntu pip 会忽略 `pyproject.toml` 请求的隔离 setuptools，并静默
生成不可用的 `UNKNOWN` 包。GPUbk 会识别这种情况并明确提示升级，不再假装安装成功。

## 预约 GPU

默认使用 shared 模式：

```bash
bk 1 30m                         # 预约 1 张 GPU，持续 30 分钟
bk 2 1h30m --mem 12g            # 每张卡预计使用 12 GiB 显存
bk 1 1h --share 1/2             # 占用 shared 总容量的一半
bk 1 1h --share-with 1          # 最多给另一条最小份额预约留空间
bk s 1 2h --gpu 3               # 显式 shared，固定 GPU 3
bk x 2 4h                        # exclusive 排他预约
bk 1 1h --at +30m               # 30 分钟后，当地时间
bk 1 1h --at "tomorrow 09:00"  # 明天 09:00
bk 1 1h --start 2030-01-01T20:00:00+08:00  # 脚本使用的精确时间
```

可以用列表序号或短 ID 管理自己的预约：

```bash
bk l
bk e 1 --duration 2h
bk e 1 --at "tomorrow 09:00"
bk d 1
bk lg --limit 100                # 当前 UID 最近的操作记录
bk lg --limit 20 --json          # 有界、机器可读的审计事件
bk config                         # 查看最终生效配置与台账策略
bk doctor                         # 只读检查台账
```

调度规则保持简单：

- 开始时间和持续时间使用服务器配置的预约边界。
- 不传 `--at` 或 `--start` 时优先使用当前预约时间片（默认配置下 `12:41` 从 `12:40` 开始）；
  若必须延后才会显示 `queued:`。
- `--at` 支持 `+30m`、`20:00`、`tomorrow 09:00` 和 `07-13 20:00`；
  `--start` 为脚本和 Agent 保留精确 ISO 8601。两者都不会在冲突时自动挪动。
- 每张 GPU 有 `max_shared_users` 个容量单位。shared 默认占 1 单位；
  `--share 3/4`、`--share 3` 或可精确换算的百分比可以申请更大份额，
  `--share-with 1` 表示保留除 1 单位以外的容量。每个重叠的预约时间片都会独立校验。
- 份额用于预约准入和默认显存估算，不会自动把 GPU SM 算力硬切成 75/25；
  需要物理隔离时仍应配置 MIG、MPS 或设备权限。
- exclusive 不能与任何预约重叠。
- `--mem` 表示**每张 GPU**预计使用的显存，管理员可将其设为 shared 必填项。
- 用户看到的是本地时间，台账内部保存 UTC。

自动选卡会综合已有预约、物理空闲显存、当前 GPU 进程、近期负载和未来预约压力。
若发现没有预约的进程，系统会给出提示，并在有其他合适 GPU 时避开忙卡。

## 查看与查找资源

普通 CLI 是日常主入口：

```bash
bk st                              # 紧凑实时状态
bk st -v                           # 展开进程和全部预约
bk st --timeline                   # 在状态后附加默认时间轴
bk tl                              # 从当前预约时间片显示未来 2 小时
bk tl 8h --step 15m --gpu 0,1
bk tl --from 20:00 --window 1d --step auto
bk slots 2 1h --mem 12g            # 只读查找多个可预约方案
bk slots x 1 30m --limit 3
```

时间轴使用固定宽度单元：`··` 表示空闲；`M1`-`M9` 表示该时间片的 shared
总占用，并且其中包含自己的预约；`S1`-`S9` 表示仅由他人占用的 shared 总容量；
`MX`/`XX` 表示自己的/他人的 exclusive。窄终端会按整小时分块换行，
不会偷偷降低指定粒度。

`bk add` 和不带修改参数的 `bk edit ID` 都是可恢复的引导流程，支持上述自然时间；
输入错误时只重新询问当前字段，还可以输入 `back` 或 `cancel`，写入前会用当地时间
显示变更摘要。已经开始的预约不可编辑，也不能把开始时间改到过去；`--queue`
只会在合法起点发生资源冲突时向后排队，不会悄悄修正非法的过去时间。`bk slots`
只读，并会为第一项方案给出可直接执行的预约命令。

## 终端界面

`bk` 打开普通行式会话，不接管屏幕，也不改变终端背景。`bk t` 打开全屏 TUI。

```bash
bk
bk t
```

常用 TUI 按键：

| 按键 | 操作 |
| --- | --- |
| `a` / `e` / `d` | 新增、编辑或取消预约 |
| `Tab`、`↑`、`↓` | 在预约列表和 GPU 详情之间移动 |
| `←`、`→` | 浏览时间轴；在 Add/Edit 中移动时间 |
| `Space` | 在 Add/Edit 中选择或取消当前 GPU |
| `-`、`=` | 缩放时间轴 |
| `[`、`]` | 按一个已配置预约时间片缩短或延长时长 |
| `,`、`.` | 快速缩短或延长，步长随缩放变化 |
| `v` | 在 1x、6x、24x 调节速度间循环 |
| `Shift` + 调节键 | 终端支持时使用更大的步长 |
| `1`-`9` | 选择 GPU 数量并跳到最近合法时段 |
| `s`、`x` | 在 Add/Edit 中切换 shared/exclusive |
| `u` | 用单位、分数或百分比设置 shared 份额 |
| `f`、`g` | 查找任意可用卡，或固定当前已选卡查找 |
| `n` | 回到带 `NOW` 标记的实时窗口 |
| `c` | 切换深色/浅色主题 |
| `?` | 打开分页帮助和快速上手说明 |
| `Enter`、`Esc`、`q` | 提交、取消当前操作或退出 |

TUI 默认每秒刷新一次。较慢终端或希望降低轮询频率时，可在配置中设置
`tui_refresh_seconds`，或用 `BK_TUI_REFRESH_SECONDS` 覆盖当前环境。

时间轴可以查看过去的预约，但历史只读。Add 和 Edit 提交时，调度器会在文件锁事务内
再次校验所选时段。预约焦点默认停在标题栏，按下方向键后才选中并闪烁某条预约。
不超过 10 张 GPU 时，预约表的 `GPU` 列为每张卡保留固定位置，只在预约使用的
位置显示对应数字，未使用位置留空。
预约 ID 默认统一显示可区分的最短 6 位前缀；发生前缀碰撞时自动增加位数。

## 到点运行命令

把命令写在 `--` 后：

```bash
bk 2 1h30m --mem 12g -- python train.py --config exp.yaml
bk j                    # 查看任务
bk j --cleanup          # 检查并清理私有任务文件
bk w                    # 执行当前用户已经到点的任务
bk w --status           # 只读检查当前用户的 worker 是否在线
bk jr ID --accept-duplicate-risk  # 检查 uncertain 任务后再确认重试
```

worker 会设置 `CUDA_VISIBLE_DEVICES`、`CUDA_DEVICE_ORDER`、
`BK_RESERVATION_ID` 和 `BK_RESERVED_GPUS`。命令和工作目录保存在当前 UID 所有的
`0600` 私有文件里，不会写入共享台账。worker 使用 `shell=False`，并持续监管命令
所在的进程组，直到它退出或预约结束；任务脚本不应自行 daemonize 或创建新 session。
确实需要 shell 语法时应明确调用 shell：

```bash
bk 1 30m -- sh -lc 'python train.py > train.log 2>&1'
```

任务启动前，worker 会再次采样所有分配到的 GPU。exclusive 任务会等待所有非系统
进程退出；shared 任务允许已有的合法共享者，但遇到未预约/身份未知进程或物理显存
不足时会等待。实时探测不可用时也默认拒绝启动。任务保持 `pending` 并显示原因，常驻
worker 会在预约窗口内持续重试；`bk worker --once` 有等待任务时返回状态码 `3`。
只有明确接受兼容性风险时才应设置 `worker_live_guard=false`。

同一个 UID 的私有任务目录同时只允许一个 worker 持有租约；worker 崩溃后内核会自动
释放锁，因此新 worker 可以恢复持久化的 `claimed` / `running` 状态而不会与健康 worker
竞争。Linux 上只检查同 UID 的 `/proc` 记录，精确匹配 `BK_RESERVATION_ID`，并在发送
信号前再次核对身份；先发 TERM，超过 `worker_recovery_grace_seconds`（默认 5 秒）后
再发 KILL。即使残留进程组已停止，任务仍保持 `uncertain`，因为崩溃前可能已产生部分
副作用，重试时必须显式接受重复执行风险。其他主机上的进程绝不会在本机被发送信号。
并发启动第二个 worker 会返回状态码 `75`；内置 systemd unit 不会因此反复重启。
升级时，旧版无租约 worker 创建的活动任务不会被接管，并会暂停领取新任务，直到旧任务
结束或预约到期。

`bk worker --status` 不创建或修改私有存储，并报告 `running`、`stopped`、`not-seen`
或不安全/不可用状态。只有内核锁能证明 `running`；文件里的 PID、主机名和获取时间仅供
诊断。加 `--json` 可得到 `gpubk.worker.v1`，加 `--require-running` 则在租约未被持有时
返回状态码 2。`bk jobs --json` 与 Agent/MCP 上下文也会暴露同一份当前 UID 状态。

预约取消、任务成功、超时或可重试窗口结束后，私有命令 spec 会被清理。worker 会在
启动、退出以及持续运行时最多每 5 分钟检查一次。没有台账引用的规范 spec 会保留
24 小时宽限期，避免与正在提交的预约发生竞争；仍待执行、运行中或可重试的任务不会
被删除。`bk jobs --cleanup --json` 提供同一清理流程的机器可读结果。私有任务日志会
保存在共享数据目录之外。worker 会持续排空脚本的 stdout/stderr，并默认用两个分段
滚动把每个任务的直接输出限制在最多 64 MiB；终态日志保留 30 天，当前 UID 的终态日志总量超过
4 GiB 时从最旧的开始清理。仍在运行或可重试的任务不会删除。`bk jobs --cleanup
--json` 会同时报告 spec 与日志清理结果；`job_log_retention_days`、`job_log_max_mb` 和
`job_log_total_max_mb` 可调整策略，设为 `0` 可关闭对应限制。脚本自行创建的文件，
包括 shell 内部重定向的输出，不受此策略管理。

需要无人值守运行时，每位用户可以安装内置的 systemd user unit：

```bash
bk service install worker
systemctl --user daemon-reload
systemctl --user enable --now bk-worker.service
bk worker --status --require-running
```

在 systemd Linux 上，用户退出后 user manager 可能停止，开机时也不一定自动启动。
确需无人值守任务时，由管理员按用户选择性开启 linger：

```bash
sudo loginctl enable-linger <worker用户>
```

生成的 unit 会固化安装当时生效的绝对 `BK_DATA_DIR`、私有 `BK_JOB_LOG_DIR`，以及
显式设置的 `BK_CONFIG_FILE`。可用 `bk service show worker` 检查；任一路径变化后
使用 `--force` 重新安装。同一 UID 的所有 worker 必须使用同一个私有目录，使租约
只有一个权威位置。其他策略仍从所选配置路径读取，并在服务每次启动时重新加载。
worker 持久启动失败在 60 秒内最多重试 3 次；普通子任务失败只写入任务状态，不会让
长驻 worker 退出。

## 监测与自动选卡

安装 `gpu` extra 后，可以采样一次或持续低开销监测：

```bash
bk m --once
bk m
bk u                              # 当前 UID 最近 24 小时
bk u users --since 30d           # 可见用户汇总
bk u samples --since 2d --resolution 5m --json
bk u events --user me --since 7d
```

NVML 只初始化一次，并复用设备句柄。初始化失败或设备句柄失效后会先短暂退避再重建，
因此驱动的瞬时故障不会让长驻 monitor 永久降级。监测器保存有限长度的调度负载、稀疏的
用户历史，以及进程开始、结束、授权和工作负载变化事件，不会每秒写入一份完整快照。
没有 NVML 时会回退到 `nvidia-smi` 获取设备指标；由于该回退没有可信的进程列表，
GPUbk 会保留最后一次已观测进程状态并报告遥测缺口，不会伪造 stop/start 事件。
monitor 警告与 Agent 的 GPU 详情都会暴露进程列表和进程级利用率能力。
monitor 还会原子更新一个很小的 `usage/collector.json` 心跳。Usage JSON、Agent
上下文、`bk doctor` 与 TUI 顶栏读取同一组 `running`、`degraded`、`stale`、
`stopped`、拓扑不匹配状态。异常退出漏过三次心跳后变为 `stale`，正常退出显示
`stopped`；TUI 中 `M:OK` 表示采集器新鲜，`M:--` 表示尚无心跳记录。
默认每 2 秒采样，并折叠为 60 秒记录。管理员可在可信配置中调整
`monitor_interval_seconds` 与 `monitor_rollup_seconds`；聚合窗口必须是采样间隔的
整数倍。命令行 `--interval`、`--rollup` 只覆盖本次运行。

进程状态根据进程 UID 和有效预约判断，包括 `ok`、`wrong-gpu`、`unreserved`、
`unknown` 和 `system`。命令行写入共享日志前会缩减为安全标签。

历史数据按天分区并带校验和，提供 1 分钟、5 分钟、10 分钟、小时和每日层级。
Python、JSON CLI 和 MCP 统一返回 `gpubk.usage.v1` 公共模型；可视化程序不应
直接解析内部文件。完整说明见 [Telemetry](TELEMETRY.md)。
`usage_load_window_minutes` 同时控制自动选卡保留并纳入判断的近期设备历史长度。

监测器也提供用户服务：

```bash
bk service install monitor
systemctl --user daemon-reload
systemctl --user enable --now bk-monitor.service
bk doctor --require-monitor --strict
```

若主机需要开机及退出登录后持续运行，只为指定 monitor 账号开启 linger：

```bash
sudo loginctl enable-linger <monitor账号>
```

共享服务器只能运行一个受信任的 monitor 写入者，不能每个用户各启一个；每位用户的
worker 仍然相互独立。上述用户 monitor 服务适合私人服务器，或 `monitor_uid` 指定的
唯一账号。
生成的 unit 会固化共享数据目录和显式可信配置路径；服务每次启动都会从该配置重新读取
采样与聚合周期。重复写入者（`75`）或角色不匹配（`77`）不会重启；其他失败在 60 秒
内最多重试 3 次，既允许短暂故障自愈，也不会无限刷日志。
上面的最后一条命令是只读的启动后验收；与部署预检不同，如果从未记录过 collector
心跳，它会明确失败。

## Agent 与 MCP

Agent 应使用版本化 JSON，而不是解析彩色终端文本：

```bash
bk agent context --compact
bk agent recommend 2 1h30m --mem 12g --compact
bk 2 1h30m --mem 12g --op-id run-20260712-001 --json
bk agent edit 6e957ef1 --duration 2h --op-id edit-20260712-001 --compact
bk agent cancel 6e957ef1 --compact
```

create 和 edit 必须使用稳定的 operation ID。完全相同的重试返回 `status=exists`；
同一个 ID 被用于不同写入时会拒绝执行。recommend 只读，身份始终来自本地进程 UID。

启动可选的 stdio MCP 服务：

```bash
bk-mcp                       # 等同于 bk mcp
bk skill install            # 安装 wheel 内置的 Codex Skill
```

MCP 提供 context、recommend、create、list、edit、cancel、私有 spec 清理和私有
任务日志工具。它只使用 stdio，不监听网络端口；每位用户运行自己的 MCP 进程。工具
schema 标明 read-only、idempotent、destructive 和 closed-world 属性。

管理员还可以通过 `BK_ALLOCATOR_COMMAND` 配置受信任的本地程序。它读取
`bk.allocator.v1` JSON，并返回 GPU 排序。外部结果只提供建议，最终仍须通过内置的
冲突、显存、时间、UID 和事务校验。完整格式见
[Agent 协议](src/bk/data/codex-skill/gpubk/references/protocol.md)。

## 多人服务器配置

为实验室用户组创建 setgid 目录：

```bash
sudo install -d -m 2770 -o root -g gpuusers /data2/shared/bk
sudo install -d -m 0755 -o root -g root /etc/gpubk
```

将 root 管理的配置放在 `/etc/gpubk/config.json`，不要与组可写台账目录同置：

```json
{
  "config_version": 1,
  "data_dir": "/data2/shared/bk",
  "gpu_count": 8,
  "slot_minutes": 5,
  "max_shared_users": 4,
  "queue_search_hours": 168,
  "timeline_hours": 2,
  "lock_timeout_seconds": 10,
  "backup_keep": 10,
  "ledger_retention_days": 90,
  "usage_load_window_minutes": 120,
  "usage_minute_retention_days": 30,
  "usage_five_minute_retention_days": 365,
  "usage_ten_minute_retention_days": 1095,
  "usage_hourly_retention_days": 1500,
  "usage_daily_retention_days": 0,
  "usage_event_retention_days": 365,
  "require_shared_memory": true,
  "shared_memory_reserve_mb": 512,
  "job_log_retention_days": 30,
  "job_log_max_mb": 64,
  "job_log_total_max_mb": 4096,
  "worker_poll_seconds": 1,
  "worker_claim_timeout_seconds": 30,
  "worker_recovery_grace_seconds": 5,
  "worker_live_guard": true,
  "monitor_interval_seconds": 2,
  "monitor_rollup_seconds": 60,
  "monitor_uid": 1001,
  "tui_refresh_seconds": 1,
  "file_mode": "0660",
  "dir_mode": "2770"
}
```

```bash
sudo chown root:root /etc/gpubk/config.json
sudo chmod 0644 /etc/gpubk/config.json
```

当 `BK_DATA_DIR` 与 `BK_CONFIG_FILE` 都未设置时，GPUbk 会自动发现
`/etc/gpubk/config.json`。系统配置必须写明绝对 `data_dir`，因此普通 SSH、MCP
客户端和用户服务不依赖 shell 启动文件也会连接同一台账。非标准可信配置可通过
`BK_CONFIG_FILE` 选择；除非同时设置 `BK_DATA_DIR`，该文件也必须包含 `data_dir`。
显式设置 `BK_DATA_DIR` 会保留原有私有/数据目录内配置行为并跳过系统配置自动发现；
需要组合其他数据目录与外部配置时应同时设置两个变量。

请用 `id -u <monitor账号>` 的结果替换 `1001`。配置文件及其每一级目录必须由 root
或当前 UID 所有，并且不可被 group/other 写入。
即使文件自身是 root 所有的 `0644`，只要它位于 `/data2/shared/bk` 这种组可写目录中，
目录成员仍能通过 rename 替换它。GPUbk 会按文件描述符逐级固定并验证配置路径，拒绝
这种部署。显式选择 `BK_DATA_DIR` 时，单用户安装继续兼容
`$BK_DATA_DIR/config.json` 默认路径。

向组可写目录写入遥测的 monitor 会执行更严格的检查：必须使用可信且 root-owned 的
外部或系统配置，配置 `monitor_uid`，且进程 UID 必须完全一致。退出码 `77` 表示当前
进程不是指定写入者。实际执行遥测维护和迁移也要求同一角色，dry-run 仍可由普通用户
查看。单用户私有目录不要求配置该角色。

`max_shared_users` 为兼容旧配置保留，现表示每张 GPU 的 shared 容量单位数；
旧预约没有 `share_units` 字段时按 1 单位读取。

`slot_minutes` 控制预约开始时间和持续时间的粒度，默认值为 `5`；可选择 1 到 60
之间能够整除一小时的整数。单用户或测试环境可用 `BK_SLOT_MINUTES` 覆盖。多人服务器
应把它放在 root 管理的配置文件里：第一次写入会将该值绑定到台账，使用其他粒度的
客户端会被拒绝。

以下命令只读显示最终生效值，不会创建目录或修改台账：

```bash
bk config
bk config --json
```

环境变量可覆盖普通文件值，单次命令参数再覆盖对应默认值。安全角色 `monitor_uid`
只能来自配置文件，不能用环境变量替换。新配置应声明
`"config_version": 1`，旧的无版本配置仍可兼容读取。未知字段、错误类型、NaN/Infinity、
不安全路径和越界数值会直接报错，不再静默忽略。JSON 报告只列出当前生效的环境变量名，
不会输出外部分配器命令内容。

调度策略、保留周期、worker 时序、外部分配器和显示默认值可以配置；schema 版本、
事务持久性、路径与权限校验、记录大小上限等防损坏约束属于实现安全边界，不开放为
管理员调优项。

读取台账时会校验每条预约中参与准入判断的身份、GPU、模式、状态和有序时间戳。
新增的未知扩展字段会原样保留；已有语义字段出现未知值时则失败关闭，避免静默超售。
主台账语义损坏时，只有通过同一完整校验的历史备份才能被采用。

所有用户和用户服务必须解析到相同的数据与配置路径；标准
`/etc/gpubk/config.json` 布局会自动满足这一点。第一次写入会把调度与存储策略绑定到
台账，配置冲突的客户端会直接拒绝操作。启用服务前应从一个干净登录环境运行预检：

```bash
bk config
bk doctor --probe --strict
bk doctor --probe --json --strict
```

启用 monitor 后，再单独验证长驻写入者确实健康：

```bash
bk doctor --require-monitor --strict
bk doctor --require-monitor --json --strict
```

共享数据目录模式下会禁用 `bk reset`。需要退役或重建共享台账时，管理员必须先停止
所有 GPUbk 写入者并完成备份，再通过受控的文件系统流程处理。该命令仅保留给私有目录
和可丢弃的模拟数据。

预检会创建随机命名的临时文件，验证同目录原子替换与目录 fsync、同机跨进程
`flock`、配置权限、剩余空间和真实 GPU 探测，随后删除所有临时文件。探测到的 GPU
编号必须严格等于 `0..gpu_count-1`；每张 NVML 设备都必须返回有效显存、进程列表和
进程级利用率。拓扑不匹配或缺少进程列表会失败；缺少进程级利用率、模拟环境或
`nvidia-smi` 回退会在 strict 模式下作为警告失败。JSON 中的 `healthy` 只表示只读
台账检查，未运行 `--probe` 时 `ready` 保持为 `null`。
普通 `doctor` 不会初始化存储、加锁、恢复待处理事务，也不会跟随受管路径上的符号
链接或硬链接别名；权限漂移也只会报告给管理员，写命令不会静默执行 `chmod`。
只有显式指定 `--probe` 才会写入临时文件。
若 NFS/FUSE 被多台机器共同挂载，
仍需从第二台机器验证跨主机锁传播，因为单机测试无法证明这一点。所有写入者都必须
通过 GPUbk。

安全边界、文件保护、WAL 恢复、私有任务文件和 MCP 隔离说明见
[SECURITY.md](SECURITY.md)。

## 无 GPU 机器试用

指定模拟卡数即可体验预约和 TUI：

```bash
BK_DATA_DIR=/tmp/gpubk-demo BK_GPU_COUNT=4 BK_MAX_SHARED_USERS=4 bk t
BK_DATA_DIR=/tmp/gpubk-demo BK_GPU_COUNT=4 BK_MAX_SHARED_USERS=4 bk 1 30m --share 3/4
```

此时硬件指标显示为 unknown，但调度、shared 容量、时间轴、Add/Edit、日志和 Agent
JSON 均可正常使用。

## 开发

```bash
python3 -m pip install -e '.[mcp,gpu]'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python3 benchmarks/scheduler_queue.py
PYTHONPATH=src python3 benchmarks/usage_store.py
```

项目文档：[安全说明](SECURITY.md) · [升级说明](UPGRADING.md) · [发布流程](RELEASING.md) ·
[更新记录](CHANGELOG.md) · [Apache-2.0 许可证](LICENSE)
