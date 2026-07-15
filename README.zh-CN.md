# GPUBK

[English](README.md) | **简体中文**

GPUBK 是面向 Linux 共享服务器的轻量 GPU 预约与用量统计工具。用户通过简短的
`bk` 命令或终端时间轴预约 GPU；管理员获得原子存储、权限控制、监测和审计能力。

## 主要功能

- shared 与 exclusive 预约、自动选卡和可配置时间粒度。
- 实时查看 GPU、进程、显存和利用率，无需网页服务。
- 到达预约时间后运行命令，自动设置 `CUDA_VISIBLE_DEVICES`。
- 本地版本化存储、原子写入、备份和基于 UID 的权限检查。
- 支持 CLI、curses TUI、JSON、MCP，并为多机扩展预留能力。

GPUBK 是协作式调度器，最终的设备强制隔离仍由 Linux 权限负责。

## 安装

需要 Python 3.10 或更高版本。

个人使用或快速体验：

```bash
python3 -m pip install 'gpubk[gpu]'
bk tutorial
```

多人 GPU 服务器由管理员执行一次：

```bash
sudo python3 -m venv /opt/gpubk
sudo /opt/gpubk/bin/python -m pip install --upgrade pip
sudo /opt/gpubk/bin/python -m pip install 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install
bk doctor --probe --require-monitor --strict
```

引导程序会创建全局命令、数据目录和开机服务。之后普通用户直接运行 `bk`，无需
`sudo`，也不能修改其他 UID 的预约或管理员配置。

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

## 管理与更新

```bash
sudo /opt/gpubk/bin/python -m pip install --upgrade 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install --yes
bk doctor --probe --require-monitor --strict
```

升级会保留数据和策略。涉及删除或所有权变更时，应先使用 `--dry-run` 预览。

## 详细文档

- [中文管理员与用户完整手册](https://github.com/lzzmm/gpubk/blob/main/docs/GUIDE.zh-CN.md)
- [English complete guide](https://github.com/lzzmm/gpubk/blob/main/docs/GUIDE.md)
- [升级说明](UPGRADING.md)
- [安全模型](SECURITY.md)
- [多机部署](CLUSTER.md)
- [监测数据格式](TELEMETRY.md)
- [发布流程](RELEASING.md)

本项目采用 [Apache-2.0](LICENSE) 许可证。
