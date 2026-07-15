# GPUBK

**English** | [简体中文](https://github.com/lzzmm/gpubk/blob/main/README.zh-CN.md)

GPUBK is a GPU booking tool for shared Linux servers. The package is named
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

GPUBK is a cooperative scheduler. It does not replace Linux device permissions
or stop a user with direct access to `/dev/nvidia*` from bypassing the tool.

## Install

GPUBK requires Python 3.10 or newer.

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
bk tutorial
```

That is enough for a private installation: run `bk` immediately. A shared
server needs one administrator initialization, described below; ordinary users
still only run `bk`.

Published wheels work with distribution-provided installers. Before installing
from a source checkout or source archive, upgrade pip in that environment:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install .
```

Some older Debian/Ubuntu pip builds ignore the isolated setuptools requested by
`pyproject.toml` and silently create an unusable `UNKNOWN` package. GPUBK detects
that condition and fails with an upgrade hint instead.

## First Five Minutes

The tutorial is read-only: it explains commands using the active server policy
but never creates or changes a reservation.

```bash
bk tutorial          # replayable, line-oriented walkthrough
bk tutorial --tui    # visual timeline and keyboard tour
```

The first plain `bk` prompt prints one short tutorial hint. The first `bk t`
launch opens the TUI tour automatically. These two reminders are recorded only
in the current user's `XDG_STATE_HOME` (normally `~/.local/state/bk`); they do
not touch the shared ledger and do not affect other users. Both tours remain
available after the reminders have been dismissed.

A normal first session is:

```bash
bk info              # administrator account and contact
bk login             # current and next reservation, without GPU probing
bk g                 # GPU you can use now, or one read-only suggestion
bk slots 1 30m       # preview choices, no write
bk 1 30m             # book the earliest suitable shared GPU
bk st                # check live state
bk l                 # list your reservations
bk e 1               # guided edit with input recovery
bk d 1               # cancel by list number or short ID
bk t                 # use the visual timeline
```

## Book GPUs

Shared mode is the default:

```bash
bk 1 30m                         # one GPU for 30 minutes
bk book 1 30m                    # equivalent explicit command form
bk 2 15m 5g                     # shorthand: 5 GiB expected VRAM per GPU
bk 2 1h30m -m 12g               # 12 GiB expected VRAM per GPU
bk 1 1h -s 2                    # request two integer shared slots per GPU
bk s 1 2h -g 3                  # explicit shared mode on GPU 3
bk 1 1h -e 2,3                  # automatic placement, except GPUs 2 and 3
bk x 2 4h                        # exclusive mode
bk 1 1h -t +30m                 # human-friendly relative time
bk 1 1h --at "tomorrow 09:00"   # local wall-clock time
bk 1 1h --start 2030-01-01T20:00:00+08:00  # exact machine time
```

Use `bk COMMAND --help` or `bk help COMMAND` for contextual help. Help never
opens the guided form, full-screen TUI, or MCP stdio server.

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

The stored reservation ID is a standard 36-character UUID. The CLI normally
shows eight characters; the space-constrained TUI starts with the shortest
unique six-character prefix and expands it on collisions. Either prefix can be
used by `bk e`, `bk d`, or `bk run` while it is unique. GPUBK rejects ambiguous
prefixes instead of guessing.

Scheduling rules are intentionally small:

- Start times and durations use the server's configured booking boundary.
- Without `--at` or `--start`, GPUBK starts in the active booking interval when possible
  (`12:41` starts at `12:40`) and prints `queued:` when it must start later.
- `--at` accepts `+30m`, `20:00`, `tomorrow 09:00`, or `07-13 20:00`.
  `--start` keeps exact ISO 8601 input for scripts and Agents. Either is exact;
  a conflict returns an error instead of silently moving the reservation.
- An exact new booking may use the current slice boundary or a future boundary. An
  earlier historical slice is rejected; retrying an already-applied operation ID still
  returns the original reservation.
- Each GPU has `max_shared_users` integer shared slots. A shared booking uses one
  slot by default; `--share 3` requests three slots on every selected GPU.
  Capacity is checked independently in every overlapping booking interval.
- Shared slots control admission and inferred VRAM, not hardware-enforced SM
  bandwidth. Use MIG/MPS or device controls when physical partitioning is required.
- Exclusive reservations cannot overlap anything.
- Positional `5g` and `--mem 5g` both mean expected VRAM **per GPU**. When it is
  omitted, GPUBK uses a share-weighted estimate; administrators can require an
  explicit value for all shared reservations.
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
bk tl --from "2026-07-12T09:00:00+08:00" --window 4h
bk slots 2 1h --mem 12g            # read-only placement alternatives
bk slots x 1 30m --limit 3
```

Timeline cells have fixed width: `··` is free; `M1`-`M9` is total shared
capacity used in a slice that includes one of your bookings; `S1`-`S9` is total
shared capacity used only by others; and `MX`/`XX` are exclusive bookings.
Narrow terminals
wrap the timeline at whole-hour boundaries without reducing the requested
resolution. A past `--from` is read-only and includes retained expired
reservations; cancelled reservations remain hidden.

When the current UID has a pending, claimed, or running scheduled command,
`bk st` also reports the private worker's instance-bound, kernel-proven state
and warns if the command cannot launch. Terminal jobs and ordinary reservations
do not trigger that private-directory probe.

`bk add` and a flag-free `bk edit ID` are recoverable guided flows. They accept
the same natural time forms, re-prompt an invalid field, support `back` and
`cancel`, and show a local-time change summary before writing. An edit cannot
target a reservation that has started or supply a start in the past. `--queue`
may resolve a resource conflict after a valid start, but never repairs an
invalid past time. `bk slots` is read-only and prints a copyable command for its
first option.

## Terminal Interfaces

`bk` opens a normal line-oriented prompt and keeps the terminal background.
`bk t` opens the full-screen TUI.

```bash
bk
bk t
bk tutorial --tui
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
| `1`-`9` | Pick a GPU count and find the earliest valid slot from the search start |
| `s`, `x` | Switch between shared and exclusive in Add/Edit |
| `u` | Set the integer shared slots requested per GPU |
| `f` | Find the earliest suitable GPUs from `NOW` or the manually selected start |
| `o` | Find the nearest valid slot around the current cursor |
| `g` | Find the earliest slot while keeping the selected GPUs fixed |
| `n` | Return to the live `NOW` window |
| `c` | Toggle the dark/light theme |
| `z` | Toggle capacity-sliced and solid-first shared bars |
| `?` | Open the paged help and quick tour |
| `u` | Open your sampled GPU-use dashboard |
| `Enter`, `Esc`, `q` | Submit, cancel the current action, or quit |

The TUI refreshes once per second by default. Set `tui_refresh_seconds` in the
configuration, or `BK_TUI_REFRESH_SECONDS` for one environment, when a slower
terminal or lower polling rate is preferred.
The header spells out the shared telemetry collector and this UID's scheduled-command
worker state. `worker=idle` means no current reservation needs automatic execution;
worker state is checked read-only and at most once every ten seconds
while a runnable command exists. Press `r` to invalidate both monitor and worker
status caches immediately.

The timeline can show past reservations, but history is read-only. Add and Edit
always validate the selected interval again inside the locked scheduler
transaction. Reservation focus starts on the header, so no booking blinks until
you press Down. For servers with up to ten GPUs, the `GPU` column keeps one
fixed position per device and shows only the numbers used by that reservation;
empty positions use the same muted dots as the timeline. Reservation IDs use the shortest unique prefix from
six characters upward, so the table, share details, and process links agree.

## Run a Command at Reservation Time

Put the command after `--`:

```bash
bk run -- python train.py              # run on an active reservation now
bk run 1 30m -- python train.py        # book the earliest GPU and run now/later
bk run 1 30m --exclude 2,3 -- COMMAND  # avoid selected GPUs
bk 2 1h30m --mem 12g -- python train.py --config exp.yaml
bk j                    # list scheduled jobs
bk j --cleanup          # inspect and prune private job files
bk w                    # read-only worker liveness check
bk w start              # run this user's scheduled-command worker
bk w once               # process due work once, then exit
bk jr ID --accept-duplicate-risk  # retry only after checking an uncertain job
```

Plain `bk run` is read-only: it shows GPUs from current reservations or the next
one-GPU suggestion. Immediate commands use stable GPU UUIDs when available and
are stopped at the earliest selected reservation end. Plain `bk w` is deliberately
read-only. Use `bk w start` for the foreground worker, `bk w once` for one pass,
or the full `bk worker` command in a service.

The worker sets `CUDA_VISIBLE_DEVICES`, `CUDA_DEVICE_ORDER`,
`BK_RESERVATION_ID`, and `BK_RESERVED_GPUS`. With the default live guard,
`CUDA_VISIBLE_DEVICES` uses the stable GPU UUIDs from the exact NVML snapshot
that passed launch validation; `BK_RESERVED_GPUS` keeps the user-facing numeric
GPU slots. This avoids assuming that NVML indices equal CUDA ordinals. A real
NVML device without a valid stable identifier remains pending instead of being
launched on a guessed device. Numeric launch tokens remain only for simulation
and the explicitly unsafe `worker_live_guard=false` compatibility path.
Commands, working directories, and the submitting process's `PATH` stay in
UID-owned `0600` job specs; they are signed by the digest and are not written to
the shared ledger. Capturing `PATH` keeps a bare command such as `python` bound
to the same search path when a systemd worker starts outside the interactive
shell. GPUBK deliberately captures no other environment variable. Load project
variables and credentials from a user-owned wrapper or configuration file.
Changing `PATH` while retrying the same operation ID is a different command
intent and is rejected. Existing v1 private specs remain readable.
On a real GPU host, guarded scheduled commands require the `gpu` extra: the
`nvidia-smi` fallback has no trustworthy process list and therefore stays
pending rather than guessing that a device is free.
The worker uses `shell=False` and supervises the command's process group until
it exits or the reservation ends. Commands must not daemonize or create a new
session. Use an explicit shell only when shell syntax is required:

```bash
bk 1 30m -- sh -lc 'python train.py > train.log 2>&1'
```

To keep adjacent reservations from overlapping, the worker sends TERM before
the reservation ends and sends KILL at the exact end if the process group is
still alive. `worker_termination_grace_seconds` controls this warning window
(default 5 seconds, allowed 0.1 through 60). Cancellation and worker shutdown
use the same grace after the event instead. Commands should handle TERM for
checkpointing; the grace is inside the booked interval, not extra GPU time.

Immediately before launch, the worker samples every assigned GPU again. An
exclusive job waits for all non-system processes to leave; a shared job allows
authorized sharers but waits on unreserved/unknown processes or insufficient
physical VRAM. Missing live telemetry also fails closed. The job remains
`pending` with a visible reason and is retried by the continuous worker until
the reservation ends; `bk worker --once` returns `3` when work is waiting.
`worker_live_guard=false` disables this protection and should only be used for
an explicitly accepted compatibility case.

The worker can start multiple due commands concurrently, including legal shared
reservations on the same GPU. Its effective limit is the smaller of
`worker_max_parallel` (default 64) and `gpu_count * max_shared_users`; this keeps
normal shared capacity from being accidentally serialized while retaining an
administrator-controlled process safety cap. `bk worker --max-parallel N`
overrides the cap for that invocation. `bk config` reports both values.

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
If the current worker itself loses ledger access while supervising a command,
its final process-group KILL and reap no longer depends on another successful
ledger read. The unchanged durable job state is intentionally recovered as
`uncertain` after restart rather than being reported as completed or retried.
Worker startup, every polling cycle, and every locked worker transaction validate
the ledger-bound policy. A mismatch exits with status `78` before acquiring a
private lease at startup. Runtime drift stops and reaps supervised commands but
does not reconcile or clean shared state under the wrong policy.

`bk worker --status` reports `running`, `stopped`, `not-seen`, `other-instance`,
`unverified`, or an unsafe/unavailable state without creating or modifying
private storage. `running` requires both the global kernel lock and a
privacy-safe, digest-named instance lock for the configured data directory. The
recorded PID, hostname, acquisition time, and digest text are diagnostic
metadata only. Add `--json` for `gpubk.worker.v1`, or `--require-running` to
return status 2 unless this exact instance's lease is actively held.
`bk jobs --json` and Agent/MCP context expose the same current-UID status.
Creating or editing a reservation with a scheduled command also checks this
lease immediately. Human output warns when no worker is proven running;
JSON/MCP `booking_result.worker` carries the same `gpubk.worker.v1` document
(`null` for reservations without a command). A successful reservation alone
does not prove that its command can launch unattended.

Private command specs are removed after cancellation, success, timeout, or an
expired retry window. The worker checks them at startup, after shutdown, and at
most every five minutes while running. A spec with no ledger reference gets a
24-hour grace period so cleanup cannot race a concurrent booking. Failed,
interrupted, uncertain, pending, claimed, and running jobs keep their specs
while they can still run or be retried. Creation, execution-time reads, and
deletion stay pinned to validated UID-owned private directory descriptors;
symbolic links and hard-linked spec files are rejected. An interrupted write
removes its partial file. If booking completion is ambiguous, GPUBK first
recovers and rereads the ledger, then removes only a spec that has no committed
reference. A stable operation ID also binds the full command digest and working
directory without putting command arguments in the shared ledger. `bk jobs
--cleanup --json` exposes the same cleanup as a machine-readable operation.
Private job logs are deliberately
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
bk doctor --require-worker --strict
```

On systemd Linux, the user manager may stop at logout and may not start at boot.
For genuinely unattended jobs, an administrator can selectively enable linger:

```bash
sudo loginctl enable-linger <worker-user>
```

The generated unit captures the absolute `BK_DATA_DIR`, private
`BK_JOB_LOG_DIR`, an explicit `BK_CONFIG_FILE`, and any explicitly active
non-secret configuration overrides such as `BK_WORKER_MAX_PARALLEL`. Values are
validated and normalized before they enter the unit; allocator commands are
never captured. Review the snapshot with `bk service show worker`; reinstall
with `--force` after changing any captured path or override.
Every worker invocation for one UID must use that same private path so the lease
has one authority. A worker serving another `BK_DATA_DIR` is reported as
`other-instance`, never as ready for the current ledger. Settings without a
captured override are reloaded from the selected configuration path whenever
the service starts. A trusted config file is preferable to shell overrides for
shared deployments.
Persistent worker startup failures are bounded to three attempts per 60 seconds;
ordinary child-command failures are recorded without terminating the long-lived
worker.

## Monitoring and Placement

Install the `gpu` extra, then run a single sample or a low-overhead monitor:

```bash
bk m --once
bk m
bk g                              # current booked GPU, otherwise one suggestion
bk u                              # this UID, last 24 hours
bk u users --since 30d           # visible per-user summaries
bk u samples --since 2d --resolution 5m --json
bk u events --user me --since 7d
```

`bk g` is the shortest read-only answer to "which GPU can I use?" If one of
your reservations is active, it prints those GPU IDs, remaining time, live
utilization, and free VRAM. Otherwise it recommends one legal 30-minute shared
placement using both the ledger and current telemetry. It never creates a
reservation; use the printed `bk 1 30m --gpu N` command to book it.

`bk u` reports sampled history only. Future reservations are excluded.
`Reserved` is past reservation time covered by monitor samples, while `Idle` is
the sampled part of that time with no GPU process attributed to the user. The
default personal view is a colored terminal dashboard with the last 7 days by
day and the last 4 weeks by week; pass `--no-chart` to omit trends. In the TUI,
press `u` for the same personal summary without leaving the timeline.

On a real GPU host, activate a CUDA PyTorch environment and run
`bk usage demo`. It checks the monitor, asks before booking one currently idle
GPU, runs a short low-duty workload, prints the resulting statistics, and
always attempts to cancel its reservation. Use `bk usage demo --yes` in an
approved non-interactive acceptance run.

NVML is initialized once and device handles are reused. A failed initialization
or stale device handle enters a short backoff and is rebuilt, so a transient
driver fault does not permanently degrade a long-running monitor. The monitor
records bounded scheduling load plus sparse per-user history and process start,
stop, authorization, and workload changes. It does not append a full snapshot
every second. Without NVML, GPUBK falls back to `nvidia-smi` for device metrics.
Because that fallback has no trustworthy process list, GPUBK preserves the last
observed process state and reports the telemetry gap instead of manufacturing
stop/start events. Process-list and per-process-utilization capabilities are
exposed in monitor warnings and Agent GPU details. The collector status tracks
stable CUDA device identifiers and numeric process-UID attribution independently.
The monitor also atomically updates a small `usage/collector.json` heartbeat.
Usage JSON, Agent context, `bk doctor`, and the TUI header expose the same states:
`running`, `degraded`, `stale`, `stopped`, or `topology-mismatch`. A crash becomes
`stale` after three missed heartbeats; a normal exit becomes `stopped`. In the
TUI, `monitor=ok` means a fresh collector and `monitor=not-seen` means no heartbeat
has been recorded.
Missing stable identifiers or an observed process whose numeric UID cannot be
resolved make the collector `degraded`, so post-start doctor verification cannot
pass while guarded scheduled commands would remain blocked. On hosts using
`hidepid` or containerized `/proc`, grant the selected monitor account only the
administrator-approved process-visibility group needed for attribution; do not
run the collector as root merely to clear the gap.
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

New history records also carry a versioned `gpubk.node` extension with a hashed
stable node ID, hostname, and stable GPU UUID when available. Legacy records remain
readable as node `legacy`; no raw machine ID is stored. This makes later export or
import preserve the difference between, for example, GPU 0 on two hosts. User
summaries already expose `nodes` and `(node_id, gpu)` devices, while capabilities
state the current topology support explicitly.

Every GPUBK ledger remains a single-host authority. Do not point independent brokers
or monitors at one NFS directory. Optional cluster federation instead queries each
host through non-interactive SSH and its versioned Agent JSON, then submits to exactly
one destination broker. Stable `(node_id, numeric UID)` pairs can be mapped by an
administrator to one global principal; usernames alone are never trusted as identity.
Existing history needs no rewrite.

Cluster controls remain hidden when no catalog exists. With a catalog, `bk c` shows
all nodes and active reservations, `bk c rec 2 1h` compares legal starts,
`bk c 2 1h` books the best single node, `bk c x 2 1h` does the same exclusively,
`bk c tui` opens the node/reservation browser, and `bk @NODE 2 1h` targets one node.
Use `bk c 1 2h -- python /absolute/path/train.py` to attach a scheduled command to
the automatically selected host. GPUBK-generated retry and JSON flags remain before
`--`; option-like workload arguments remain untouched after it. A mixed-version node
must advertise scheduled-job and private-spec support before it can receive this write.
Use executable and script paths valid on the destination: the remote non-interactive
SSH session supplies its working directory and `PATH`, not the caller's local directory.
Human cluster bookings repeat destination warnings with the node name. A `created`
reservation accompanied by a stopped or unseen worker warning will not execute its
command until that destination user's worker is started. JSON callers should inspect
`result.warnings` instead.
In the browser, `Tab` changes focus and `Enter` opens complete reservation details.
Older nodes remain visible during
rolling upgrades but are read-only until they advertise the required safe-write
capabilities.
See [CLUSTER.md](https://github.com/lzzmm/gpubk/blob/main/CLUSTER.md) for transport,
failure, NFS export, and rollout boundaries.

The monitor also has a user service:

```bash
bk service install monitor
systemctl --user daemon-reload
systemctl --user enable --now bk-monitor.service
bk doctor --require-monitor --strict
```

For boot and logout persistence, enable linger only for the selected monitor
account if the host requires it:

```bash
sudo loginctl enable-linger <monitor-account>
```

Run exactly one trusted monitor writer on a shared server. Per-user workers are
still separate. The monitor service above is intended for a private server or
for the account whose numeric UID is selected by `monitor_uid`. Its generated
unit captures the absolute shared data directory and explicit trusted config
path, plus any explicit non-secret configuration overrides active at install
time. Uncaptured sampling and rollup values are reloaded from that config whenever
the service starts. A duplicate writer (`75`), role mismatch (`77`), or ledger
policy mismatch (`78`) is not restarted. Other failures retry at most three times
in 60 seconds, allowing a short transient recovery without an endless log loop.
The final command above is a read-only post-start check. Unlike deployment
preflight, it fails when no collector heartbeat has ever been recorded.
A normal signal-driven exit publishes `stopped`. A fatal sampling or storage
error attempts to flush partial rollups but leaves the last `running`/`degraded`
heartbeat untouched so it becomes `stale`; the original nonzero failure remains
visible to systemd and the single-writer lease is still released.
The monitor validates policy before telemetry maintenance or GPU sampling on
every cycle. Policy drift is different from an ordinary sampling failure: pending
rollups are discarded without a crash flush, no graceful stop is published, and
the writer lease is released for operator repair.

## Agents and MCP

Agents should use the versioned JSON interface instead of parsing terminal text:

```bash
bk agent context --compact
bk agent recommend 2 1h30m --mem 12g --compact
bk 2 1h30m --mem 12g --share 2 --op-id run-20260712-001 --json
bk agent edit 6e957ef1 --duration 2h --op-id edit-20260712-001 --compact
bk agent cancel 6e957ef1 --compact
```

Create and edit operations require a stable operation ID. An identical retry
returns `status=exists`; reusing the ID for a different write is rejected.
GPUBK resolves a committed exact retry before live GPU probing, external
allocation, or private command-spec writes. JSON reports
`allocator.source=idempotent-replay`; when the caller did not already supply
advice, replay-only live fields are `unknown` rather than stale. This confirms
the committed reservation, not that an old working directory or worker remains
launchable. Agent context advertises `capabilities.preflight_idempotent_replay`.
Recommendations are read-only. Identity always comes from the local process UID.

Run the optional stdio MCP server with:

```bash
bk-mcp                       # same as: bk mcp
bk skill install            # installs the bundled Codex Skill
```

The default destination is `$CODEX_HOME/skills/gpubk` when `CODEX_HOME` is
absolute, otherwise `$HOME/.codex/skills/gpubk`. `--force` refuses a symbolic
link or an active working-directory tree and restores the previous Skill if the
staged replacement fails.

The MCP server provides context, recommendation, create, list, edit, cancel,
private-spec cleanup, and private job-log tools. It listens on stdio only; each
user runs their own process. Tool schemas include read-only, idempotent,
destructive, and closed-world annotations.

An administrator may also set `BK_ALLOCATOR_COMMAND` to a trusted local program
that reads `bk.allocator.v1` JSON and returns a GPU ordering. Its output is
advisory: every result still passes the built-in conflict, VRAM, time, UID, and
transaction checks. GPUBK validates the ledger-bound policy before invoking the
allocator for create, recommend, or edit operations. Timeout, invalid output,
and ordinary allocator failures fall back to built-in ordering; an interrupt
terminates the allocator process group before propagating. See the
[Agent protocol](https://github.com/lzzmm/gpubk/blob/main/src/bk/data/codex-skill/gpubk/references/protocol.md).

## Shared Server Setup

For a shared server, keep GPUBK in an isolated system virtual environment. This
avoids modifying the operating system's Python and gives upgrades one stable path:

```bash
sudo python3 -m venv /opt/gpubk
sudo /opt/gpubk/bin/python -m pip install --upgrade pip
sudo /opt/gpubk/bin/python -m pip install 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install
bk doctor --probe --require-monitor --strict
```

The guided installer asks one question at a time and shows a final review. Press
Enter to accept its conservative defaults. It initializes trusted configuration,
installs a tracked `/usr/local/bin/bk` link, installs the broker and monitor units,
optionally installs the colored login reminder, and enables both services at boot.
Use `--dry-run` to preview, `--yes` for unattended defaults, `--no-start` to install
without starting, or `--no-command-link` when another package manager owns the
global command. Python package installation itself never invokes `sudo` or changes
`/etc`; this explicit administrator command owns those system changes.

Running the same command on an existing managed deployment is safe and deliberately
different from first setup. It preserves the checksummed configuration and all data,
rejects flags that would change policy, reconciles only the tracked command and unit
files, and performs a controlled service restart. A failure after stopping services
triggers a best-effort restart of the previously managed units.

The lower-level path remains available for troubleshooting. It assumes `bk` is
already available at the path users will run:

```bash
sudo bk admin init --yes
sudo bk admin services install --yes
sudo systemctl daemon-reload
sudo systemctl enable --now gpubk-broker.service gpubk-monitor.service
```

The installer never uses a force-link operation. If `/usr/local/bin/bk` is absent,
it creates and tracks an absolute link to `/opt/gpubk/bin/bk`. If the same link
already existed before GPUBK setup, it records that fact and preserves the link on
uninstall. Any regular file or link to another target stops installation before
server state is changed. Inspect such a path explicitly:

```bash
ls -l /usr/local/bin/bk
readlink -f /usr/local/bin/bk
```

When the second command prints `/opt/gpubk/bin/bk`, rerun the installer; it will
adopt the correct pre-existing link without taking ownership of it:

```bash
sudo /opt/gpubk/bin/bk admin install
```

Otherwise, resolve the conflict deliberately or use `--no-command-link`. Never
use `ln -sf` over an unknown path.

On Debian or Ubuntu, install `python3-venv` first if `venv` is unavailable. The
initializer detects the GPUs and uses the account that invoked `sudo` as the
broker and monitor owner. It creates the same production paths used by a normal
deployment: `/etc/gpubk`, `/var/lib/gpubk`, and `/run/gpubk`. It does not create
an account or a group. The tracked system units start at boot, run under that
non-root UID, and keep writable access limited to the data and socket directories.

By default every local account can connect to the Unix socket and use `bk`, but
only the selected owner can write the ledger files. Ordinary users cannot edit
another UID's reservations or system policy and never need `sudo`. Using the
administrator's own account is fully supported. A dedicated account remains an
optional operational choice, not a security requirement.

The selected broker owner is also the public GPUBK administrator. `bk info`
shows that Linux account, numeric UID, and its `adduser`/GECOS Full Name, Room,
Work Phone, Home Phone, and Other fields; `bk info --json` exposes the same
versioned document to tools and Agents. In the TUI, press `i`. The administrator
can update these local account fields with `sudo chfn USER`; changes and
`sudo bk admin transfer` take effect immediately without rewriting GPUBK data. Only
put contact information there that may be shown to every local GPUBK user.

To federate several working GPUBK hosts, create a catalog on the machine where users
will run cluster commands. A GPU host can include itself with `cluster init`. A login
node or other client without local GPUs should skip init, probe its first GPU host,
and run the printed `cluster add` command; that first add creates a remote-only catalog.
Probe as the ordinary user who will use the cluster. The probe uses non-interactive SSH
with strict host-key checking, validates the stable identity, version, clock, GPU count,
and retry-safe write capabilities, then prints the root command to review and run:

```bash
sudo bk admin cluster init gpu-a --yes
bk c probe gpu-b gpu-b
# Run the exact sudo bk admin cluster add ... command printed above.
sudo bk admin cluster map lab-user gpu-a 1003 --yes
sudo bk admin cluster map lab-user gpu-b 2042 --yes
# Undo a wrong mapping with: sudo bk admin cluster unmap gpu-b 2042 --yes
sudo bk admin cluster status
bk c
bk c check
bk c check --jobs       # additionally require this user's worker on every node
bk c rec 1 30m
bk c 1 30m -j
bk c x 1 30m          # exclusive, earliest node
bk c 1 30m -t "tomorrow 9"  # exact friendly local start
bk c 1 2h -- python /absolute/path/train.py
```

The mapping is reporting metadata, not authorization: SSH and the destination's
numeric UID still decide what a caller may change. Mapped principals appear in live
reservation rows, TUI details, usage aggregation, and structured cluster contexts.
`bk c check` warns when the current caller is mapped on only some nodes or to different
principals, so incomplete reporting setup is visible before collecting history.

For a remote-only client, replace the first two lines with `bk c probe gpu-a gpu-a`
and run its printed add command, then probe and add `gpu-b`. Catalog creation is
create-only and refuses to replace a file that appears concurrently.

If an SSH alias, remote executable, timeout, or tie-break priority changes, update the
node in place so its stable identity and UID mappings remain intact:

```bash
bk c probe gpu-b new-gpu-b        # first confirm the stable node ID is unchanged
sudo bk admin cluster set gpu-b --target new-gpu-b --timeout 12 --yes
sudo bk admin cluster set gpu-b --priority 10 --yes
```

`bk c -h`, `bk c probe -h`, and every long-form subcommand's `-h` work before this
catalog exists. The probe is read-only and does not bypass SSH authentication; verify
the host key before trusting the discovered stable ID. For
retry-safe automation, keep the same `--op-id` when repeating an exact cluster book,
edit, or cancel request. Every user should run `bk c check` once: it verifies that
their own SSH identity can reach each enabled node, that stable identities and clocks
are valid, and that the remote version supports retry-safe writes. Use `--jobs` before
depending on automatic command launch; ordinary checks also warn when a pending command
already exists on a node whose worker is not running.

Use a username-free host or per-user SSH alias in the shared root catalog, never
`user@host`. A fixed username would make every local caller act as that one remote UID.
Users whose account names differ between nodes should map the common alias in their own
`~/.ssh/config`. GPUBK rejects pinned usernames in root catalogs; `sudo bk admin cluster
status` and `cluster set` remain available to repair a legacy entry.

Put a host into maintenance without deleting its endpoint, UID mappings, or archived
history. Disabled hosts are not contacted and never participate in placement:

```bash
sudo bk admin cluster disable gpu-b --yes
sudo bk admin cluster enable gpu-b --yes
```

To stop federation on this client, run `sudo bk admin cluster delete --yes`. It removes
only the local routing catalog; no GPU host, reservation, ledger, usage history, archive,
worker, or SSH configuration is changed.

Human cluster tables use the caller's local timezone. Structured `--json` documents
continue to use canonical UTC timestamps.

Before enabling a real shared catalog, the repository includes one end-to-end candidate test:

```bash
python3 tools/cluster_acceptance.py user@gpu-a user@gpu-b
```

Those `user@host` values are private transport arguments for the isolated test, not
entries for the shared root catalog.

It builds the current checkout as a wheel, installs that exact wheel under each SSH account's
private temporary cache, uses one simulated GPU and an isolated ledger per host, then
exercises pre-catalog discovery, cluster status, recommendation, placement on two
independent nodes, operation replay, and cancellation. It needs key-based non-interactive
SSH with known host keys, uses no `sudo`, never
contacts the production broker or NVML, and removes its remote files. Pass `--wheel DIST.whl` to
test an already-built artifact. The command writes a mode-`0600` JSON report under
`acceptance-reports/`.

This transport test does not replace the final live checks: run the ordinary single-host
acceptance on every GPU server, verify a second user's authorization, run one approved live GPU
workload, and verify service restart/reboot behavior before calling a release stable.

A full managed uninstall also understands the standard root-owned cluster catalog. If
GPUBK originally created `/etc/gpubk`, the validated catalog is removed with that
directory; catalogs in a configuration directory that predated GPUBK are preserved.

The root-owned catalog contains endpoints, stable node IDs, priorities, and optional
identity mappings, but no SSH keys. `bk c` also shows each reachable node's GPUBK version,
which makes mixed-version rollout visible. Each user is authenticated independently by SSH
and acts as that remote numeric UID. Node priority only breaks ties after earliest
start. The remote broker revalidates every write; one reservation never spans hosts.
Local CLI-to-broker IPC remains a Unix socket, while cross-host calls use outbound
SSH. Never point independent live ledgers at one NFS directory.

An optional NFS archive can keep portable offline statistics without joining the live
writers. Create a dedicated directory, add it to each catalog, and export completed UTC
days from every node:

```bash
sudo install -d -m 0755 /srv/gpubk-cluster-history
sudo bk admin cluster history-root /srv/gpubk-cluster-history --yes
sudo bk admin cluster export-history --since 1095d --resolution 10m --yes
sudo bk admin cluster verify-history
bk c history --since 30d
```

Later exports are incremental. Every node writes only its stable-ID namespace; every
generation is compressed, checksummed, immutable-style, and atomically published.
If an export is interrupted, the next locked export removes only a strictly validated
incomplete generation; suspicious temporary entries and symlinks are reported and left
untouched.
Only versioned public usage summaries and samples are exported, never a ledger, command
arguments, job specs, secrets, or logs. Booking remains available when the archive is
offline. NFS `root_squash`, multi-owner roots, daily operation, and recovery rules are
covered in [CLUSTER.md](CLUSTER.md).

Useful non-interactive forms:

```bash
sudo bk admin init --dry-run
sudo bk admin init --yes                         # owner: the user who invoked sudo
sudo bk admin init --yes --service-user "$USER" # same choice, made explicitly
sudo bk admin init --yes --service-user gpubk --data-dir /data2/shared/gpubk
sudo bk admin init --yes --service-user gpubk --access group --group gpuusers
sudo bk admin init --yes --disabled-gpus 7 --gpu-priority 6=10
```

Group access is optional and only restricts who can connect to the socket. The
initializer never creates accounts, groups, or memberships. Ledger files use
`0644`, directories use `0755`, and both are owned by the service account: all
users can inspect scheduling state, but only the broker can mutate it. The
broker authenticates each local connection from kernel peer credentials, not a
client-supplied username or UID.

An administrator can take unreliable GPUs out of new scheduling or lower their
preference without editing trusted JSON. Larger priority levels are less
preferred, but only break ties at the same earliest start time. Monitoring and
history remain available for disabled GPUs. Stop both writers, preview, apply,
then restart them:

```bash
sudo systemctl stop gpubk-broker.service gpubk-monitor.service
sudo bk admin gpu-policy --disabled-gpus 7 --gpu-priority 6=10 --dry-run
sudo bk admin gpu-policy --disabled-gpus 7 --gpu-priority 6=10 --yes
sudo systemctl start gpubk-broker.service gpubk-monitor.service
```

The same atomic policy command can allow users to omit a shared VRAM estimate:

```bash
sudo bk admin gpu-policy --allow-implicit-shared-memory --dry-run
sudo bk admin gpu-policy --allow-implicit-shared-memory --yes
```

An omitted value remains `auto`, not zero: zero would falsely claim that a job
uses no VRAM. Use `--require-shared-memory` to restore the strict policy.

Use `--enable-all` or `--clear-priority` to clear either policy. If power is lost
during the update, leave `/etc/gpubk/config-update.json` in place and run
`sudo bk admin gpu-policy --recover --dry-run`, followed by the same command with
`--yes`. Normal startup fails closed until the prior trusted files are restored.

For a reversible foreground trial before enabling system services, start the
broker in a second terminal as the selected owner, without `sudo`:

```bash
bk broker --check
bk broker
```

This foreground trial is not a simulation. It uses the real root-owned config,
service-owned ledger, Unix socket, locking, GPU probes, and user identities.
Only process supervision differs from the systemd deployment. Do not pass
`--gpu-count` on a GPU server unless you deliberately want simulated topology.

Then, as an ordinary user:

```bash
bk info                                  # find the responsible administrator
bk config                                # storage transport should be broker
bk doctor --probe --strict               # checks socket identity and connectivity
bk 1 30m
bk l
bk t
```

### One-command remote acceptance

From a trusted local checkout, one command downloads the exact public PyPI
release, verifies every wheel against PyPI, uploads a private bundle over SSH,
runs isolated scheduler checks against the real GPU topology, inspects the
deployed services, and downloads a SHA-256-verified report:

```bash
python3 tools/remote_acceptance.py USER@GPU-HOST \
  --remote-python /opt/gpubk/bin/python \
  --system-bk /usr/local/bin/bk \
  --sudo
```

The script downloads locally first; if local PyPI access is unavailable, it
automatically downloads and verifies the same wheels on the GPU host. `--sudo` opens a remote password
prompt only for read-only service-account, ownership, and systemd checks. The
runner never restarts services, writes the production ledger, or launches a GPU
workload. Candidate scheduling uses a private directory under
`~/.cache/gpubk/acceptance/`, which is removed after the report is retrieved.

Reports are written below `acceptance-reports/` and include the JSON result,
human-readable summary, bundle manifest, original archive, and checksum. A
failed automated check still downloads its report and returns a nonzero status.
Use `--keep-remote` only while debugging. `--include-journal` explicitly opts in
to the last 80 lines from the two GPUBK units. TUI appearance, cross-user
authorization, a maintenance-approved tiny workload, and reboot persistence
remain manual checks.

To hand operation to another existing local account, stop the broker and monitor,
preview the transaction, apply it, then reload and restart the tracked units:

```bash
sudo systemctl stop gpubk-broker.service gpubk-monitor.service
sudo bk admin transfer NEWUSER --dry-run
sudo bk admin transfer NEWUSER --yes
sudo systemctl daemon-reload
sudo systemctl start gpubk-broker.service gpubk-monitor.service
```

The command takes broker, monitor, and ledger maintenance guards, changes managed
ownership in place, and updates only `broker_uid` and `monitor_uid`. Reservation
UIDs, bookings, audit events, usage history, and scheduling policy are not
rewritten. Tracked system units are updated to the new numeric UID and GID in the
same transaction. A root-only recovery journal protects the operation; after an
interrupted handoff run `sudo bk admin transfer --recover --yes`, reload systemd,
and restart the units.

Package upgrades do not rewrite configuration or data. Stop the broker and monitor,
upgrade the isolated environment, reconcile the tracked deployment, and verify:

```bash
sudo systemctl stop gpubk-broker.service gpubk-monitor.service
sudo /opt/gpubk/bin/python -m pip install --upgrade 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin install --yes
bk --version
bk broker --check
bk doctor --probe --require-monitor --strict
```

See [UPGRADING.md](https://github.com/lzzmm/gpubk/blob/main/UPGRADING.md) for service restart, rollback, and release-specific
checks.

### Backup, clear, and restore

The administrator commands cover the complete managed data tree: reservations,
operation logs, internal ledger snapshots, usage events, rollups, and collector state.
They do not silently replace the trusted system configuration. Stop both writers first:

```bash
sudo systemctl stop gpubk-broker.service gpubk-monitor.service

sudo bk admin data backup /var/backups/gpubk/pre-change
sudo bk admin data verify /var/backups/gpubk/pre-change

# Destructive: this creates and verifies pre-clear before changing the data directory.
sudo bk admin data clear --backup-to /var/backups/gpubk/pre-clear --yes

# Restore is accepted only while the managed data directory is empty.
sudo bk admin data restore /var/backups/gpubk/pre-change --yes

sudo systemctl start gpubk-broker.service gpubk-monitor.service
bk doctor --probe --require-monitor --strict
```

Omit the backup path on `backup` or `--backup-to` on `clear` to use a UTC-stamped
directory below `/var/backups/gpubk`. Every snapshot is a private directory with a
versioned manifest, an informational copy of the configuration, and per-file size and
SHA-256 metadata. Creation, clear, and restore use same-filesystem atomic directory
replacement and filesystem syncs. They reject symbolic links, hard links, special files,
unexpected backup contents, ownership drift, checksum drift, and concurrent writers.
Files are copied in bounded chunks, so large history does not need to fit in memory.

`bk reset --yes` remains only for private/test data. It is disabled for shared server
storage. `bk admin uninstall --purge-data` removes the tracked deployment; use
`bk admin data clear` when keeping the installation but starting with empty history.

An administrator may add a short reservation reminder to interactive login
shells:

```bash
sudo bk admin login-hook install --yes
sudo bk admin login-hook status
```

The hook runs `bk login --hook` only once per login, only when stdout is a
terminal, and through a one-second `timeout`. It reads the committed ledger
without taking a write lock or probing NVML. Color terminals distinguish current
and upcoming reservations. A fresh trustworthy monitor also enables a red alert
when this UID still occupies a GPU after its reservation expired. With no active or next-24-hour
reservation it prints nothing; failures are suppressed so SSH login cannot be
blocked. `sudo bk admin login-hook uninstall --yes` removes only the marked
GPUBK file. A full tracked `sudo bk admin uninstall` also removes that managed hook.

Stop and disable the tracked services before uninstalling. GPUBK verifies each
unit against its root-only manifest, restores any reviewed pre-existing unit,
and refuses drift. Non-empty data is never deleted without the explicit purge
flag:

```bash
sudo systemctl disable --now gpubk-monitor.service gpubk-broker.service
sudo bk admin services uninstall --yes
sudo systemctl daemon-reload

sudo bk admin uninstall --dry-run --purge-data
sudo bk admin uninstall --purge-data --yes
sudo rm -rf /opt/gpubk
```

The uninstall manifest restores pre-existing empty-directory metadata and an
older replaced configuration. It refuses to proceed if the broker is active,
the managed configuration changed, or an unknown file appears in a directory
GPUBK would remove. Accounts and groups are left untouched because GPUBK never
creates them. `bk admin uninstall` removes a command link it created, but preserves
an identical link that existed before GPUBK setup. These commands remove the tracked
server state and isolated Python environment; pre-existing files and directories
recorded by the install manifest are restored. Each user who installed a worker
unit can remove it in the same way with `systemctl --user disable --now
bk-worker.service` and `bk service uninstall worker`.

`sudo bk admin services status` reports the tracked interpreter, UID/GID, unit file
state, and remaining enable links. GPUBK writes unit files but leaves
`systemctl enable`, `start`, `stop`, and `disable` visible in the deployment
steps so an administrator can see exactly when a persistent process changes.

### Configuration and production notes

`sudo bk admin init` writes the root-owned configuration outside the service-owned
ledger directory. A generated configuration contains the broker identity and
socket policy in addition to scheduling settings:

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

When neither `BK_DATA_DIR` nor `BK_CONFIG_FILE` is set, GPUBK automatically
discovers `/etc/gpubk/config.json`. A system configuration must contain an
absolute `data_dir`, so normal SSH sessions, MCP clients, and user services all
reach the same ledger without shell startup files. A nonstandard trusted file
can be selected with `BK_CONFIG_FILE`; its `data_dir` field has the same
behavior and is required unless `BK_DATA_DIR` is also set. Explicit
`BK_DATA_DIR` keeps the previous private/data-local behavior and skips automatic
system discovery. Set both variables when deliberately combining an alternate
data directory with an external configuration.

For a private installation without those overrides, GPUBK uses
`$XDG_DATA_HOME/bk` for the ledger, `$XDG_STATE_HOME/bk/jobs` for private job
state, and `$XDG_CONFIG_HOME/systemd/user` for installed user units. Per the XDG
base-directory rules, only absolute non-empty XDG values are accepted; relative
or empty values fall back to `$HOME/.local/share`, `$HOME/.local/state`, and
`$HOME/.config`. This keeps CLI and user services on the same paths regardless
of their working directory. An explicit `BK_JOB_LOG_DIR` or `job_log_dir` must
be absolute (a leading `~` is expanded).

Replace `991` with `id -u <service-account>`. `broker_gid` appears only in
optional group access; it controls the socket, not ledger ownership. The
configuration file and every directory that contains it must be root-owned and
must not be writable by group or other users. Broker security fields are accepted
only from this trusted external configuration. For a single-user installation,
the backward-compatible default remains `$BK_DATA_DIR/config.json` whenever
`BK_DATA_DIR` is explicitly selected.

The monitor runs as the configured service UID and writes usage history directly
to the service-owned data directory. Exit status `77` means the process is not
the configured monitor writer. Per-user workers keep private command specs and
logs under each user's XDG state directory; only their constrained job-state
updates pass through the broker.
Exit status `78` from either daemon means its effective policy differs from the
ledger. Do not retry with altered limits; inspect `bk config`, repair the trusted
configuration, reinstall captured service settings if necessary, then restart.

`max_shared_users` is retained as the compatible configuration name; it now
defines the integer shared-slot maximum per GPU. Old reservations without a
`share_units` field consume one slot.

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

Environment variables override ordinary file values, and a command flag
overrides the corresponding default for that invocation. Security role
`monitor_uid`, `broker_socket`, `broker_uid`, and `broker_gid` are file-only and
cannot be replaced by environment variables.
New files should declare
`"config_version": 1`; unversioned files remain readable for compatibility.
Unknown keys, wrong types, non-finite numbers, unsafe paths, and excessive
values are rejected instead of ignored. The JSON report lists active override
names but never prints the external allocator command.

Scheduling policy, retention, worker timing and concurrency, allocator integration, and display
defaults are configurable. Schema versions, transaction durability, path and
permission checks, record-size limits, and other corruption defenses are fixed
implementation safeguards rather than administrator tuning knobs.

Ledger reads validate every reservation field used for admission: identity,
GPU IDs, mode, status, and ordered timestamps. Unknown extension fields remain
preserved for forward compatibility, while an unknown value in a current
semantic field fails closed. A semantically damaged primary ledger can fall
back only to a backup that passes the same complete validation.

All users and user services must resolve the same root-owned configuration. The
standard `/etc/gpubk/config.json` layout provides that automatically. The first
write binds scheduling and storage policy into the ledger; clients with
conflicting settings fail closed. Start the broker, then run the deployment
preflight from a normal login:

```bash
bk config
bk doctor --probe --strict
bk doctor --probe --json --strict
```

For an ordinary user, preflight verifies read-only ledger access and a
kernel-authenticated broker connection. Run it once more as the service account
to exercise atomic replacement, locking, GPU telemetry, and cross-user process
identity. A restrictive `hidepid` policy can prevent process attribution.

After enabling the monitor, verify the long-running writer separately:

```bash
bk doctor --require-monitor --strict
bk doctor --require-monitor --json --strict
```

Each user who enables scheduled commands should verify that their private worker
holds the lease for this exact data directory:

```bash
bk doctor --require-worker --strict
bk doctor --require-worker --json --strict
```

Both flags can be combined when checking a complete current-user deployment.
Ordinary `doctor` reports the privacy-safe worker state without requiring the
optional service or creating its private directory.

`bk reset` is disabled for broker-backed storage. Use the manifest-checked
`sudo bk admin uninstall` path to retire a test deployment. `bk reset` remains
available for private and disposable simulation directories.

As the service account, the probe creates randomly named temporary files,
verifies same-directory atomic replace and directory fsync, checks same-host
cross-process `flock`, confirms modes and free space, probes the real GPU
telemetry source, and then removes its files. As an ordinary user it performs
read-only checks and broker authentication instead. GPU indices must exactly match
`0..gpu_count-1`; every NVML
device must report usable memory, a process list, a stable CUDA-compatible GPU
identifier, and per-process utilization.
A topology mismatch or missing process list fails the probe. Missing per-process
utilization, simulation, or an `nvidia-smi` fallback is a strict-mode warning.
In JSON, `healthy` covers read-only ledger checks; `ready` remains `null` until
`--probe` supplies deployment evidence.
Plain `doctor` never initializes storage, acquires a lock, recovers a pending
transaction, or follows a symbolic link or hard-linked alias at a managed path.
It also reports permission drift across the ledger, backups, and telemetry tree.
Configured mode or owner drift makes writes fail closed instead of silently
repairing a non-empty directory; only a service-account `--probe` writes temporary
files.
For NFS/FUSE used by multiple hosts, additionally verify locking from a second
host because one machine cannot prove cross-host lock propagation. Every writer
must use GPUBK.

See [SECURITY.md](https://github.com/lzzmm/gpubk/blob/main/SECURITY.md) for the supported boundary, file safety, WAL
recovery, private job specs, MCP isolation, and administrator responsibilities.

## Try It Without a GPU

Booking and the TUI can run with simulated GPU count:

```bash
export BK_DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gpubk-demo.XXXXXX")"
export BK_GPU_COUNT=4 BK_MAX_SHARED_USERS=4
bk t
bk 1 30m --share 3
```

The private directory is created before GPUBK starts, so the example also works
when an operating system exposes `/tmp` through a symbolic link. The cards show
unknown hardware metrics, but scheduling, shared capacity, the timeline,
Add/Edit, logs, and Agent JSON remain usable.

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
