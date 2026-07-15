# GPUBK

[English](README.md) | **简体中文**

GPUBK 是面向 Linux 共享服务器的 GPU 预约工具。PyPI 包名是 `gpubk`，
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

GPUBK 是协作式调度器，不代替 Linux 设备权限。拥有 `/dev/nvidia*` 直接访问权的
用户仍可绕过本工具，管理员需要另行配置设备访问策略。

## 安装

GPUBK 需要 Python 3.10 或更高版本。

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
bk tutorial
```

个人安装到这里已经完成，可以直接运行 `bk`。多人服务器只需要管理员再执行一次下面的
初始化；普通用户仍然只需使用 `bk`。

通过 PyPI 发布的 wheel 可直接使用系统自带 installer。若从 Git 源码目录或 sdist
安装，请先升级当前环境里的 pip：

```bash
python3 -m pip install --upgrade pip
python3 -m pip install .
```

部分旧版 Debian/Ubuntu pip 会忽略 `pyproject.toml` 请求的隔离 setuptools，并静默
生成不可用的 `UNKNOWN` 包。GPUBK 会识别这种情况并明确提示升级，不再假装安装成功。

## 第一次使用

教程是只读的：它会按当前服务器策略讲解命令，但不会创建、修改或删除任何预约。

```bash
bk tutorial          # 可随时重放的普通命令行教程
bk tutorial --tui    # 时间轴和按键的可视化教程
```

第一次进入普通 `bk` 提示符时只显示一行教程提醒；第一次运行 `bk t` 时会自动打开
TUI 教程。两个已读标记仅保存在当前用户的 `XDG_STATE_HOME`（通常是
`~/.local/state/bk`），不会写共享台账，也不会影响其他用户。之后仍可随时重放。

一次典型的新手体验是：

```bash
bk info              # 查看管理员账号和联系方式
bk login             # 只读查看正在进行和最近预约，不探测 GPU
bk g                 # 当前可用 GPU；没有生效预约时只读推荐一张
bk slots 1 30m       # 只读查看方案
bk 1 30m             # 自动预约最合适的 shared GPU
bk st                # 查看实时状态
bk l                 # 查看自己的预约
bk e 1               # 可恢复输入的引导编辑
bk d 1               # 用序号或短 ID 取消
bk t                 # 进入可视化时间轴
```

## 预约 GPU

默认使用 shared 模式：

```bash
bk 1 30m                         # 预约 1 张 GPU，持续 30 分钟
bk book 1 30m                    # 完全等价的显式命令形式
bk 2 15m 5g                     # 简写：每张卡预计使用 5 GiB 显存
bk 2 1h30m -m 12g               # 每张卡预计使用 12 GiB 显存
bk 1 1h -s 2                    # 每张卡申请 2 个整数 shared slot
bk s 1 2h -g 3                  # 显式 shared，固定 GPU 3
bk 1 1h -e 2,3                  # 自动调度，但排除 GPU 2、3
bk x 2 4h                        # exclusive 排他预约
bk 1 1h -t +30m                 # 30 分钟后，当地时间
bk 1 1h --at "tomorrow 09:00"  # 明天 09:00
bk 1 1h --start 2030-01-01T20:00:00+08:00  # 脚本使用的精确时间
```

使用 `bk COMMAND --help` 或 `bk help COMMAND` 查看对应帮助。帮助命令不会进入
引导表单、全屏 TUI 或 MCP stdio 服务。

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

底层预约 ID 是标准 UUID（36 个字符）。CLI 通常显示前 8 位；空间紧张的 TUI 从
可区分的前 6 位开始，发生碰撞时自动增加位数。6 位和 8 位都只是同一 ID 的唯一前缀，
只要当前台账中不歧义，都可以用于 `bk e`、`bk d`、`bk run` 等命令；歧义时程序会拒绝，
不会猜测目标。

调度规则保持简单：

- 开始时间和持续时间使用服务器配置的预约边界。
- 不传 `--at` 或 `--start` 时优先使用当前预约时间片（默认配置下 `12:41` 从 `12:40` 开始）；
  若必须延后才会显示 `queued:`。
- `--at` 支持 `+30m`、`20:00`、`tomorrow 09:00` 和 `07-13 20:00`；
  `--start` 为脚本和 Agent 保留精确 ISO 8601。两者都不会在冲突时自动挪动。
- 精确新建可以选择当前时间片起点或未来边界，不能写入更早的历史时间片；已经成功
  应用的 operation ID 在任务开始后重试时仍返回原预约。
- 每张 GPU 有 `max_shared_users` 个整数 shared slot。shared 默认申请 1 个；
  `--share 3` 表示在每张选中的 GPU 上申请 3 个 slot。每个重叠时间片都会独立校验。
- slot 用于预约准入和默认显存估算，不会自动限制 GPU 的 SM 算力；
  需要物理隔离时仍应配置 MIG、MPS 或设备权限。
- exclusive 不能与任何预约重叠。
- 位置参数 `5g` 与 `--mem 5g` 都表示**每张 GPU**预计使用的显存。省略时按 shared slot
  自动估算；管理员仍可要求所有 shared 预约显式填写。
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
bk tl --from "2026-07-12T09:00:00+08:00" --window 4h
bk slots 2 1h --mem 12g            # 只读查找多个可预约方案
bk slots x 1 30m --limit 3
```

时间轴使用固定宽度单元：`··` 表示空闲；`M1`-`M9` 表示该时间片的 shared
总占用，并且其中包含自己的预约；`S1`-`S9` 表示仅由他人占用的 shared 总容量；
`MX`/`XX` 表示自己的/他人的 exclusive。窄终端会按整小时分块换行，
不会偷偷降低指定粒度。`--from` 可以指定过去时间，历史视图只读，会显示保留期内
已过期的预约，但不会显示已取消的预约。

当前 UID 存在待领取、已领取或运行中的预约脚本时，`bk st` 还会显示由内核锁和当前
实例绑定共同证明的私有 worker 状态；脚本不能启动时会明确告警。普通预约和已经终结
的任务不会触发这次私有目录探测。

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
bk tutorial --tui
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
| `1`-`9` | 选择 GPU 数量，并从搜索起点向后查找最早合法时段 |
| `s`、`x` | 在 Add/Edit 中切换 shared/exclusive |
| `u` | 设置每张 GPU 要申请的整数 shared slot 数 |
| `f` | 从 `NOW` 或手动指定的开始时间向后查找最早可用卡 |
| `o` | 查找当前时间光标附近最近的合法时段 |
| `g` | 固定当前已选卡，向后查找最早合法时段 |
| `n` | 回到带 `NOW` 标记的实时窗口 |
| `c` | 切换深色/浅色主题 |
| `z` | 切换容量切片与优先实心的 shared 时间条 |
| `?` | 打开分页帮助和快速上手说明 |
| `Enter`、`Esc`、`q` | 提交、取消当前操作或退出 |

