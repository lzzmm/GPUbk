# GPUbk

GPUbk is an offline, zero-core-dependency GPU booking CLI and curses TUI for shared lab servers. Its deliberately short command is `bk`. It provides 5-minute shared/exclusive scheduling, VRAM admission, live and historical load-aware placement, per-user scheduled jobs, atomic file transactions, stable JSON, optional MCP, and a bundled Codex Skill.

```bash
python3 -m pip install gpubk          # zero runtime dependencies
python3 -m pip install 'gpubk[gpu]'  # low-overhead NVML telemetry
bk 2 1h30m --mem 12g
bk t
```

The detailed guide below is currently in Chinese.

`bk` 是一个离线、文件存储、面向实验室 GPU 共享服务器的预约工具。

## 常用命令

```bash
bk
bk 1 4h
bk 2 1h30m --mem 12g
bk 2 1h30m --mem 12g -- python train.py --config exp.yaml
bk s 1 4h
bk x 1 4h
bk t
bk a
bk e <number_or_short_id>
bk d <number_or_short_id>
bk l
bk m
bk u
bk agent context
bk agent recommend 2 1h30m --mem 12g
bk reset --yes
```

- `bk`：进入普通行式交互会话，不接管屏幕、不切换背景。
- `bk 1 4h`：默认共享模式，自动预约 1 张 GPU 4 小时。
- `bk 2 1h30m --mem 12g`：预约 2 张卡 1 小时 30 分钟，并声明每张卡预计使用 12 GiB 显存。
- 在 `--` 后追加命令，会把实验和预约绑定；worker 到点执行并自动设置可见 GPU。
- `bk s 1 4h`：显式 shared；`shared` / `auto` 仍兼容。
- `bk x 1 4h`：exclusive 排他模式；完整单词 `exclusive` 仍兼容。
- `bk t`：进入全屏 TUI；完整命令是 `bk tui`。
- `a/e/d/l/m/u` 分别是 `add/edit/del/list/monitor/usage` 的短命令。
- `bk doctor`：只读检查台账里违反当前策略的记录，例如 shared 超容量或 exclusive 重叠。
- `bk monitor`：低开销持续采集 GPU 进程行为，记录状态变化和分钟级汇总。
- `bk usage`：查看最近的进程事件；`bk usage --rollups` 查看利用率汇总。
- `bk agent context`：输出版本化、脱敏的完整资源上下文 JSON。
- `bk agent recommend 2 1h30m --mem 12g`：只读计算合法卡组和最早时段，不写台账。
- `bk edit 1 --duration 8h`：用列表序号修改自己的预约。
- `bk del 6e957ef1`：用短 ID 取消自己的预约。
- `bk reset --yes`：清空当前数据目录里的台账、日志、备份。

不传 `--start` 时，系统会从当前时间开始找最早可用时段；如果当前不可用，会自动排到后面并显示 `queued:`。显式传 `--start` 时表示精确预约，冲突就失败，不会悄悄改时间。

预约粒度为 5 分钟：自动预约会从当前时间向上取整到下一个 5 分钟点；显式 `--start` 必须落在 5 分钟边界；时长必须是 5 分钟倍数。

共享模式按预约记录计数：同一张 GPU 的同一 5 分钟时间片最多允许 `BK_MAX_SHARED_USERS` 条 shared 预约，哪怕这些预约来自同一个 UID；exclusive 仍然与任何重叠预约互斥。

自动选卡不只看预约台账。系统按“当前进程与整卡利用率 + 最近 30 分钟衰减加权负载 + 请求时段内已有预约密度”生成可解释评分，优先选择当前空闲、近期负载低、未来预约稀疏的 GPU。实时任务没有可靠结束时间，因此它们是强优先级提示而不是伪造的永久预约：空闲卡足够时自动避开忙卡；不得不选择忙卡时明确输出当前用户/PID 或利用率警告。

shared 预约建议使用 `--mem` 声明每张 GPU 的预计显存。调度器会同时检查 shared 记录数和预计显存总量；未声明的记录按“可分配显存 / shared limit”保守估算。创建结果会显示当前物理空闲显存和预约区间的预计剩余显存。管理员可设置 `BK_REQUIRE_SHARED_MEMORY=true` 将声明改为必填。

所有对用户显示的时间都是本地时间，例如 `2026-07-08 14:30 +0800`；台账内部仍使用 UTC 保存。

