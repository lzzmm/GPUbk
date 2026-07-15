# GPUBK

[English](README.md) | **简体中文**

<p align="center">
  <img src="docs/assets/gpubk-terminal-logo.svg" alt="GPUBK 终端字标" width="860">
</p>

**把 GPU 时间留给实验，而不是表格。**

GPUBK 是面向 Linux 共享服务器的轻量 GPU 预约与用量统计工具。用户通过简短的
`bk` 命令或终端时间轴预约 GPU；管理员获得原子存储、权限控制、监测和审计能力。

[安装](#安装) · [日常使用](#日常使用) · [共享服务器](#共享服务器) ·
[多机协同](#多机协同) · [详细文档](#详细文档)

## 主要功能

- shared 与 exclusive 预约、自动选卡和可配置时间粒度。
- 实时查看 GPU、进程、显存和利用率，无需网页服务。
- 容器感知的用量归属，明确区分实测、推断和歧义状态。
- 到达预约时间后运行命令，自动设置 `CUDA_VISIBLE_DEVICES`。
- 本地版本化存储、原子写入、备份和基于 UID 的权限检查。
- 支持 CLI、curses TUI、JSON、MCP，并为多机扩展预留能力。

GPUBK 是协作式调度器，最终的设备强制隔离仍由 Linux 权限负责。

## 安装

需要 Python 3.10 或更高版本。

### 快速体验

安装到自己的 Python 环境，不会创建系统服务，也不会修改 `/etc`：

```bash
python3 -m pip install 'gpubk[gpu]'
bk --version
bk tutorial
```

没有 GPU 也可以模拟时间轴：

```bash
BK_DATA_DIR=/tmp/gpubk-demo BK_GPU_COUNT=4 bk t
```

### 多人服务器

多人 GPU 服务器由管理员执行一次：

```bash
sudo python3 -m venv /opt/gpubk
sudo /opt/gpubk/bin/python -m pip install --upgrade pip
sudo /opt/gpubk/bin/python -m pip install 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install
bk doctor --probe --require-monitor --strict
```

安装器默认创建 `/usr/local/bin/bk`。如果 `/usr/local/bin` 不是 root 持有，不要修改
这个目录的所有权；先确认 `/usr/bin/bk` 不存在，再运行
`sudo /opt/gpubk/bin/bk admin install --command-path /usr/bin/bk`。

引导程序会创建全局命令、数据目录和开机服务。之后普通用户直接运行 `bk`，无需
`sudo`，也不能修改其他 UID 的预约或管理员配置。

部署成功时，两个服务应为 `active`，预检结果应为 `ready`：

```bash
sudo systemctl status gpubk-broker gpubk-monitor
bk doctor --probe --require-monitor --strict
```

## 日常使用

```bash
bk                 # 状态和交互提示符
bk 1 30m           # 预约 1 张 GPU，持续 30 分钟
bk 2 1h30m 12g     # 2 张 GPU、90 分钟、每卡预计 12 GiB
bk x 1 2h           # 排他预约
bk a                # 引导式预约
bk l                # 查看自己的预约
bk g                # 推荐当前可用 GPU
bk run -- python train.py
bk t                # 可视化终端时间轴
bk u                # 个人用量统计
```

需要帮助时运行 `bk -h`、`bk help COMMAND` 或 `bk tutorial`。

## 共享服务器

broker 是受保护台账的唯一写入者，monitor 通过 NVML 采集 GPU 状态，CLI 和 TUI
通过本机 Unix socket 访问服务。两个服务都会在开机后自动启动。GPUBK 默认不创建
Linux 用户或用户组，由执行安装的管理员账号持有部署。

升级不会删除配置和历史数据：

```bash
sudo /opt/gpubk/bin/python -m pip install --upgrade 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install --yes
bk doctor --probe --require-monitor --strict
```

升级会保留数据和策略。涉及删除或所有权变更时，应先使用 `--dry-run` 预览。

### 卸载多人服务器部署

管理员卸载会删除服务、配置、全局 `bk` 链接，并可选择清除全部 GPUBK 数据。Python
包安装在独立环境 `/opt/gpubk` 中，不属于系统 `python3`；最后直接删除这个环境即可：

```bash
sudo systemctl disable --now gpubk-monitor.service gpubk-broker.service
sudo /opt/gpubk/bin/bk admin services uninstall --yes
sudo /opt/gpubk/bin/bk admin uninstall --purge-data --yes
sudo rm -rf /opt/gpubk
hash -r
command -v bk || echo "GPUBK 已卸载"
```

需要先核对删除范围时，运行
`sudo /opt/gpubk/bin/bk admin uninstall --dry-run --purge-data`。不要运行
`python3 -m pip uninstall gpubk`：系统 Python 看不到安装在 `/opt/gpubk` 中的包。

## 多机协同

cluster 模式连接多个各自独立、安全的 GPUBK 节点。每台 GPU 服务器仍保留自己的
broker、台账、monitor 和最终调度权；客户端通过校验 host key 的 SSH 比较各节点，
并把一次预约提交给其中一台。它不需要中央数据库或新的网络服务，预约也不会跨机器拆分。

1. 在每台 GPU 服务器上按单机方式安装并通过 `doctor`。
2. 为需要使用集群的用户配置免交互 SSH，并事先核对 host key。
3. 建立 root 持有的节点目录并检查所有连接。

```bash
sudo bk admin cluster init gpu-a --yes
bk c probe gpu-b gpu-b
# 核对并运行它打印的完整 `sudo bk admin cluster add ...` 命令。
bk c check
bk c rec 2 1h
bk c 2 1h
```

如果当前机器只是登录节点，不执行 `cluster init`，直接用 `bk c probe` 添加 GPU
服务器。正式部署前请阅读 [CLUSTER.md](docs/CLUSTER.md)，其中说明了节点身份、UID 映射、
故障行为、NFS 历史导出和定时任务。

## 详细文档

- [中文管理员与用户完整手册](https://github.com/lzzmm/gpubk/blob/main/docs/GUIDE.zh-CN.md)
- [English complete guide](https://github.com/lzzmm/gpubk/blob/main/docs/GUIDE.md)
- [升级说明](docs/UPGRADING.md)
- [安全模型](SECURITY.md)
- [多机部署](docs/CLUSTER.md)
- [监测数据格式](docs/TELEMETRY.md)
- [发布流程](docs/RELEASING.md)

本项目采用 [Apache-2.0](LICENSE) 许可证。