TUI 默认每秒刷新一次。较慢终端或希望降低轮询频率时，可在配置中设置
`tui_refresh_seconds`，或用 `BK_TUI_REFRESH_SECONDS` 覆盖当前环境。
头部直接写出共享遥测采集器与当前 UID 的预约脚本 worker 状态；`worker=idle`
表示当前没有任务需要自动执行。只有存在可运行脚本时才会只读检查 worker，并限制为
最多每 10 秒一次；按 `r` 会立即清空 monitor 和 worker 状态缓存并重新检查。

时间轴可以查看过去的预约，但历史只读。Add 和 Edit 提交时，调度器会在文件锁事务内
再次校验所选时段。预约焦点默认停在标题栏，按下方向键后才选中并闪烁某条预约。
不超过 10 张 GPU 时，预约表的 `GPU` 列为每张卡保留固定位置，只在预约使用的
位置显示对应数字，未使用位置显示与时间轴一致的灰色点。
预约 ID 默认统一显示可区分的最短 6 位前缀；发生前缀碰撞时自动增加位数。

## 到点运行命令

把命令写在 `--` 后：

```bash
bk run -- python train.py              # 使用当前生效预约，立即运行
bk run 1 30m -- python train.py        # 预约最早 GPU，现在或到点运行
bk run 1 30m --exclude 2,3 -- COMMAND  # 自动选择时排除指定 GPU
bk 2 1h30m --mem 12g -- python train.py --config exp.yaml
bk j                    # 查看任务
bk j --cleanup          # 检查并清理私有任务文件
bk w                    # 只读检查当前用户的 worker 是否在线
bk w start              # 前台运行当前用户的定时命令 worker
bk w once               # 只处理一次到期任务后退出
bk jr ID --accept-duplicate-risk  # 检查 uncertain 任务后再确认重试
```

只输入 `bk run` 不会写台账：它会显示当前属于你的 GPU；若当前没有生效预约，则只读给出
最早的 1-GPU 建议。即时命令使用稳定 GPU UUID（可用时）设置
`CUDA_VISIBLE_DEVICES`，并在所选预约中最早的结束时间停止整个命令进程组。
裸 `bk w` 特意设计成只读状态检查，不会意外启动常驻进程。前台运行使用 `bk w start`，
单次处理使用 `bk w once`，systemd 等服务使用完整命令 `bk worker`。

worker 会设置 `CUDA_VISIBLE_DEVICES`、`CUDA_DEVICE_ORDER`、
`BK_RESERVATION_ID` 和 `BK_RESERVED_GPUS`。默认 live guard 会把同一轮 NVML 启动校验
通过的稳定 GPU UUID 写入 `CUDA_VISIBLE_DEVICES`，而 `BK_RESERVED_GPUS` 继续保留用户
看到的数字卡位，从而不假设 NVML 编号与 CUDA ordinal 一致。真实 NVML 设备若缺少合法
稳定标识，任务会继续等待而不会猜一张卡启动；数字启动标识只保留给模拟环境和显式接受
风险的 `worker_live_guard=false` 兼容路径。命令、工作目录和提交进程当时的 `PATH`
保存在当前 UID 所有的 `0600` 私有文件里，并共同参与摘要签名，不会写入共享台账。
因此 systemd worker 即使不在原交互 shell 中启动，`python` 这类裸命令仍按预约时的路径
查找。GPUBK 不会自动复制其他环境变量；项目变量和凭据应由用户私有 wrapper 或配置文件
加载。同一 operation ID 若换了 `PATH` 再提交会被视为不同命令并拒绝；旧 v1 私有 spec
仍可读取。worker 使用 `shell=False`，并持续监管命令所在的进程组，直到它退出或预约
结束；任务脚本不应自行 daemonize 或创建新 session。
真实 GPU 主机上的受保护定时任务需要安装 `gpu` extra；`nvidia-smi` 回退没有可信的
进程列表，因此任务会保持等待，而不会猜测设备为空闲。
确实需要 shell 语法时应明确调用 shell：

```bash
bk 1 30m -- sh -lc 'python train.py > train.log 2>&1'
```

为避免相邻预约重叠，worker 会在预约结束前发送 TERM；到达结束时间后进程组仍存活则
立即发送 KILL。`worker_termination_grace_seconds` 控制提前通知窗口（默认 5 秒，允许
0.1 到 60 秒）。取消预约或停止 worker 时，则从事件发生时开始计算同样的宽限。任务应
处理 TERM 以保存 checkpoint；这段宽限位于已预约时段内，不会额外占用下一位用户时间。

任务启动前，worker 会再次采样所有分配到的 GPU。exclusive 任务会等待所有非系统
进程退出；shared 任务允许已有的合法共享者，但遇到未预约/身份未知进程或物理显存
不足时会等待。实时探测不可用时也默认拒绝启动。任务保持 `pending` 并显示原因，常驻
worker 会在预约窗口内持续重试；`bk worker --once` 有等待任务时返回状态码 `3`。
只有明确接受兼容性风险时才应设置 `worker_live_guard=false`。

worker 可以并发启动多条到期命令，包括同一 GPU 上合法的 shared 预约。实际并发上限
取 `worker_max_parallel`（默认 64）与 `gpu_count * max_shared_users` 的较小值，既不会再
按物理 GPU 张数错误地串行化 shared 任务，也保留管理员控制的进程安全上限。
`bk worker --max-parallel N` 可覆盖单次运行；`bk config` 会同时显示配置值和实际值。