## 配置

默认数据目录遵循 XDG，为 `~/.local/share/bk`，适合单用户试用。多人服务器必须显式指定一个由管理员预先创建、绑定实验室用户组的共享目录：

```bash
sudo install -d -m 2770 -o root -g gpuusers /data2/shared/bk
export BK_DATA_DIR=/data2/shared/bk
export BK_GPU_COUNT=8
export BK_MAX_SHARED_USERS=2
export BK_QUEUE_SEARCH_HOURS=168
export BK_LEDGER_RETENTION_DAYS=90
export BK_REQUIRE_SHARED_MEMORY=false
export BK_SHARED_MEMORY_RESERVE_MB=512
export BK_JOB_LOG_DIR="$HOME/.local/state/bk/jobs"
export BK_FILE_MODE=0660
export BK_DIR_MODE=2770
```

也可以在数据目录下放置 `config.json`：

```json
{
  "gpu_count": 8,
  "max_shared_users": 2,
  "queue_search_hours": 168,
  "ledger_retention_days": 90,
  "lock_timeout_seconds": 10,
  "require_shared_memory": false,
  "shared_memory_reserve_mb": 512,
  "file_mode": "0660",
  "dir_mode": "2770"
}
```

环境变量优先级高于 `config.json`。

多人共享部署中的 `config.json` 必须由 root 所有且不可被 group/other 写入，例如 `root:gpuusers 0644`；单用户配置也可由当前 UID 所有。程序拒绝 symlink、非普通文件、超过 1 MiB 的配置以及不可信权限，避免共享目录中的其他用户注入 allocator 命令或私有日志路径。

未设置 `gpu_count` 或 `BK_GPU_COUNT` 时，bk 会自动发现本机可见 GPU 数量；无 GPU 环境保持一张 unknown 卡，便于直接试用。多人共享服务器仍建议显式配置卡数，以固定调度边界，并避免容器可见设备变化影响策略。

首个预约写事务会把 GPU 数量、shared limit、5 分钟粒度、显存策略以及文件/目录模式绑定进台账。后续客户端若通过环境变量或不同配置改变这些关键规则，所有规划和写入都会 fail closed，并明确列出不一致字段；这避免同一个共享台账被不同用户按不同容量规则解释。旧台账在下一次成功新增或编辑时自动绑定当前策略。

为避免热台账随多年历史无限膨胀，已结束或取消超过 `ledger_retention_days` 的预约会在后续写事务中从 `ledger.json` 移除；完整新增、取消和任务事件仍保留在只追加 `ops.log`。设为 `0` 可关闭清理，但长期共享部署不建议这样做。超过保留期后，同一个 operation ID 不再提供幂等重放保证。

## Agent 与 JSON 接口

Agent 不需要解析彩色 TUI 或人类文本。稳定机器接口使用 `schema_version=bk.agent.v1`：

```bash
bk agent context --compact
bk agent recommend 2 1h30m --mode s --mem 12g --compact
bk 2 1h30m --mem 12g --op-id agent-run-20260711-001 --json
bk l --json
bk j --json
```

`context` 包含当前 UID、调度策略、GPU 型号与温度、每卡实时状态、近期预测负载、显存、预约和能力声明；不包含完整进程参数、私有 job spec 或任意 token。`recommend` 是严格只读的，返回推荐卡组、起止时间、是否排队、置信度、每卡评分、预约压力、显存余量和警告。显式 `--start` 保持 exact 语义，冲突时 `available=false` 并给出 `nearest_available`。

实际写入建议总是传唯一 `--op-id`。相同 UID 重试同一个 operation ID 会返回原结果，不会重复预约。`--json` 成功和业务错误都输出单个 JSON 对象；冲突/参数错误退出码为 `2`，只读推荐无合法精确时段退出码为 `3`。

### 外部 AI allocator

管理员可以选择配置一个本地 Agent 子进程：

```bash
export BK_ALLOCATOR_COMMAND='python3 /opt/bk/my_allocator.py'
export BK_ALLOCATOR_TIMEOUT_SECONDS=3
export BK_ALLOCATOR_WEIGHT=5
```

`bk` 以 `shell=False` 启动它，通过 stdin 发送 `bk.allocator.v1` JSON；Agent 在 stdout 返回：

