# bk GPU 预约系统

`bk` 是一个离线、文件存储、面向实验室 GPU 共享服务器的预约工具。

## 常用命令

```bash
bk
bk 1 4h
bk auto 1 4h
bk exclusive 1 4h
bk tui
bk add
bk edit <number_or_short_id>
bk del <reservation_id>
bk log
bk doctor
bk reset --yes
```

- `bk`：进入普通行式交互会话，不接管屏幕、不切换背景。
- `bk 1 4h`：默认共享模式，自动预约 1 张 GPU 4 小时。
- `bk auto 1 4h`：兼容别名，等价于 `bk 1 4h`。
- `bk exclusive 1 4h`：排他模式，预约期间不允许其他人共享同一张卡。
- `bk tui`：显式进入全屏 TUI 实验界面。
- `bk doctor`：只读检查台账里违反当前策略的记录，例如 shared 超容量或 exclusive 重叠。
- `bk edit 1 --duration 8h`：用列表序号修改自己的预约。
- `bk del 6e957ef1`：用短 ID 取消自己的预约。
- `bk reset --yes`：清空当前数据目录里的台账、日志、备份。

不传 `--start` 时，系统会从当前时间开始找最早可用时段；如果当前不可用，会自动排到后面并显示 `queued:`。显式传 `--start` 时表示精确预约，冲突就失败，不会悄悄改时间。

预约粒度为 5 分钟：自动预约会从当前时间向上取整到下一个 5 分钟点；显式 `--start` 必须落在 5 分钟边界；时长必须是 5 分钟倍数。

共享模式按预约记录计数：同一张 GPU 的同一 5 分钟时间片最多允许 `BK_MAX_SHARED_USERS` 条 shared 预约，哪怕这些预约来自同一个 UID；exclusive 仍然与任何重叠预约互斥。

所有对用户显示的时间都是本地时间，例如 `2026-07-08 14:30 +0800`；台账内部仍使用 UTC 保存。

## 配置

默认数据目录为 `/data2/shared/bk`，可通过环境变量覆盖：

```bash
export BK_DATA_DIR=/data2/shared/bk
export BK_GPU_COUNT=8
export BK_MAX_SHARED_USERS=2
export BK_QUEUE_SEARCH_HOURS=168
```

也可以在数据目录下放置 `config.json`：

```json
{
  "gpu_count": 8,
  "max_shared_users": 2,
  "queue_search_hours": 168,
  "lock_timeout_seconds": 10
}
```

环境变量优先级高于 `config.json`。

## 实时 GPU 行为监测

生产服务器建议安装官方 NVML Python 绑定：

```bash
python3 -m pip install -e '.[gpu]'
```

`bk tui` 每秒自动刷新一次，不需要按键。NVML 在进程内只初始化一次并复用设备句柄，不会每帧启动 `nvidia-smi`。GPU 行显示整卡利用率、显存、温度、进程数和违规数；进入 GPU 焦点后，下方显示该卡的实时进程：PID、UID 对应用户名、进程类型、进程 SM 利用率、显存、预约匹配状态和命令。

进程状态：

- `ok`：该 UID 当前在这张 GPU 上有有效预约。
- `wrong-gpu`：该 UID 当前有预约，但没有预约正在使用的这张 GPU。
- `unreserved`：该 UID 当前没有任何有效预约。
- `unknown`：系统无法从 `/proc/<pid>` 读取 UID；只告警未知，不直接判定违规。

shared 模式按 UID 分别匹配预约；同一 UID 的多个 CUDA 进程可以归属同一条预约。违规进程在 GPU 标签中显示 `!N`，并在进程表中标红。`bk status` 也会输出相同的进程与预约匹配结果。

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
bk> exclusive 1 2h --gpu 1
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
[/] 缩短/延长持续时间，每次 5 分钟
s/x 切换 shared/exclusive
f 按当前已选 GPU 数量，自动查找任意 GPU 的最近可用时段
g 固定当前已选 GPU，仅查找这些卡的最近可用时段
Enter 提交当前时间轴选区，冲突时不会自动挪时间
Esc 取消
```

- `f` / `g` 从当前时间游标向后搜索，并把时间轴直接定位到结果；它们只更新闪烁预览，不会直接创建预约，仍需按 `Enter` 确认。
- `f` 可以自动更换 GPU；如果当前没有选卡，则按 1 张 GPU 查找。`g` 要求至少选中一张 GPU，并保持这些卡不变。
- edit 会预载原预约的开始时间、时长、GPU 和模式；校验时自动排除原记录，不会与自己冲突。
- add/edit 进入时锁定时间轴基准，操作期间不会因跨过 5 分钟边界而自动跳格。
- shared 预览使用青绿色，exclusive 预览使用橙色，冲突预览使用红色。
- add 预览区间自身会闪烁；原来选中的预约在 add/edit 期间保持静止，避免两处同时闪烁。edit 预览保持常亮，便于和原区间比较。

GPU 焦点：

- 在预约列表第一项继续按 `↑`，或按 `Tab`，进入 GPU 焦点；`↑/↓` 选择 GPU。
- GPU 焦点会用 `>` 标记当前 GPU，下方先展开该卡在当前窗口内的 shared 预约明细，再显示实时 GPU 进程与预约匹配状态。
- GPU 焦点下按 `a` 会直接以当前 GPU 开始新增；按 `Tab`，或从最后一张 GPU继续按 `↓`，返回预约列表。

TUI 时间轴：

- 默认每列代表 5 分钟，顶部显示当前窗口范围。
- 时间刻度分为小时、分钟和连续刻度尺三行；小时显示为 `17h`，分钟显示为 `15` / `30`，整点由小时行表达，不再重复显示 `00`。
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