同一个 UID 的私有任务目录同时只允许一个 worker 持有租约；worker 崩溃后内核会自动
释放锁，因此新 worker 可以恢复持久化的 `claimed` / `running` 状态而不会与健康 worker
竞争。Linux 上只检查同 UID 的 `/proc` 记录，精确匹配 `BK_RESERVATION_ID`，并在发送
信号前再次核对身份；先发 TERM，超过 `worker_recovery_grace_seconds`（默认 5 秒）后
再发 KILL。即使残留进程组已停止，任务仍保持 `uncertain`，因为崩溃前可能已产生部分
副作用，重试时必须显式接受重复执行风险。其他主机上的进程绝不会在本机被发送信号。
并发启动第二个 worker 会返回状态码 `75`；内置 systemd unit 不会因此反复重启。
升级时，旧版无租约 worker 创建的活动任务不会被接管，并会暂停领取新任务，直到旧任务
结束或预约到期。
如果当前 worker 在监督任务时失去台账访问能力，最终的进程组 KILL 和回收不再依赖
另一次成功的台账读取。未更新的持久化任务状态会在重启后诚实地恢复为 `uncertain`，
不会被误报为完成，也不会自动重复执行。
worker 会在启动、每轮轮询及每个持锁事务中校验台账绑定策略。启动时不一致会在创建
私有租约前以状态码 `78` 退出；运行中发生策略漂移时，会停止并回收自己监管的脚本，
但不会再用错误策略对账或清理共享状态。

`bk worker --status` 不创建或修改私有存储，并报告 `running`、`stopped`、`not-seen`、
`other-instance`、`unverified` 或不安全/不可用状态。只有全局内核锁和以当前数据目录摘要
命名的实例锁都被持有，才能证明 `running`；文件里的 PID、主机名、获取时间和摘要文本
仅供诊断。加 `--json` 可得到 `gpubk.worker.v1`，加 `--require-running` 则在当前实例的
租约未被持有时返回状态码 2。`bk jobs --json` 与 Agent/MCP 上下文也会暴露同一份当前
UID 状态。
新增或编辑带脚本的预约时也会立即检查该租约；未证明 worker 在线时，普通 CLI 会明确
告警，JSON/MCP 的 `booking_result.worker` 则返回同一份 `gpubk.worker.v1`（不带脚本的
预约为 `null`）。预约创建成功本身不代表脚本能够无人值守启动。