```json
{
  "schema_version": "bk.allocator.v1",
  "gpu_order": [3, 2, 1, 0],
  "reason": "spread recent thermal and memory pressure"
}
```

`gpu_order` 必须是所有 GPU 索引的完整排列。外部顺序只以有界权重参与本地评分，最终方案仍强制经过 exclusive/shared 冲突、5 分钟粒度、显存预算和文件事务校验，Agent 无法绕过安全约束。超时、崩溃、非法 JSON、重复或越界卡号会回退内置算法并在 JSON 中给出 warning。该命令是明确的信任边界：默认关闭；启用后它以配置者当前 UID 运行，可自行访问网络或本地文件，因此只应配置受信任程序。

### MCP 与 Codex Skill

核心包保持零依赖。需要 MCP 时安装可选 extra：

```bash
python3 -m pip install 'gpubk[mcp]'
bk-mcp                 # 或 bk mcp；本地 stdio transport
```

MCP 服务提供 `bk://context` resource、规划 prompt，以及以下结构化 tools：

- `get_gpu_context`
- `recommend_gpu_booking`
- `create_gpu_booking`（强制 `operation_id`）
- `list_gpu_reservations`
- `cancel_my_gpu_booking`
- `read_my_job_log`

服务身份始终来自启动 `bk-mcp` 的系统 UID，tool schema 没有 UID 参数。默认只提供本地 stdio，不开放监听端口；每位用户应启动自己的 MCP 进程。当前 optional extra 固定在官方 Python SDK 稳定 v1 线 `<2`，避免预发布 v2 的破坏性变化。

所有 MCP tools 都带标准风险注解：context/recommend/list/log 标记为 read-only，create 因强制 operation ID 标记为 idempotent write，cancel 标记为 destructive，且全部是本地 closed-world 操作。支持这些注解的 Agent 无需从文案猜测调用风险。

只读 Agent/MCP 调用不会为一个空数据目录创建台账、锁或备份目录，也不会修改已有预约。唯一例外是检测到此前已经持久化但尚未应用完成的 WAL：读取会先完成该已提交事务的恢复，以免向 Agent 返回过期状态。

wheel 内置 Skill：

```bash
bk skill install
bk skill show
```

默认安装到 `${CODEX_HOME:-~/.codex}/skills/gpubk`；已有目录时拒绝覆盖，更新需显式 `--force`。Skill 会指导 Agent 先读 context、再 recommend、获得写入授权后使用稳定 operation ID 提交，并正确处理 shared VRAM、exact start、queued、uncertain job 和外部 allocator 的安全边界。

私有默认权限为目录 `0700`、文件 `0600`。共享部署必须同时配置 `file_mode=0660`、`dir_mode=2770`，并让共享目录具有正确组所有权和 setgid 位；程序不会擅自 `chown` 或修改已存在目录的权限。原子替换产生的新台账、journal、备份和日志都会保持配置的文件模式。

预约写入使用 write-ahead journal：先持久化完整事务，再原子替换台账，最后以事件 ID 幂等追加审计日志。进程在任一步崩溃后，下次访问会完成恢复，不会把“台账已写、CLI 报失败”变成重复预约。`transaction.json` 正常提交后立即删除；若长期存在，说明有待恢复的事务，应先运行 `bk doctor`，不要手工删除。

`flock` 是 advisory lock，所有写入者都必须通过 `bk`。Linux 本地文件系统可直接使用；NFS 是否提供可靠跨客户端锁取决于服务端、挂载参数和锁服务，正式部署前必须运行仓库的并发验收测试。无法保证锁语义的对象存储、部分 FUSE 文件系统不属于支持范围。

共享台账、锁、WAL、备份与监控日志的叶子路径只接受普通文件；符号链接、FIFO、设备文件和符号链接备份目录会 fail closed。该防护避免共享目录成员将另一个用户执行 `bk` 时的写入重定向到其个人文件。

## 到点自动运行实验

最短工作流：

```bash
bk 2 1h30m --mem 12g -- python train.py --config exp.yaml
bk jobs
bk worker
```

预约保存的是参数数组而不是 shell 字符串，worker 使用 `shell=False` 原样启动，因此 `;`、重定向和命令替换不会被隐式解释。确实需要 shell 语法时显式写成：

```bash
bk 1 30m -- sh -lc 'python train.py > train.log 2>&1'
```

