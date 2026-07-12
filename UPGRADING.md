# Upgrading GPUbk

## Before upgrading a shared server

1. Read the target release notes and run `bk doctor --json` with the installed
   version.
2. Record the canonical data, trusted configuration, and per-user job-log paths
   reported by `bk config --json` and `bk service show`.
3. Back up the shared data directory without following symbolic links. Keep its
   ownership, group, modes, and ACLs.
4. Stop the one trusted monitor and ask users to stop their GPUbk workers. Do
   not stop GPU workloads or unrelated services.
5. Test the new wheel in a separate virtual environment and an isolated
   `BK_DATA_DIR` before changing the shared installation.

Do not use `bk reset` to prepare a shared upgrade. It is disabled for shared
directory modes; preserve and migrate the existing data instead.

If an older shared deployment keeps `config.json` inside its group-writable data
directory, move a reviewed copy to a non-writable administrator directory before
starting 0.2.x:

```bash
sudo install -d -m 0755 -o root -g root /etc/gpubk
sudo install -m 0644 -o root -g root /data2/shared/bk/config.json \
  /etc/gpubk/config.json
```

Add the canonical shared data directory and numeric UID of the one telemetry
account to that reviewed file before starting the 0.2 monitor:

```json
{
  "data_dir": "/data2/shared/bk",
  "monitor_uid": 1001
}
```

Replace `1001` with `id -u <monitor-account>`. This is required when the
configured data-directory mode is group-writable. With the standard path,
GPUbk discovers `/etc/gpubk/config.json` without shell exports. An alternate
trusted path still requires `BK_CONFIG_FILE`.

Confirm `bk config` reports the new canonical path and a matching ledger policy.
Reinstall monitor and worker units with `--force` so they capture it. GPUbk rejects
an existing configuration whose directory can be replaced by shared-group members.

GPUbk does not require an in-place ledger migration. It preserves unknown
reservation extension fields and writes ledger changes atomically. Fields with
current scheduling semantics still require valid identity, GPU, mode, status,
and ordered timestamps; run `bk doctor --json --strict` before upgrading so a
legacy damaged record can be repaired or restored deliberately. Telemetry uses
a separate versioned store; inspect legacy telemetry with `bk usage migrate`
and copy it only with `bk usage migrate --yes`.

## 0.1.x to 0.2.x

Version 0.2 adds weighted shared capacity, versioned telemetry history,
deployment probes, launch-time GPU checks, bounded private job storage, and a
single-worker crash-recovery lease.

After installing the new package:

```bash
bk --version
bk doctor --probe --strict
bk usage migrate
bk service install monitor --force
bk service install worker --force
```

Review generated units with `bk service show monitor` and
`bk service show worker` before enabling them. Start exactly one trusted
monitor for a shared data directory. Each UID may run one worker, and every
worker for that UID must use the same private `BK_JOB_LOG_DIR`.
Restart the monitor during this upgrade. A legacy `gpubk.collector.v1` heartbeat
without the additive stable-device-identifier capability is intentionally shown
as degraded until the current monitor replaces it.
After starting the services, verify the monitor with
`bk doctor --require-monitor --strict` and each user's worker with
`bk worker --status --require-running`.
If those user services must survive logout or start at boot, have an
administrator verify selective `loginctl enable-linger <user>` state; GPUbk
never changes linger policy automatically.

Run `bk usage migrate --yes` only after reviewing its dry-run report. Legacy
files remain untouched. Verify the result with read-only `bk agent context
--compact`, `bk jobs --json`, and `bk usage --json` calls before enabling
unattended services.

Version 0.2 treats a failed containing-directory `fsync` as a real durability
failure. A booking whose WAL was already renamed is returned with a deferred-
recovery warning and is recovered idempotently on the next operation; telemetry,
private job files, and unit installation fail visibly. Do not enable unattended
services on a mount where `bk doctor --probe --strict` fails the atomic-replace
probe.

Version 0.2 also requires configured modes on managed write targets and rejects
files with hard-linked aliases. Run plain `bk doctor --json --strict` after the
upgrade. If it reports a mode or link-count issue, stop GPUbk writers and have an
administrator repair that named path and its ownership deliberately; GPUbk does
not silently `chmod` existing shared data.

## Running-job boundary

A 0.2 worker does not take over an active job created by a pre-lease worker.
That job blocks new claims until it exits or its reservation expires. A clean
upgrade therefore stops old workers after their current commands finish.

After a 0.2 worker crash, the next worker may terminate only same-UID local
process groups whose environment exactly matches the reservation. The job
remains `uncertain`; retry it only after reviewing its effects and explicitly
accepting duplicate-execution risk.

A 0.2 worker also reserves `worker_termination_grace_seconds` (5 seconds by
default) at the end of each scheduled command window: it sends TERM during that
interval and KILL at `end_at`. Make long-running commands checkpoint on TERM.
Reinstall the bundled worker unit so its 75-second systemd stop timeout can
accommodate the configurable grace and durable final state update.

## Rollback

Stop GPUbk monitor and worker services before reinstalling the previous
version. Do not run a 0.1 worker against job state already claimed by a 0.2
worker.

Plain 0.1 reservations remain readable by 0.2. The reverse scheduling behavior
is not equivalent: 0.1 does not understand weighted `--share` capacity and can
overbook a GPU if such reservations are active. Roll back to 0.1 only after all
weighted reservations and 0.2-managed jobs have reached a terminal state.
Keep the newer telemetry directory for a future upgrade; 0.1 ignores it.