预约取消、任务成功、超时或可重试窗口结束后，私有命令 spec 会被清理。worker 会在
启动、退出以及持续运行时最多每 5 分钟检查一次。没有台账引用的规范 spec 会保留
24 小时宽限期，避免与正在提交的预约发生竞争；仍待执行、运行中或可重试的任务不会
被删除。spec 的创建、执行前读取和删除都固定在经过校验、归当前 UID 所有的私有目录
描述符上，拒绝符号链接和硬链接别名；写入被中断时会清除半写文件。如果预约是否完成
存在歧义，GPUBK 会先恢复并重读最新台账，只删除未被任何已提交预约引用的 spec。
稳定 operation ID 还会绑定完整命令摘要和工作目录，但命令参数不会进入共享台账。
`bk jobs --cleanup --json` 提供同一清理流程的机器可读结果。私有任务日志会
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
bk doctor --require-worker --strict
```

在 systemd Linux 上，用户退出后 user manager 可能停止，开机时也不一定自动启动。
确需无人值守任务时，由管理员按用户选择性开启 linger：

```bash
sudo loginctl enable-linger <worker用户>
```

生成的 unit 会固化安装时生效的绝对 `BK_DATA_DIR`、私有 `BK_JOB_LOG_DIR`、显式
`BK_CONFIG_FILE`，以及 `BK_WORKER_MAX_PARALLEL` 等明确启用的非敏感配置覆盖。写入
unit 前会先校验并规范化数值，allocator 命令绝不会被固化。可用
`bk service show worker` 检查；任一路径或覆盖项变化后使用 `--force` 重新安装。
同一 UID 的所有 worker 必须使用同一个私有目录，使租约只有一个权威位置。服务于其他
`BK_DATA_DIR` 的 worker 会显示为 `other-instance`，不会冒充当前账本已就绪。没有被
固化覆盖的策略会在每次服务启动时从选定配置文件重新读取；共享部署应优先使用可信
配置文件，而不是依赖 shell 环境变量。
worker 持久启动失败在 60 秒内最多重试 3 次；普通子任务失败只写入任务状态，不会让
长驻 worker 退出。

## 监测与自动选卡

安装 `gpu` extra 后，可以采样一次或持续低开销监测：

```bash
bk m --once
bk m
bk g                              # 当前属于你的 GPU，否则推荐一张
bk u                              # 当前 UID 最近 24 小时
bk u users --since 30d           # 可见用户汇总
bk u samples --since 2d --resolution 5m --json
bk u events --user me --since 7d
```

`bk g` 是回答“我现在该用哪张卡”的最短只读命令。有生效预约时，它只显示属于你的 GPU、
剩余时间、实时利用率和可用显存；没有生效预约时，它结合台账和实时监测推荐一个合法的
30 分钟 shared 方案。它不会创建预约，需要时执行输出的 `bk 1 30m --gpu N` 即可。

`bk u` 只统计 monitor 已采样到的过去数据，未来预约不会进入任何统计。`Reserved` 是已有
采样覆盖的历史预约时长，`Idle` 是其中没有检测到该用户 GPU 进程的部分。个人默认视图还
是彩色终端仪表盘，并画最近 7 天逐日图和最近 4 周逐周图；不看趋势可加
`--no-chart`。TUI 普通模式按 `u` 可直接打开同一份个人总结，不必退出时间轴。

在真实 GPU 主机上激活带 CUDA PyTorch 的环境后运行 `bk usage demo`。它会先检查
monitor，确认后预约一张当前空闲卡，运行短时低占用负载，打印统计结果，并始终尝试取消
测试预约。已获准的非交互验收可使用 `bk usage demo --yes`。

NVML 只初始化一次，并复用设备句柄。初始化失败或设备句柄失效后会先短暂退避再重建，
因此驱动的瞬时故障不会让长驻 monitor 永久降级。监测器保存有限长度的调度负载、稀疏的
用户历史，以及进程开始、结束、授权和工作负载变化事件，不会每秒写入一份完整快照。
没有 NVML 时会回退到 `nvidia-smi` 获取设备指标；由于该回退没有可信的进程列表，
GPUBK 会保留最后一次已观测进程状态并报告遥测缺口，不会伪造 stop/start 事件。
monitor 警告与 Agent 的 GPU 详情会暴露进程列表和进程级利用率能力；collector 状态会
独立报告稳定 CUDA 设备标识与数字 UID 归属缺口。
monitor 还会原子更新一个很小的 `usage/collector.json` 心跳。Usage JSON、Agent
上下文、`bk doctor` 与 TUI 顶栏读取同一组 `running`、`degraded`、`stale`、
`stopped`、拓扑不匹配状态。异常退出漏过三次心跳后变为 `stale`，正常退出显示
`stopped`；TUI 中 `monitor=ok` 表示采集器新鲜，`monitor=not-seen` 表示尚无心跳记录。
缺少稳定设备标识，或当前观测到的 GPU 进程无法解析出数字 UID 时，collector 会进入
`degraded`，因此受保护定时任务仍无法启动时，启动后的 doctor 验收也不会误报成功。
若主机使用 `hidepid` 或容器化 `/proc`，只把管理员批准的进程可见组授予 monitor 账号；
不要为了消除该缺口而直接让采集器以 root 运行。
默认每 2 秒采样，并折叠为 60 秒记录。管理员可在可信配置中调整
`monitor_interval_seconds` 与 `monitor_rollup_seconds`；聚合窗口必须是采样间隔的
整数倍。命令行 `--interval`、`--rollup` 只覆盖本次运行。

进程状态根据进程 UID 和有效预约判断，包括 `ok`、`wrong-gpu`、`unreserved`、
`unknown` 和 `system`。命令行写入共享日志前会缩减为安全标签。

历史数据按天分区并带校验和，提供 1 分钟、5 分钟、10 分钟、小时和每日层级。
Python、JSON CLI 和 MCP 统一返回 `gpubk.usage.v1` 公共模型；可视化程序不应
直接解析内部文件。完整说明见 [Telemetry](TELEMETRY.md)。
`usage_load_window_minutes` 同时控制自动选卡保留并纳入判断的近期设备历史长度。

新写入的历史还会带版本化 `gpubk.node` 扩展，其中包含经过哈希的稳定节点 ID、主机名，
以及可用时的稳定 GPU UUID；不会保存原始 machine-id。旧记录继续作为 `legacy` 节点读取。
因此以后导出、汇总或迁移时，两台机器各自的 GPU 0 不会被误认为同一设备。用户汇总已经
返回 `nodes` 和 `(node_id, gpu)` 设备对，capabilities 也会明确说明当前拓扑能力。

每份 GPUBK 台账仍只管理一台主机。不要让多台机器上的独立 broker/monitor 共写同一个
NFS 目录。可选集群联邦通过非交互 SSH 和版本化 Agent JSON 查询各主机，并把预约提交给
唯一一台目标机器的 broker。管理员可以把稳定的 `(node_id, 数字 UID)` 映射成统一人员；
不能只凭用户名合并，已有历史数据无需重写。

没有集群目录时，所有节点入口都会隐藏。配置后，`bk c` 查看所有节点和活动预约，
`bk c rec 2 1h` 比较合法开始时间，`bk c 2 1h` 在最佳单节点预约，`bk c x 2 1h`
进行排他预约，`bk c tui` 打开节点及预约浏览器，`bk @NODE 2 1h` 则明确指定机器。
浏览器中用 `Tab` 切换焦点、`Enter` 查看预约完整详情。滚动升级期间旧节点仍可
查看，但在声明安全写入所需能力前保持只读。传输、故障、NFS 导出及上线边界见
[CLUSTER.md](CLUSTER.md)。

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
生成的 unit 会固化共享数据目录、显式可信配置路径，以及安装时启用的非敏感配置覆盖；
未被固化的采样与聚合周期会在服务每次启动时从该配置重新读取。重复写入者（`75`）或
角色不匹配（`77`）、台账策略不匹配（`78`）不会重启；其他失败在 60 秒
内最多重试 3 次，既允许短暂故障自愈，也不会无限刷日志。
上面的最后一条命令是只读的启动后验收；与部署预检不同，如果从未记录过 collector
心跳，它会明确失败。
正常的信号退出会发布 `stopped`。致命的采样或存储错误只会尽力冲刷部分聚合，不会
伪造正常停止状态；最后一份 `running` / `degraded` 心跳会自然变为 `stale`，原始非零
错误仍交给 systemd 处理，同时单写锁一定释放。
monitor 每轮都会在遥测维护和 GPU 采样前校验策略。策略漂移与普通采样错误不同：尚未
落盘的聚合会被丢弃，不做崩溃冲刷、不发布正常停止状态，但一定释放单写锁，等待管理员
修复配置。

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
对于已经提交的完全相同重试，GPUBK 会在实时 GPU 探测、外部 allocator 和私有命令
spec 写入之前直接确认。JSON 中会显示 `allocator.source=idempotent-replay`；调用方没有
预先提供 advice 时，重放专用的实时字段明确为 `unknown`，不会伪装成最新数据。它只证明
预约已经提交，不证明旧工作目录或 worker 此刻仍可执行。Agent context 通过
`capabilities.preflight_idempotent_replay` 暴露这项能力。

启动可选的 stdio MCP 服务：

```bash
bk-mcp                       # 等同于 bk mcp
bk skill install            # 安装 wheel 内置的 Codex Skill
```

`CODEX_HOME` 为绝对路径时，默认安装到 `$CODEX_HOME/skills/gpubk`；否则使用
`$HOME/.codex/skills/gpubk`。`--force` 会拒绝符号链接和当前工作目录所在的目录树；
暂存替换失败时会恢复原有 Skill。

MCP 提供 context、recommend、create、list、edit、cancel、私有 spec 清理和私有
任务日志工具。它只使用 stdio，不监听网络端口；每位用户运行自己的 MCP 进程。工具
schema 标明 read-only、idempotent、destructive 和 closed-world 属性。

管理员还可以通过 `BK_ALLOCATOR_COMMAND` 配置受信任的本地程序。它读取
`bk.allocator.v1` JSON，并返回 GPU 排序。外部结果只提供建议，最终仍须通过内置的
冲突、显存、时间、UID 和事务校验。create、recommend、edit 都会先核对台账绑定策略，
再调用 allocator；超时、输出无效或普通错误会回退到内置排序，收到中断时则先终止
allocator 进程组再继续抛出中断。完整格式见
[Agent 协议](src/bk/data/codex-skill/gpubk/references/protocol.md)。

## 多人服务器配置

多人服务器建议把 GPUBK 放进独立的系统虚拟环境。这样不改系统 Python，升级时也始终使用
同一个路径：

```bash
sudo python3 -m venv /opt/gpubk
sudo /opt/gpubk/bin/python -m pip install --upgrade pip
sudo /opt/gpubk/bin/python -m pip install 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install
bk doctor --probe --require-monitor --strict
```

安装向导每次只问一个设置，所有问题都有稳妥默认值，直接回车即可接受；最后会统一预览再
确认。它会初始化可信配置、受跟踪地创建 `/usr/local/bin/bk`、安装 broker/monitor
unit、可选安装彩色登录提醒，并设置两个服务开机启动。`--dry-run` 只预览，`--yes`
使用无人值守默认值，`--no-start` 只安装不启动；若全局命令由别的包管理器负责，可用
`--no-command-link`。pip 本身不会调用 sudo，也不会修改 `/etc`；系统级改动只由这条
明确的管理员命令执行。

在已有受管部署上重复执行同一条命令也是安全的，但语义会自动变成“校准”：保留带校验
摘要的配置和全部数据，拒绝会改变策略的参数，只校准已跟踪的命令链接和 unit，并受控
重启服务。若停止服务后的步骤失败，安装器会尽力重新启动原有受管服务，避免留下无意停机。

排障或高级部署仍可使用底层分步流程；此时需自行确保用户执行的路径上已有 `bk`：

```bash
sudo bk admin init --yes
sudo bk admin services install --yes
sudo systemctl daemon-reload
sudo systemctl enable --now gpubk-broker.service gpubk-monitor.service
```

安装器绝不使用强制覆盖链接。`/usr/local/bin/bk` 不存在时，它会创建并记录指向
`/opt/gpubk/bin/bk` 的绝对软链接；安装前若已存在完全相同的链接，则记录为原有链接，
卸载时保留。普通文件或指向其他目标的链接会让安装在修改服务器状态前停止。此时先检查：

```bash
ls -l /usr/local/bin/bk
readlink -f /usr/local/bin/bk
```

如果第二条输出 `/opt/gpubk/bin/bk`，直接重新运行安装器；它会采用但不接管这个原有
链接：

```bash
sudo /opt/gpubk/bin/bk admin install
```

否则应明确处理冲突，或使用 `--no-command-link`。不要对未知路径使用 `ln -sf`。

Debian/Ubuntu 若没有 `venv`，先安装 `python3-venv`。初始化会探测真实 GPU，并默认把
发起 `sudo` 的管理员账号作为 broker 和 monitor 的运行账号。它创建的就是正式部署路径：
`/etc/gpubk`、`/var/lib/gpubk` 和 `/run/gpubk`，不会新建账号或用户组。受跟踪的
systemd unit 会在开机时启动，进程仍以该非 root UID 运行，可写路径只开放给台账和
socket 目录。

默认所有本机账号都可通过 Unix socket 使用 `bk`，但只有选定的运行账号能写台账文件。
普通用户不能修改其他 UID 的预约或系统策略，也不需要 `sudo`。直接使用管理员自己的账号
是完整支持的方案；专用账号只是可选的运维选择，并非安全边界的必要条件。

选定的 broker 运行账号同时也是用户可见的 GPUBK 管理员。`bk info` 会显示该 Linux
账号、数字 UID，以及 `adduser`/GECOS 中填写的 Full Name、Room、Work Phone、Home
Phone 和 Other；`bk info --json` 会把同一份带版本结构的数据提供给工具和 Agent。
TUI 中按 `i` 即可查看。管理员可以用 `sudo chfn 用户名` 更新这些本机账号字段；修改后
立即生效，执行 `sudo bk admin transfer` 后也会自动跟随新账号，不需要重写 GPUBK 数据。
这些字段会展示给所有本机 GPUBK 用户，因此只应填写适合公开的联系方式。

若要把多台正常工作的 GPUBK 主机组成联邦，在用户执行集群命令的机器上初始化目录。
先从每台远端的 `bk agent context --compact` 读取稳定节点 ID，并确认普通用户可通过已验证
主机密钥的非交互 SSH 登录，再添加节点：

```bash
sudo bk admin cluster init gpu-a --yes
ssh -T gpu-b /usr/local/bin/bk agent context --compact
sudo bk admin cluster add gpu-b gpu-b NODE_ID_FROM_ABOVE --yes
sudo bk admin cluster map lab-user gpu-a 1003 --yes
sudo bk admin cluster map lab-user gpu-b 2042 --yes
# 映射有误时撤销：sudo bk admin cluster unmap gpu-b 2042 --yes
sudo bk admin cluster status
bk c
bk c check
bk c rec 1 30m
bk c 1 30m -j
bk c x 1 30m          # 排他模式，选择最早节点
```

目录尚未创建时也可以使用 `bk cluster -h` 及各子命令的 `-h`。自动化重试完全相同的
集群预约、修改或取消请求时，应复用同一个 `--op-id`。每个用户都应至少运行一次
`bk c check`：它会检查该用户自己的 SSH 身份能否访问每个启用节点，并校验稳定节点
身份、时钟以及远端版本是否支持可安全重试的写操作。

节点维护时不需要删除端点、UID 映射或历史归档。停用节点不会被访问，也不会参与选址：

```bash
sudo bk admin cluster disable gpu-b --yes
sudo bk admin cluster enable gpu-b --yes
```

面向人的集群表格使用执行命令一侧的本地时区；带 `--json` 的结构化输出仍统一使用 UTC。

正式启用共享目录前，可以先从本仓库执行一次端到端候选版本验收：

```bash
python3 tools/cluster_acceptance.py user@gpu-a user@gpu-b
```

它会把当前代码构建成 wheel，在每个 SSH 账号的私有临时缓存中安装同一个 wheel，并为每台
主机使用一张模拟 GPU 和独立测试台账；随后依次验证集群状态、推荐、两节点独立落单、相同
操作 ID 重试和取消，最后自动删除远端临时目录。它要求主机密钥已确认且密钥 SSH 可非交互
登录，不需要 `sudo`，不会连接正式 broker、读取 NVML 或改写 `/var/lib/gpubk`。已有 wheel
可用 `--wheel DIST.whl` 指定。私有 JSON 报告以 `0600` 写入 `acceptance-reports/`。

这个测试覆盖的是安装包和跨机传输，不代替最后的真实验收：每台 GPU 主机仍要分别执行普通
单机验收，再检查第二用户权限、一次获准的真实 GPU 工作负载，以及服务重启和整机重启。

root 管理的目录只保存端点、稳定节点 ID、优先级和可选身份映射，不保存 SSH 私钥。每个
用户仍由目标机器的 SSH 独立认证，并以远端真实数字 UID 操作。`bk c` 还会显示每个可达
节点的 GPUBK 版本，滚动升级时可以直接看出混合版本。节点优先级只在最早开始
时间相同时打破平局；所有写入都由远端 broker 最终校验，一条预约不会跨机器拆分。本机
CLI 与 broker 仍通过 Unix socket 通信，跨主机只使用出站 SSH。不要让多个实时台账共写
同一个 NFS 目录。

若需要跨机器长期查看历史，可以额外配置 NFS 只读归档；它不会把各主机的实时 writer
合并。每台机器只导出已经结束的 UTC 日期：

```bash
sudo install -d -m 0755 /srv/gpubk-cluster-history
sudo bk admin cluster history-root /srv/gpubk-cluster-history --yes
sudo bk admin cluster export-history --since 1095d --resolution 10m --yes
sudo bk admin cluster verify-history
bk c history --since 30d
```

后续导出会从最新结束位置增量继续。每个节点只写自己的稳定 ID 命名空间；每一代数据都
经过 gzip、SHA-256、fsync 和原子发布，发布后按不可修改方式保存。归档只包含带版本的
公开用户统计与样本，不包含预约台账、命令参数、任务 spec、密钥或日志；即使 NFS 离线，
预约也完全不受影响。`root_squash`、多节点目录权限和日常操作见 [CLUSTER.md](CLUSTER.md)。

常用的非交互形式：

```bash
sudo bk admin init --dry-run
sudo bk admin init --yes                         # 默认使用发起 sudo 的用户
sudo bk admin init --yes --service-user "$USER" # 显式指定同一个用户
sudo bk admin init --yes --service-user gpubk --data-dir /data2/shared/gpubk
sudo bk admin init --yes --service-user gpubk --access group --group gpuusers
sudo bk admin init --yes --disabled-gpus 7 --gpu-priority 6=10
```

用户组模式只是可选地限制谁能连接 socket；初始化命令不会创建账号、用户组或修改成员。
台账文件固定为 `0644`、目录为 `0755`，均归服务账号所有：所有用户可以查看排期，但只有
broker 能修改。broker 从内核提供的对端凭据识别 UID，不信任客户端传来的用户名或 UID。

管理员无需手改可信 JSON，就能禁止有故障的 GPU 接受新预约，或降低其调度优先级。
优先级数字越大越晚选，但它只在“最早开始时间相同”时打破平局，不会为了选好卡而让用户
多等。被禁用 GPU 的监控和历史仍保留。更新前停止两个写进程，先预览，再应用并重启：

```bash
sudo systemctl stop gpubk-broker.service gpubk-monitor.service
sudo bk admin gpu-policy --disabled-gpus 7 --gpu-priority 6=10 --dry-run
sudo bk admin gpu-policy --disabled-gpus 7 --gpu-priority 6=10 --yes
sudo systemctl start gpubk-broker.service gpubk-monitor.service
```

同一个原子策略命令也可以允许用户省略共享显存预期：

```bash
sudo bk admin gpu-policy --allow-implicit-shared-memory --dry-run
sudo bk admin gpu-policy --allow-implicit-shared-memory --yes
```

省略值会保留为 `auto`，不会写成 0；0 会错误表示任务完全不占显存。需要恢复严格策略时
使用 `--require-shared-memory`。

`--enable-all` 和 `--clear-priority` 可分别清空两项策略。若更新中断，不要删除
`/etc/gpubk/config-update.json`；先运行
`sudo bk admin gpu-policy --recover --dry-run`，确认后再加 `--yes`。恢复完成前普通启动会
安全拒绝运行，避免新旧可信文件混用。

若想先做可回滚的前台试运行，再启用 systemd 服务，可以用选定的运行账号在第二个终端
启动 broker，不要加 `sudo`：

```bash
bk broker --check
bk broker
```

这不是模拟模式：它使用真实的 root 配置、服务账号台账、Unix socket、文件锁、GPU 探测和
用户 UID，与 systemd 正式部署完全相同；区别只有进程暂时由前台终端看管。在 GPU
服务器上不要传
`--gpu-count`，除非确实要模拟 GPU 拓扑。

然后用普通用户体验：

```bash
bk info                                  # 找到负责维护的管理员
bk config                                # storage transport 应显示 broker
bk doctor --probe --strict               # 校验 socket 身份与连通性
bk 1 30m
bk l
bk t
```

### 一条命令完成远端实机验收

在可信的本地仓库中执行下面一条命令，即可自动下载指定的 PyPI 正式文件、逐个核对
PyPI SHA-256、通过 SSH 上传私有测试包、在真实 GPU 拓扑上运行隔离调度测试、检查已
部署服务，并把经过 SHA-256 校验的报告下载回本机：

```bash
python3 tools/remote_acceptance.py USER@GPU-HOST \
  --remote-python /opt/gpubk/bin/python \
  --system-bk /usr/local/bin/bk \
  --sudo