worker 只领取与当前系统 UID 完全相同的预约，不能代表其他用户执行。启动子进程时自动设置：

- `CUDA_VISIBLE_DEVICES`：预约到的物理 GPU，例如 `2,5`；程序内部会看到逻辑设备 `0,1`。
- `CUDA_DEVICE_ORDER=PCI_BUS_ID`。
- `BK_RESERVATION_ID`、`BK_RESERVED_GPUS`。
- 声明 `--mem` 时还会设置 `BK_EXPECTED_GPU_MEMORY_MB`。

完整命令和工作目录不会写入共享 `ledger.json`。它们保存在当前用户的私有 job spec 中（目录 `0700`、文件 `0600`）；共享台账只包含 spec UUID、SHA-256 完整性摘要和不含任意参数值的简短标签。worker 启动前同时校验文件所有者、权限、UID 和摘要，台账或 spec 任意一边被篡改都会拒绝执行。stdout/stderr 同样写入当前用户私有日志。常用命令：

```bash
bk j                     # jobs
bk jl 1                  # job-log
bk jr 1                  # retry failed/interrupted job
bk w --once              # 只执行当前已到点任务，结束后退出
```

任务到预约结束仍未退出时，worker 先发送 `SIGTERM`，5 秒后仍存活则发送 `SIGKILL`；取消正在运行的预约也会触发相同流程。领取任务采用 at-most-once 策略：worker 在持久化 claim 后崩溃时，任务变为 `uncertain`，不会冒险自动执行第二次。确认进程确实不存在后，使用：

```bash
bk jr 1 --accept-duplicate-risk
```

生产环境建议每位需要自动运行实验的用户自行安装并启用 wheel 内置的用户级 worker unit，而不是部署共享 root worker：

```bash
bk service show worker
bk service install worker
systemctl --user daemon-reload
systemctl --user enable --now bk-worker.service
```

服务只以该用户身份执行其预约。若系统未为用户启用 linger，用户退出登录后 user service 可能停止；是否开启 linger 应由服务器管理员按实验室策略决定。

## 实时 GPU 行为监测

生产服务器建议安装官方 NVML Python 绑定：

```bash
python3 -m pip install -e '.[gpu]'
```

`bk tui` 每秒自动刷新一次，不需要按键。NVML 在进程内只初始化一次并复用设备句柄，不会每帧启动 `nvidia-smi`。GPU 行显示整卡利用率、显存、温度、进程数和违规数；进入 GPU 焦点后，下方显示该卡的实时进程：PID、UID 对应用户名、进程类型、进程 SM 利用率、显存、预约匹配状态和脱敏命令标签。任意命令参数不会写入共享审计日志，避免意外泄露 token。

进程状态：

- `ok`：该 UID 当前在这张 GPU 上有有效预约。
- `wrong-gpu`：该 UID 当前有预约，但没有预约正在使用的这张 GPU。
- `unreserved`：该 UID 当前没有任何有效预约。
- `unknown`：系统无法从 `/proc/<pid>` 读取 UID；只告警未知，不直接判定违规。
- `system`：已知的显示服务或 NVIDIA 守护进程，例如 Xorg；保留可见性，但不计为违规。普通 root 计算进程不会自动获得豁免。

shared 模式按 UID 分别匹配预约；同一 UID 的多个 CUDA 进程可以归属同一条预约。违规进程在 GPU 标签中显示 `!N`，并在进程表中标红。`bk status` 也会输出相同的进程与预约匹配结果。

### 全天候审计

前台启动监测器：

```bash
bk monitor
```

默认每 2 秒采样、每 60 秒写一批汇总。监测器不会每秒写文件：只有进程开始、结束或预约授权变化时才追加事件，利用率在窗口结束时批量追加。常用试验参数：

```bash
bk monitor --once
bk monitor --samples 5 --interval 1 --rollup 5 --verbose
bk usage --limit 20
bk usage --rollups --limit 20
```

数据文件：

- `usage-events.jsonl`：只追加的 `process-start`、`process-stop`、`authorization-change` 事件。
- `usage-rollups.jsonl`：按 GPU、UID、授权状态和预约集合聚合的利用率；包含进程数、SM、显存、整卡利用率和观察时长。
- `usage-state.json`：当前进程状态，用于监测器重启后避免重复产生开始事件。
- `usage-load.json`：每张 GPU 最近的紧凑负载窗口，用于自动选卡预测；默认只保留有限窗口，不在每次预约时扫描完整历史日志。
- `usage.lock`：保证同一数据目录只有一个监测器运行。

