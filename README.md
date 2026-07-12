# GPUbk

**English** | [简体中文](https://github.com/lzzmm/gpubk/blob/main/README.zh-CN.md)

GPUbk is a GPU booking tool for shared Linux servers. The package is named
`gpubk`; the command is the shorter `bk`.

It works offline, stores data in local files, and has no required runtime
dependencies. Users can book GPUs from a plain terminal prompt, a curses TUI,
JSON commands, or an optional local MCP server.

## What It Covers

- Shared and exclusive reservations in configurable intervals (5 minutes by default).
- Automatic queueing, live GPU awareness, and per-GPU VRAM budgets.
- A compact timeline that works on dark and light terminals.
- Scheduled commands with automatic `CUDA_VISIBLE_DEVICES`.
- NVML process monitoring and recent-load history.
- Stable JSON, MCP tools, a bundled Codex Skill, and an optional external allocator.
- Atomic file transactions, UID ownership checks, backups, and an append-only audit log.

GPUbk is a cooperative scheduler. It does not replace Linux device permissions
or stop a user with direct access to `/dev/nvidia*` from bypassing the tool.

## Install

GPUbk requires Python 3.10 or newer.

```bash
python3 -m pip install gpubk          # core CLI and TUI; no dependencies
python3 -m pip install 'gpubk[gpu]'  # add low-overhead NVML telemetry
python3 -m pip install 'gpubk[mcp]'  # add the local MCP server
python3 -m pip install 'gpubk[all]'  # both optional extras
```

Verify the installation:

```bash
bk --version
bk --help
```

Published wheels work with distribution-provided installers. Before installing
from a source checkout or source archive, upgrade pip in that environment:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install .
```

Some older Debian/Ubuntu pip builds ignore the isolated setuptools requested by
`pyproject.toml` and silently create an unusable `UNKNOWN` package. GPUbk detects
that condition and fails with an upgrade hint instead.

## Book GPUs

Shared mode is the default:

```bash
bk 1 30m                         # one GPU for 30 minutes
bk 2 1h30m --mem 12g            # 12 GiB expected VRAM per GPU
bk 1 1h --share 1/2             # reserve half of the shared capacity
bk 1 1h --share-with 1          # leave room for at most one minimum-share booking
bk s 1 2h --gpu 3               # explicit shared mode on GPU 3
bk x 2 4h                        # exclusive mode
bk 1 1h --at +30m                # human-friendly relative time
bk 1 1h --at "tomorrow 09:00"   # local wall-clock time
bk 1 1h --start 2030-01-01T20:00:00+08:00  # exact machine time
```

Manage your reservations with a list number or short ID:

```bash
bk l
bk e 1 --duration 2h
bk e 1 --at "tomorrow 09:00"
bk d 1
bk lg --limit 100                # recent operations for this UID
bk lg --limit 20 --json          # bounded machine-readable audit events
bk config                         # inspect effective configuration and policy
bk doctor                         # read-only ledger checks
```

Scheduling rules are intentionally small:

- Start times and durations use the server's configured booking boundary.
- Without `--at` or `--start`, GPUbk starts in the active booking interval when possible
  (`12:41` starts at `12:40`) and prints `queued:` when it must start later.
- `--at` accepts `+30m`, `20:00`, `tomorrow 09:00`, or `07-13 20:00`.
  `--start` keeps exact ISO 8601 input for scripts and Agents. Either is exact;
  a conflict returns an error instead of silently moving the reservation.
- Each GPU has `max_shared_users` capacity units. A shared booking uses one unit
  by default; `--share 3/4`, `--share 3`, and an exact percentage select a
  larger portion. `--share-with 1` reserves all but one unit. Capacity is checked
  independently in every overlapping booking interval.
- Share units control admission and inferred VRAM, not hardware-enforced SM
  bandwidth. Use MIG/MPS or device controls when physical partitioning is required.
- Exclusive reservations cannot overlap anything.
- `--mem` is expected VRAM **per GPU**. Administrators can require it for all
  shared reservations.
- Times shown to users are local. The ledger stores UTC.

Automatic placement considers reservations, physical free VRAM, current GPU
processes, recent load, and near-future booking pressure. A process without a
reservation is reported and avoided when another suitable GPU is free.

## Inspect and Find Capacity

The plain CLI is designed for the common path:

```bash
bk st                              # compact live status
bk st -v                           # include processes and all reservations
bk st --timeline                   # append the default timeline
bk tl                              # current booking interval, next 2 hours
bk tl 8h --step 15m --gpu 0,1
bk tl --from 20:00 --window 1d --step auto
bk slots 2 1h --mem 12g            # read-only placement alternatives
bk slots x 1 30m --limit 3
```

Timeline cells have fixed width: `··` is free; `M1`-`M9` is total shared
capacity used in a slice that includes one of your bookings; `S1`-`S9` is total
shared capacity used only by others; and `MX`/`XX` are exclusive bookings.
Narrow terminals
wrap the timeline at whole-hour boundaries without reducing the requested
resolution.

`bk add` and a flag-free `bk edit ID` are recoverable guided flows. They accept
the same natural time forms, re-prompt an invalid field, support `back` and
`cancel`, and show a local-time change summary before writing. `bk slots` is
read-only and prints a copyable command for its first option.

## Terminal Interfaces

`bk` opens a normal line-oriented prompt and keeps the terminal background.
`bk t` opens the full-screen TUI.

```bash
bk
bk t
```

Useful TUI keys:

| Key | Action |
| --- | --- |
| `a` / `e` / `d` | Add, edit, or cancel a reservation |
| `Tab`, `↑`, `↓` | Move between reservations and GPU details |
| `←`, `→` | Browse the timeline; move time in Add/Edit |
| `Space` | Toggle the current GPU in Add/Edit |
| `-`, `=` | Change timeline zoom |
| `[`, `]` | Shorten or extend duration by one configured booking interval |
| `,`, `.` | Quickly shorten or extend duration; step follows zoom |
| `v` | Cycle adjustment speed through 1x, 6x, and 24x |
| `Shift` + adjustment | Use a larger step when the terminal reports it |
| `1`-`9` | Pick a GPU count and jump to the nearest valid slot |
| `s`, `x` | Switch between shared and exclusive in Add/Edit |
| `u` | Set shared capacity as units, a fraction, or a percentage |
| `f`, `g` | Find any suitable GPUs, or keep the selected GPUs fixed |
| `n` | Return to the live `NOW` window |
| `c` | Toggle the dark/light theme |
| `?` | Open the paged help and quick tour |
| `Enter`, `Esc`, `q` | Submit, cancel the current action, or quit |

The timeline can show past reservations, but history is read-only. Add and Edit
always validate the selected interval again inside the locked scheduler
transaction. Reservation focus starts on the header, so no booking blinks until
you press Down. For servers with up to ten GPUs, the `GPU` column keeps one
fixed position per device and shows only the numbers used by that reservation;
empty positions stay blank. Reservation IDs use the shortest unique prefix from
six characters upward, so the table, share details, and process links agree.

## Run a Command at Reservation Time

Put the command after `--`:

```bash
bk 2 1h30m --mem 12g -- python train.py --config exp.yaml
bk j                    # list scheduled jobs
bk j --cleanup          # inspect and prune private job files
bk w                    # run this user's due jobs
bk jr ID --accept-duplicate-risk  # retry only after checking an uncertain job
```

The worker sets `CUDA_VISIBLE_DEVICES`, `CUDA_DEVICE_ORDER`,
`BK_RESERVATION_ID`, and `BK_RESERVED_GPUS`. Commands and working directories
stay in UID-owned `0600` job specs; they are not written to the shared ledger.
The worker uses `shell=False` and supervises the command's process group until
it exits or the reservation ends. Commands must not daemonize or create a new
session. Use an explicit shell only when shell syntax is required:

```bash
bk 1 30m -- sh -lc 'python train.py > train.log 2>&1'
```

Immediately before launch, the worker samples every assigned GPU again. An
exclusive job waits for all non-system processes to leave; a shared job allows
authorized sharers but waits on unreserved/unknown processes or insufficient
physical VRAM. Missing live telemetry also fails closed. The job remains
`pending` with a visible reason and is retried by the continuous worker until
the reservation ends; `bk worker --once` returns `3` when work is waiting.
`worker_live_guard=false` disables this protection and should only be used for
an explicitly accepted compatibility case.

Only one worker can hold a UID's private job directory. The kernel releases its
lease after a crash, so the next worker can recover durable `claimed` or
`running` records without racing a healthy worker. On Linux, recovery reads only
same-UID `/proc` entries, matches the exact `BK_RESERVATION_ID`, rechecks identity
immediately before signalling, then sends TERM and KILL after the configurable
`worker_recovery_grace_seconds` (default 5 seconds). Recovered jobs remain
`uncertain`, even after their process groups stop, because partial side effects
may already exist. Retry requires the explicit duplicate-risk acknowledgement.
Processes recorded on another host are never signalled locally. A concurrent
worker exits with status `75`; the bundled systemd unit does not restart-loop on
that status. During an upgrade, active jobs created by a pre-lease worker are
left untouched and block new claims until they finish or their reservation ends.

Private command specs are removed after cancellation, success, timeout, or an
expired retry window. The worker checks them at startup, after shutdown, and at
most every five minutes while running. A spec with no ledger reference gets a
24-hour grace period so cleanup cannot race a concurrent booking. Failed,
interrupted, uncertain, pending, claimed, and running jobs keep their specs
while they can still run or be retried. `bk jobs --cleanup --json` exposes the
same cleanup as a machine-readable operation. Private job logs are deliberately
kept outside shared data. Direct stdout/stderr is drained through a two-segment
rolling log capped at 64 MiB per job by default. Terminal logs are kept for 30
days and oldest terminal logs are pruned if this UID exceeds 4 GiB. Active and
retryable jobs are retained. `bk jobs --cleanup --json` reports spec and log cleanup;
`job_log_retention_days`, `job_log_max_mb`, and `job_log_total_max_mb` configure
the policy, and `0` disables the corresponding limit. Files created by a command
itself, including shell redirections, are outside this policy.

For unattended jobs, each user can install the bundled systemd user unit:

```bash
bk service install worker
systemctl --user daemon-reload
systemctl --user enable --now bk-worker.service
```

The generated unit captures the absolute `BK_DATA_DIR`, private
`BK_JOB_LOG_DIR`, and an explicit `BK_CONFIG_FILE` in effect at installation
time. Review it with `bk service show worker`; reinstall with `--force` after
changing any captured path.
Every worker invocation for one UID must use that same private path so the lease
has one authority. Other policy is reloaded from the selected configuration path
whenever the service starts.

## Monitoring and Placement

Install the `gpu` extra, then run a single sample or a low-overhead monitor:

```bash
bk m --once
bk m
bk u                              # this UID, last 24 hours
bk u users --since 30d           # visible per-user summaries
bk u samples --since 2d --resolution 5m --json
bk u events --user me --since 7d
```

NVML is initialized once and device handles are reused. The monitor records
bounded scheduling load plus sparse per-user history and process start, stop,
authorization, and workload changes. It does not append a full snapshot every
second. Without NVML, GPUbk falls back to `nvidia-smi` with less process detail.
The default cadence is a 2-second sample folded into 60-second records. Set
`monitor_interval_seconds` and `monitor_rollup_seconds` in the trusted config to
tune this for the server; the rollup must be an exact multiple of the sample
interval. Command-line `--interval` and `--rollup` values override one run.

Process status is based on the process UID and active reservation:
`ok`, `wrong-gpu`, `unreserved`, `unknown`, or `system`. Command lines are
reduced to safe labels before shared logging.

History is stored in checksummed daily partitions with 1-minute, 5-minute,
10-minute, hourly, and daily levels. The public `gpubk.usage.v1` query model is
available through Python, JSON CLI, and MCP; visualizers should not parse storage
files. See [Telemetry](https://github.com/lzzmm/gpubk/blob/main/TELEMETRY.md).
`usage_load_window_minutes` controls how much recent device history is retained
and considered by automatic GPU placement.

The monitor also has a user service:

```bash
bk service install monitor
systemctl --user daemon-reload
systemctl --user enable --now bk-monitor.service
```

Run exactly one trusted monitor writer on a shared server. Per-user workers are
still separate. The monitor service above is intended for a private server or
for the one account selected by the administrator. Its generated unit captures
the absolute shared data directory. A second monitor fails with exit status 75;
systemd does not restart that duplicate instance in a loop.

## Agents and MCP

Agents should use the versioned JSON interface instead of parsing terminal text:

```bash
bk agent context --compact
bk agent recommend 2 1h30m --mem 12g --compact
bk 2 1h30m --mem 12g --share 1/2 --op-id run-20260712-001 --json
bk agent edit 6e957ef1 --duration 2h --op-id edit-20260712-001 --compact
bk agent cancel 6e957ef1 --compact
```

Create and edit operations require a stable operation ID. An identical retry
returns `status=exists`; reusing the ID for a different write is rejected.
Recommendations are read-only. Identity always comes from the local process UID.

Run the optional stdio MCP server with:

```bash
bk-mcp                       # same as: bk mcp
bk skill install            # installs the bundled Codex Skill
```

The MCP server provides context, recommendation, create, list, edit, cancel,
private-spec cleanup, and private job-log tools. It listens on stdio only; each
user runs their own process. Tool schemas include read-only, idempotent,
destructive, and closed-world annotations.

An administrator may also set `BK_ALLOCATOR_COMMAND` to a trusted local program
that reads `bk.allocator.v1` JSON and returns a GPU ordering. Its output is
advisory: every result still passes the built-in conflict, VRAM, time, UID, and
transaction checks. See the [Agent protocol](https://github.com/lzzmm/gpubk/blob/main/src/bk/data/codex-skill/gpubk/references/protocol.md).

## Shared Server Setup

Create one setgid directory for the lab group:

```bash
sudo install -d -m 2770 -o root -g gpuusers /data2/shared/bk
sudo install -d -m 0755 -o root -g root /etc/gpubk
export BK_DATA_DIR=/data2/shared/bk
export BK_CONFIG_FILE=/etc/gpubk/config.json
```

Put the root-owned configuration at `/etc/gpubk/config.json`, outside the
group-writable ledger directory:

```json
{
  "config_version": 1,
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
  "file_mode": "0660",
  "dir_mode": "2770"
}
```

```bash
sudo chown root:root /etc/gpubk/config.json
sudo chmod 0644 /etc/gpubk/config.json
```

The configuration file and every directory that contains it must be owned by
root or the current UID and must not be writable by group or other users. A
root-owned file inside `/data2/shared/bk` is still replaceable by members who
can write that directory, regardless of the file's `0644` mode. GPUbk therefore
opens the configured parent chain and file by descriptor and rejects that layout.
For a single-user installation, the backward-compatible default remains
`$BK_DATA_DIR/config.json`; `BK_CONFIG_FILE` is required only when selecting a
separate file.

`max_shared_users` is retained as the compatible configuration name; it now
defines whole shared capacity units per GPU. Old reservations without a
`share_units` field consume one unit.

`slot_minutes` controls booking start and duration granularity. It defaults to
`5` and may be any divisor of one hour from 1 through 60. `BK_SLOT_MINUTES`
overrides it for single-user or test setups. On a shared server, keep this in the
trusted root-owned file: the ledger binds the value on first write and rejects
clients using another grid.

Inspect the resolved settings without creating or changing data:

```bash
bk config
bk config --json
```

Environment variables override file values, and a command flag overrides the
corresponding default for that invocation. New files should declare
`"config_version": 1`; unversioned files remain readable for compatibility.
Unknown keys, wrong types, non-finite numbers, unsafe paths, and excessive
values are rejected instead of ignored. The JSON report lists active override
names but never prints the external allocator command.

Scheduling policy, retention, worker timing, allocator integration, and display
defaults are configurable. Schema versions, transaction durability, path and
permission checks, record-size limits, and other corruption defenses are fixed
implementation safeguards rather than administrator tuning knobs.

All users and user services must use the same `BK_DATA_DIR` and
`BK_CONFIG_FILE`. The first write binds scheduling and storage policy into the
ledger. Clients with conflicting settings fail closed. Run the deployment
preflight before enabling services:

```bash
BK_DATA_DIR=/data2/shared/bk BK_CONFIG_FILE=/etc/gpubk/config.json \
  bk doctor --probe --strict
BK_DATA_DIR=/data2/shared/bk BK_CONFIG_FILE=/etc/gpubk/config.json \
  bk doctor --probe --json --strict
```

The probe creates randomly named temporary files, verifies same-directory atomic
replace and directory fsync, checks same-host cross-process `flock`, confirms
configured modes and free space, probes the real GPU telemetry source, and then
removes its files. A simulation or `nvidia-smi` fallback is a strict-mode warning.
In JSON, `healthy` covers read-only ledger checks; `ready` remains `null` until
`--probe` supplies deployment evidence.
Plain `doctor` never initializes storage, acquires a lock, recovers a pending
transaction, or follows a symbolic link or hard-linked alias at a managed path.
It also reports permission drift; write commands fail closed instead of silently
changing modes. Only explicit `--probe` writes temporary files.
For NFS/FUSE used by multiple hosts, additionally verify locking from a second
host because one machine cannot prove cross-host lock propagation. Every writer
must use GPUbk.

See [SECURITY.md](https://github.com/lzzmm/gpubk/blob/main/SECURITY.md) for the supported boundary, file safety, WAL
recovery, private job specs, MCP isolation, and administrator responsibilities.

## Try It Without a GPU

Booking and the TUI can run with simulated GPU count:

```bash
BK_DATA_DIR=/tmp/gpubk-demo BK_GPU_COUNT=4 BK_MAX_SHARED_USERS=4 bk t
BK_DATA_DIR=/tmp/gpubk-demo BK_GPU_COUNT=4 BK_MAX_SHARED_USERS=4 bk 1 30m --share 3/4
```

The cards show unknown hardware metrics, but scheduling, shared capacity, the
timeline, Add/Edit, logs, and Agent JSON remain usable.

## Development

```bash
python3 -m pip install -e '.[mcp,gpu]'
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
PYTHONPATH=src python3 benchmarks/scheduler_queue.py
PYTHONPATH=src python3 benchmarks/usage_store.py
```

Project documents: [Security](https://github.com/lzzmm/gpubk/blob/main/SECURITY.md) ·
[Upgrading](https://github.com/lzzmm/gpubk/blob/main/UPGRADING.md) ·
[Release process](https://github.com/lzzmm/gpubk/blob/main/RELEASING.md) ·
[Changelog](https://github.com/lzzmm/gpubk/blob/main/CHANGELOG.md) ·
[Apache-2.0 license](https://github.com/lzzmm/gpubk/blob/main/LICENSE)