```

脚本优先在本机下载；如果本机无法访问 PyPI，会自动改由 GPU 服务器下载并校验相同文件。
`--sudo` 只会在远端要求输入一次密码，用于只读检查服务
账号、文件所有权和 systemd；脚本不会重启服务、写正式台账或启动 GPU 任务。候选版本
的调度测试只使用 `~/.cache/gpubk/acceptance/` 下的私有临时目录，报告取回后自动清理。

报告保存在本地 `acceptance-reports/`，包含 JSON 结果、文字摘要、上传清单、原始压缩包
和校验值。即使自动检查失败，脚本仍会尽量下载报告并返回非零状态。只有排错时才使用
`--keep-remote`；`--include-journal` 会额外收集两个 GPUBK unit 最近 80 行日志，必须
显式开启。TUI 观感、第二个真实用户的越权测试、维护窗口内的小型 GPU 任务和重启后
自启动仍需人工确认。

需要把运行职责交给另一个已有本机账号时，先停止 broker 和 monitor，再预览、执行、
重新加载并启动受跟踪的 unit：

```bash
sudo systemctl stop gpubk-broker.service gpubk-monitor.service
sudo bk admin transfer NEWUSER --dry-run
sudo bk admin transfer NEWUSER --yes
sudo systemctl daemon-reload
sudo systemctl start gpubk-broker.service gpubk-monitor.service
```

该命令会占住 broker socket、monitor 锁和 ledger 锁，在原目录内变更所有权，并且只更新
`broker_uid` 与 `monitor_uid`。预约 UID、预约记录、审计日志、使用率历史和调度策略都不
重写。受跟踪 systemd unit 中的数字 UID/GID 会在同一事务内更新。root-only 恢复日志会
保护整个过程；若交接中途断电，运行 `sudo bk admin transfer --recover --yes` 回到原
账号，然后执行 `systemctl daemon-reload` 并重新启动 unit。

升级软件包不会改写配置或数据。停止 broker 和 monitor，升级同一个独立环境，校准受管
部署并检查：

```bash
sudo systemctl stop gpubk-broker.service gpubk-monitor.service
sudo /opt/gpubk/bin/python -m pip install --upgrade 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install --yes
bk --version
bk broker --check
bk doctor --probe --require-monitor --strict
```

服务重启、版本回滚和跨版本注意事项见 [UPGRADING.md](UPGRADING.md)。

### 备份、清空与恢复

管理员数据命令覆盖完整受管数据树：预约、操作日志、台账内部快照、用量事件、聚合记录和
采集器状态；它不会用旧备份悄悄覆盖当前可信系统配置。操作前先停掉两个写进程：

```bash
sudo systemctl stop gpubk-broker.service gpubk-monitor.service