预约存在但没有对应进程时仍会生成 `status=ok`、`avg_process_count=0` 的汇总，用来识别预约空置。`bk reset --yes` 会在监测器未运行时一起清理这些审计文件；监测器正在运行时 reset 会因 `usage.lock` 超时而拒绝执行。

wheel 内置 `bk-monitor.service` 用户级 systemd 模板。正式启用前创建 `~/.config/bk/bk.env`：

```bash
BK_DATA_DIR=/data2/shared/bk
BK_GPU_COUNT=8
BK_MAX_SHARED_USERS=2
```

然后安装和启动：

```bash
bk service show monitor
bk service install monitor
systemctl --user daemon-reload
systemctl --user enable --now bk-monitor.service
```

服务模板使用 `Nice=10` 和 idle IO 调度，降低对训练任务的影响。实验部署建议先用 `--samples` 有界运行验证，不要直接启用常驻服务。

没有安装 `nvidia-ml-py` 时会回退到 `nvidia-smi`，但只能稳定提供整卡级指标，进程级行为监测应以 NVML 模式运行。MIG 下 NVML 可能不提供进程 SM 利用率，此时该列显示 `-`，PID、UID 和显存归属仍可继续工作。在容器里运行时必须看到宿主机 `/proc` PID 命名空间，例如使用 `--pid=host`。

无 GPU 机器可通过模拟文件验证：

```bash
BK_GPU_SIM_FILE=/tmp/bk-gpu-sim.json BK_GPU_COUNT=2 \
  BK_DATA_DIR=/tmp/bk-demo PYTHONPATH=src python3 -m bk tui
```

模拟文件格式：

```json
{
  "gpus": [
    {
      "index": 0,
      "name": "Sim Pro 6000",
      "memory_used_mb": 4096,
      "memory_total_mb": 98304,
      "utilization_percent": 72,
      "temperature_c": 61,
      "processes": [
        {
          "pid": 4321,
          "uid": 1001,
          "username": "alice",
          "command": "python train.py",
          "gpu_memory_mb": 3072,
          "sm_utilization_percent": 68,
          "kind": "C"
        }
      ]
    }
  ]
}
```

## 开发测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
BK_DATA_DIR=/tmp/bk-dev BK_GPU_COUNT=2 BK_MAX_SHARED_USERS=2 PYTHONPATH=src python3 -m bk 1 30m
BK_DATA_DIR=/tmp/bk-dev BK_GPU_COUNT=2 BK_MAX_SHARED_USERS=2 PYTHONPATH=src python3 -m bk
```

进入交互会话后可输入：

```text
bk> status
bk> 1 2h --gpu 0
bk> x 1 2h --gpu 1
bk> list
bk> edit 1
bk> del 1
bk> log
bk> doctor
bk> quit
```

交互首页会显示普通终端里的 ASCII 时间轴：

```text
Timeline (next 24h, local)
      14 15 16 17 18 19 ...
