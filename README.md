# GPUBK

**English** | [简体中文](https://github.com/lzzmm/gpubk/blob/main/README.zh-CN.md)

<p align="center">
  <img src="docs/assets/gpubk-terminal-logo.svg" alt="GPUBK terminal logo" width="860">
</p>

**GPU time, without spreadsheet time.**

GPUBK is a lightweight GPU reservation and usage tool for shared Linux servers.
Users book GPUs with the short `bk` command or an interactive terminal timeline;
administrators get atomic storage, access control, monitoring, and audit history.

[Install](#install) · [Everyday use](#everyday-use) ·
[Shared server](#shared-server) · [Multiple servers](#multiple-servers) ·
[Documentation](#documentation)

## Highlights

- Shared or exclusive reservations, automatic placement, and configurable time slots.
- Live GPU, process, memory, and utilization views without a web service.
- Container-aware attribution with explicit verified, inferred, or ambiguous ownership.
- Scheduled commands with automatic `CUDA_VISIBLE_DEVICES` selection.
- Local, versioned storage with atomic writes, backups, and UID-based authorization.
- CLI, curses TUI, JSON output, MCP tools, and multi-host expansion support.

GPUBK is a cooperative scheduler. Linux device permissions remain the final
enforcement boundary.

## Install

Python 3.10 or newer is required.

### Quick evaluation

Install into your own Python environment. This does not create system services or
change `/etc`:

```bash
python3 -m pip install 'gpubk[gpu]'
bk --version
bk tutorial
```

Try the scheduler without a GPU:

```bash
BK_DATA_DIR=/tmp/gpubk-demo BK_GPU_COUNT=4 bk t
```

### Shared server

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

Successful deployment has two active services and a ready preflight:

```bash
sudo systemctl status gpubk-broker gpubk-monitor
bk doctor --probe --require-monitor --strict
```

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

## Shared Server

The broker is the only writer of the protected ledger. The monitor samples NVML
telemetry, while CLI and TUI clients connect through a local Unix socket. Both
services start automatically after reboot. GPUBK does not create a Linux user or
group by default; the administrator who installs it owns the deployment.

Upgrade an existing installation without deleting policy or history:

```bash
sudo /opt/gpubk/bin/python -m pip install --upgrade 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install --yes
bk doctor --probe --require-monitor --strict
```

The installer preserves data and policy during upgrades. Preview destructive or
ownership-changing operations with `--dry-run` first.

## Multiple Servers

Cluster mode federates independently safe GPUBK hosts. Every GPU server keeps its
own broker, ledger, monitor, and authority; the client compares hosts over
host-key-verified SSH and books exactly one host. No central database or new network
daemon is required, and a reservation never spans hosts.

1. Install and validate GPUBK normally on every GPU server.
2. Configure non-interactive SSH with verified host keys for participating users.
3. Build the root-owned client catalog and verify every route.

```bash
sudo bk admin cluster init gpu-a --yes
bk c probe gpu-b gpu-b
# Review and run the exact `sudo bk admin cluster add ...` command it prints.
bk c check
bk c rec 2 1h
bk c 2 1h
```

On a login node, omit `cluster init` and add each GPU host with `bk c probe`.
See [CLUSTER.md](CLUSTER.md) before production use; it covers per-node identity,
UID mapping, failure behavior, NFS history export, and scheduled jobs.

## Documentation

- [Complete administrator and user guide](https://github.com/lzzmm/gpubk/blob/main/docs/GUIDE.md)
- [中文完整手册](https://github.com/lzzmm/gpubk/blob/main/docs/GUIDE.zh-CN.md)
- [Upgrading](UPGRADING.md)
- [Security model](SECURITY.md)
- [Cluster deployment](CLUSTER.md)
- [Telemetry format](TELEMETRY.md)
- [Release process](RELEASING.md)

Licensed under [Apache-2.0](LICENSE).