sudo bk admin data backup /var/backups/gpubk/pre-change
sudo bk admin data verify /var/backups/gpubk/pre-change

# 破坏性操作：必须先成功创建并校验 pre-clear，之后才会清空。
sudo bk admin data clear --backup-to /var/backups/gpubk/pre-clear --yes

# 只有受管数据目录为空时才允许恢复。
sudo bk admin data restore /var/backups/gpubk/pre-change --yes

sudo systemctl start gpubk-broker.service gpubk-monitor.service
bk doctor --probe --require-monitor --strict
```

`backup` 不写路径或 `clear` 不写 `--backup-to` 时，默认使用
`/var/backups/gpubk` 下带 UTC 时间戳的目录。每份快照都是私有目录，包含版本化清单、仅供
核对的配置副本，以及每个文件的大小和 SHA-256。创建、清空和恢复都使用同文件系统原子
目录替换和 fsync；符号链接、硬链接、特殊文件、额外备份内容、所有权漂移、摘要不一致及
并发写入都会被拒绝。文件按固定大小分块复制，多年历史也不需要整体装入内存。

`bk reset --yes` 仍只用于个人/测试数据，共享服务器存储会直接禁用它。若保留安装但从空
历史重新开始，使用 `bk admin data clear`；若要移除整个受跟踪部署，使用
`bk admin uninstall --purge-data`。

管理员可以选择在用户交互式登录时显示一条很短的预约提醒：

```bash
sudo bk admin login-hook install --yes
sudo bk admin login-hook status
```

该钩子每次登录最多运行一次，并且仅在标准输出是终端时通过一秒 `timeout` 执行
`bk login --hook`。它只读已提交台账，不申请写锁，也不探测 NVML。用户没有正在进行或
未来 24 小时内的预约时完全无输出。终端支持颜色时会区分当前和即将开始的预约；若新鲜
且可信的 monitor 仍看到当前 UID 在预约过期后占用 GPU，则显示红色告警。所有错误都会
被抑制，因此不会阻塞 SSH 登录。
`sudo bk admin login-hook uninstall --yes` 只删除带 GPUBK 标记的受管文件；完整执行
`sudo bk admin uninstall` 也会自动清理该钩子。

卸载前先停止并禁用受跟踪服务。GPUBK 会依据 root-only 清单核对每个 unit，恢复安装前经
确认的旧 unit，并拒绝有漂移的文件。非空数据必须显式给出 `--purge-data`：

```bash
sudo systemctl disable --now gpubk-monitor.service gpubk-broker.service
sudo bk admin services uninstall --yes
sudo systemctl daemon-reload