GPU0  .  M  M  1  1  X ...
GPU1  .  .  .  .  .  . ...
Legend: . free, M mine, X exclusive, 1-9 shared record count
```

预约列表第一列是你自己的预约序号，可直接用于 `edit` / `del`；第二列是 8 位短 ID，也可用于命令。

全屏 TUI：

```bash
BK_DATA_DIR=/tmp/bk-demo BK_GPU_COUNT=4 PYTHONPATH=src python3 -m bk tui
```

快捷键：

```text
q 退出
r 刷新
+/- 缩放时间轴，默认 5 分钟/列
←/→ 平移时间窗口
↑/↓ 选择自己的预约
Tab 在预约列表与 GPU 列表之间切换焦点
a 新增预约
e 修改选中的预约
d 删除选中的预约
? 查看帮助
```

Add/Edit 时间轴模式快捷键：

```text
a 进入时间轴新增模式
e 将当前选中的预约载入时间轴编辑模式
←/→ 以 5 分钟粒度移动开始时间
↑/↓ 移动 GPU 光标
space 选中/取消当前 GPU，支持多卡
+/- 或 [/] 缩短/延长持续时间，每次 5 分钟
m 输入每张 GPU 的预计显存，例如 12g；输入 - 清除声明
1-9 设置 GPU 数量，并立即自动跳到最近满足数量的时间和卡组
s/x 切换 shared/exclusive
f 按当前已选 GPU 数量，自动查找任意 GPU 的最近可用时段
g 固定当前已选 GPU，仅查找这些卡的最近可用时段
r 恢复 Add 默认值，或将 Edit 恢复为原预约
Enter 提交当前时间轴选区，冲突时不会自动挪时间
Esc 取消
```

- Add/Edit 顶部会显示模式、短 ID、GPU 数量与卡号、日期星期、起止时间、时长以及 `READY/BLOCKED` 状态。
- `1-9`、`f` / `g` 从当前时间游标向后搜索，并把时间轴直接定位到结果；它们只更新预览，不会直接写入，仍需按 `Enter` 确认。
- `f` 可以自动更换 GPU；如果当前没有选卡，则按 1 张 GPU 查找。`g` 要求至少选中一张 GPU，并保持这些卡不变。
- Edit 会预载原预约的开始时间、时长、GPU 和模式；校验和自动找位时都会排除原记录，不会与自己冲突，按 `r` 可恢复原值。
- Add/Edit 顶部同时显示预计显存和当前所选卡中最小的物理空闲显存；shared 预览会实时执行显存预算校验。
- add/edit 进入时锁定时间轴基准，操作期间不会因跨过 5 分钟边界而自动跳格。
- shared 预览使用青绿色，exclusive 预览使用橙色，冲突预览使用红色。
- add 预览区间自身会闪烁；原来选中的预约在 add/edit 期间保持静止，避免两处同时闪烁。edit 预览保持常亮，便于和原区间比较。

GPU 焦点：

- 在预约列表第一项继续按 `↑`，或按 `Tab`，进入 GPU 焦点；`↑/↓` 选择 GPU。
- GPU 焦点会用 `>` 标记当前 GPU，下方先展开该卡在当前窗口内的 shared 预约明细，再显示实时 GPU 进程与预约匹配状态。
- GPU 焦点下按 `a` 会直接以当前 GPU 开始新增；按 `Tab`，或从最后一张 GPU继续按 `↓`，返回预约列表。

TUI 时间轴：

- 默认每列代表 5 分钟，顶部显示当前窗口范围。
- 时间刻度分为日期、小时、分钟和连续刻度尺四行；左侧始终显示当前窗口的日期与星期，例如 `07-11 Fri`，跨午夜会在准确列标出下一天。小时显示为 `17h`，分钟显示为 `15` / `30`，整点不再重复显示 `00`。
- 不同预约会用相对柔和的高对比颜色显示；同一个预约在时间轴和表格中颜色一致。
- `.` 表示空闲，彩色 `█` 表示单条预约区间。
- 主时间轴始终保持一张 GPU 一行，适合 8 卡以上机器查看全局占用。
- 两条 shared 重叠时，主时间轴用 `▀` 上下双色半格直接显示双方。
- 三条以上 shared 使用 `▚` / `▞` 象限格做等面积彩色编织：三人按 `AB / BC / CA` 循环，四人按 `AB / CD` 循环；不改变每列 5 分钟的时间尺度，并在 GPU 标签中显示峰值容量，例如 `S4/4`。
- 终端颜色能力不足时，多人编织自动回退为 `▓`，预约与容量信息仍保持正确。
- 选中 shared 预约后，下方只展开共享最密集的那张 GPU；每条细轨显示短 ID、用户名和固定颜色，选中轨道闪烁。终端高度不足时保持一卡一行，不强行展开。
- 新增预览可用时高亮，不可用时标红并在底部显示原因。
- 下方表格选中的预约会在上方时间轴中闪烁提示。
- 下方表格包含序号、短 ID、用户名、模式、GPU、共享容量、开始、结束、持续时间。

## 性能回归

仓库包含无第三方依赖的 8 卡满负载排队基准：

```bash
PYTHONPATH=src python3 benchmarks/scheduler_queue.py
```

输出为 `bk.benchmark.v1` JSON，默认构造 2688 条预约并搜索 7 天后的首个 8 卡时段。单元测试同时约束每条活动记录在一次搜索中最多解析一次开始和结束时间，并约束 TUI 整帧时间格不重复解析预约时间；这比依赖具体机器速度的固定毫秒阈值更稳定。
