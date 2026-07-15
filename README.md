# GPUBK

**English** | [简体中文](https://github.com/lzzmm/gpubk/blob/main/README.zh-CN.md)

GPUBK is a lightweight GPU reservation and usage tool for shared Linux servers.
Users book GPUs with the short `bk` command or an interactive terminal timeline;
administrators get atomic storage, access control, monitoring, and audit history.

## Highlights

- Shared or exclusive reservations, automatic placement, and configurable time slots.
- Live GPU, process, memory, and utilization views without a web service.
- Scheduled commands with automatic `CUDA_VISIBLE_DEVICES` selection.
- Local, versioned storage with atomic writes, backups, and UID-based authorization.
- CLI, curses TUI, JSON output, MCP tools, and multi-host expansion support.

GPUBK is a cooperative scheduler. Linux device permissions remain the final
enforcement boundary.

## Install

Python 3.10 or newer is required.

For one user or a quick evaluation:

```bash
python3 -m pip install 'gpubk[gpu]'
bk tutorial
```

For a shared GPU server, run this once as an administrator:

```bash
sudo python3 -m venv /opt/gpubk
sudo /opt/gpubk/bin/python -m pip install --upgrade pip
sudo /opt/gpubk/bin/python -m pip install 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install
bk doctor --probe --require-monitor --strict
```

The guided installer creates the system command, data directories, and boot
services. Ordinary users run `bk` without `sudo` and cannot edit another UID's
reservations or administrator policy.

## Everyday Use

```bash
bk                 # status and interactive prompt
bk 1 30m           # reserve one GPU for 30 minutes
bk 2 1h30m 12g     # two GPUs, 90 minutes, 12 GiB per GPU
bk x 1 2h           # exclusive reservation
bk a                # guided reservation
bk l                # list your reservations
bk g                # suggest a GPU available now
bk run -- python train.py
bk t                # visual terminal timeline
bk u                # your usage summary
```

Run `bk -h`, `bk help COMMAND`, or `bk tutorial` whenever you need guidance.

## Administration

```bash
sudo /opt/gpubk/bin/python -m pip install --upgrade 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install --yes
bk doctor --probe --require-monitor --strict
```

The installer preserves data and policy during upgrades. Preview destructive or
ownership-changing operations with `--dry-run` first.

## Documentation

- [Complete administrator and user guide](https://github.com/lzzmm/gpubk/blob/main/docs/GUIDE.md)
- [中文完整手册](https://github.com/lzzmm/gpubk/blob/main/docs/GUIDE.zh-CN.md)
- [Upgrading](UPGRADING.md)
- [Security model](SECURITY.md)
- [Cluster deployment](CLUSTER.md)
- [Telemetry format](TELEMETRY.md)
- [Release process](RELEASING.md)

Licensed under [Apache-2.0](LICENSE).