sudo bk admin uninstall --dry-run --purge-data
sudo bk admin uninstall --purge-data --yes
sudo rm -rf /opt/gpubk
```

卸载清单会恢复安装前已有空目录的权限和被替换的旧配置。如果 broker 仍在运行、配置被
外部修改，或待删除目录中出现未知文件，卸载会拒绝继续。GPUBK 从不创建账号和用户组，
所以卸载也不会删除它们。`bk admin uninstall` 会删除由它创建的全局命令链接，但保留
安装前就存在的同目标链接。以上命令会删除受跟踪的服务器状态和独立 Python 环境；安装
清单中记录的原有文件与目录会恢复到安装前状态。安装过 worker unit 的每位
用户可同样执行 `systemctl --user disable --now bk-worker.service` 和
`bk service uninstall worker`。

`sudo bk admin services status` 会显示受跟踪解释器、UID/GID、unit 文件状态以及尚未清除的
enable 链接。GPUBK 负责安全写入和恢复 unit 文件；部署步骤仍显式保留 `systemctl`
的 enable、start、stop、disable，让管理员能看清持久进程何时发生变化。

### 配置与生产说明

`sudo bk admin init` 会把 root 管理的配置放在服务账号所有的台账目录之外。生成的配置除调度
参数外，还包含 broker 身份和 socket 策略：

```json
{
  "config_version": 1,
  "data_dir": "/data2/shared/bk",
  "gpu_count": 8,
  "slot_minutes": 5,
  "max_shared_users": 4,
  "disabled_gpus": [7],
  "gpu_priority": {"6": 10},
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
  "require_shared_memory": false,
  "shared_memory_reserve_mb": 512,
  "job_log_retention_days": 30,
  "job_log_max_mb": 64,
  "job_log_total_max_mb": 4096,
  "worker_poll_seconds": 1,
  "worker_max_parallel": 64,
  "worker_termination_grace_seconds": 5,
  "worker_claim_timeout_seconds": 30,
  "worker_recovery_grace_seconds": 5,
  "worker_live_guard": true,
  "monitor_interval_seconds": 2,
  "monitor_rollup_seconds": 60,
  "monitor_uid": 991,
  "broker_socket": "/run/gpubk/broker.sock",
  "broker_uid": 991,
  "broker_socket_mode": "0666",
  "tui_refresh_seconds": 1,
  "file_mode": "0644",
  "dir_mode": "0755"
}
```

当 `BK_DATA_DIR` 与 `BK_CONFIG_FILE` 都未设置时，GPUBK 会自动发现
`/etc/gpubk/config.json`。系统配置必须写明绝对 `data_dir`，因此普通 SSH、MCP
客户端和用户服务不依赖 shell 启动文件也会连接同一台账。非标准可信配置可通过
`BK_CONFIG_FILE` 选择；除非同时设置 `BK_DATA_DIR`，该文件也必须包含 `data_dir`。
显式设置 `BK_DATA_DIR` 会保留原有私有/数据目录内配置行为并跳过系统配置自动发现；
需要组合其他数据目录与外部配置时应同时设置两个变量。

私有安装若不设置上述覆盖，台账默认位于 `$XDG_DATA_HOME/bk`，私有任务状态位于
`$XDG_STATE_HOME/bk/jobs`，用户 unit 安装到 `$XDG_CONFIG_HOME/systemd/user`。按照
XDG 目录规范，只采用非空绝对路径；相对或空值分别回退到 `$HOME/.local/share`、
`$HOME/.local/state` 与 `$HOME/.config`。因此从不同工作目录运行 CLI 或 user service
仍会使用同一组路径。显式 `BK_JOB_LOG_DIR` 或 `job_log_dir` 必须为绝对路径（开头的
`~` 会先展开）。

请用 `id -u <服务账号>` 的结果替换 `991`。可选用户组模式下才会出现 `broker_gid`，
它只约束 socket，不改变台账所有权。配置文件及其每一级目录必须由 root 所有，并且
不可被 group/other 写入。broker 安全字段只接受这份可信外部配置。显式选择
`BK_DATA_DIR` 时，单用户安装继续兼容 `$BK_DATA_DIR/config.json` 默认路径。

monitor 由配置中的服务 UID 运行，直接写服务账号所有的利用率历史目录。退出码 `77`
表示当前进程不是指定 monitor 写入者。每位用户的 worker 仍把命令描述和日志放在自己的
XDG state 目录中，只有受限的任务状态更新通过 broker 写回公共台账。
任一守护进程返回 `78` 都表示其生效策略与台账不一致。不要改限制后盲目重试；先检查
`bk config`，修复可信配置，必要时重新安装固化配置的 service，再启动守护进程。

`max_shared_users` 为兼容旧配置保留，现表示每张 GPU 的整数 shared slot 上限；
旧预约没有 `share_units` 字段时按 1 个 slot 读取。

`slot_minutes` 控制预约开始时间和持续时间的粒度，默认值为 `5`；可选择 1 到 60
之间能够整除一小时的整数。单用户或测试环境可用 `BK_SLOT_MINUTES` 覆盖。多人服务器
应把它放在 root 管理的配置文件里：第一次写入会将该值绑定到台账，使用其他粒度的
客户端会被拒绝。

以下命令只读显示最终生效值，不会创建目录或修改台账：

```bash
bk config
bk config --json
```

环境变量可覆盖普通文件值，单次命令参数再覆盖对应默认值。安全字段 `monitor_uid`、
`broker_socket`、`broker_uid` 和 `broker_gid` 只能来自配置文件，不能用环境变量替换。新配置应声明
`"config_version": 1`，旧的无版本配置仍可兼容读取。未知字段、错误类型、NaN/Infinity、
不安全路径和越界数值会直接报错，不再静默忽略。JSON 报告只列出当前生效的环境变量名，
不会输出外部分配器命令内容。

调度策略、保留周期、worker 时序与并发、外部分配器和显示默认值可以配置；schema 版本、
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

普通用户运行时，预检会验证台账只读权限和内核认证的 broker 连接。服务账号还应再运行
一次，用于验证原子替换、文件锁、GPU 遥测和跨用户进程归属。受限的 `hidepid` 策略可能
阻止后者完成。

启用 monitor 后，再单独验证长驻写入者确实健康：

```bash
bk doctor --require-monitor --strict
bk doctor --require-monitor --json --strict
```

每位启用预约脚本的用户还应确认自己的私有 worker 正在服务这一份数据目录：

```bash
bk doctor --require-worker --strict
bk doctor --require-worker --json --strict
```

检查当前用户的完整部署时可同时使用两个 `--require-*` 参数。普通 `doctor` 会只读报告
隐私安全的 worker 状态，但不会强制纯预约用户启用可选服务，也不会创建私有目录。

broker 存储模式下会禁用 `bk reset`。试部署退役请使用带安装清单校验的
`sudo bk admin uninstall`；`bk reset` 仅保留给私有目录和可丢弃的模拟数据。

服务账号运行预检时会创建随机命名的临时文件，验证同目录原子替换与目录 fsync、同机
跨进程 `flock`、权限、剩余空间和真实 GPU 探测，随后删除临时文件。普通用户只执行
只读检查和 broker 身份验证。探测到的 GPU
编号必须严格等于 `0..gpu_count-1`；每张 NVML 设备都必须返回有效显存、进程列表和
CUDA 可用的稳定 GPU 标识及进程级利用率。拓扑不匹配、缺少稳定标识或缺少进程列表
会失败；缺少进程级利用率、模拟环境或
`nvidia-smi` 回退会在 strict 模式下作为警告失败。JSON 中的 `healthy` 只表示只读
台账检查，未运行 `--probe` 时 `ready` 保持为 `null`。
普通 `doctor` 不会初始化存储、加锁、恢复待处理事务，也不会跟随受管路径上的符号
链接或硬链接别名；它会报告台账、备份和遥测目录树中的权限漂移。权限或所有者发生漂移
时，写命令会在修改数据前失败，而不会静默修复非空目录。只有服务账号显式指定
`--probe` 才会写入临时文件。
若 NFS/FUSE 被多台机器共同挂载，
仍需从第二台机器验证跨主机锁传播，因为单机测试无法证明这一点。所有写入者都必须
通过 GPUBK。

安全边界、文件保护、WAL 恢复、私有任务文件和 MCP 隔离说明见
[SECURITY.md](SECURITY.md)。

## 无 GPU 机器试用

指定模拟卡数即可体验预约和 TUI：

```bash
export BK_DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gpubk-demo.XXXXXX")"
export BK_GPU_COUNT=4 BK_MAX_SHARED_USERS=4
bk t
bk 1 30m --share 3
```

示例会先创建权限私有的目录，因此在 `/tmp` 由符号链接提供的系统上也能运行。此时
硬件指标显示为 unknown，但调度、shared 容量、时间轴、Add/Edit、日志和 Agent JSON
均可正常使用。

## 开发

```bash
python3 -m pip install -e '.[mcp,gpu]'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python3 benchmarks/scheduler_queue.py
PYTHONPATH=src python3 benchmarks/usage_store.py
```

项目文档：[安全说明](SECURITY.md) · [升级说明](UPGRADING.md) · [发布流程](RELEASING.md) ·
[更新记录](CHANGELOG.md) · [Apache-2.0 许可证](LICENSE)
