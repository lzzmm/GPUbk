# Upgrading GPUBK

## Routine upgrade for `/opt/gpubk`

The recommended shared-server layout keeps code in `/opt/gpubk`, trusted
configuration in `/etc/gpubk`, and mutable state in `/var/lib/gpubk`. A package
upgrade touches only `/opt/gpubk`.

1. Record the current version and keep its wheel or exact PyPI version.
2. Stop the broker and the one monitor. Stop each scheduled-job worker before
   replacing code; running GPU workloads are unrelated and must not be stopped.
3. Upgrade the existing isolated environment.
4. Run the broker preflight as the configured owner, restart the same services,
   and verify them from an ordinary account.

```bash
/opt/gpubk/bin/bk --version
sudo systemctl stop gpubk-broker.service gpubk-monitor.service
sudo /opt/gpubk/bin/python -m pip install --upgrade 'gpubk[gpu]'
sudo /opt/gpubk/bin/bk admin services install --yes
sudo systemctl daemon-reload
sudo systemctl start gpubk-broker.service gpubk-monitor.service
/opt/gpubk/bin/bk --version
/opt/gpubk/bin/bk broker --check
/opt/gpubk/bin/bk doctor --probe --require-monitor --strict
```

Keep the same extras used during installation (`gpu`, `mcp`, or `all`). Do not
rerun `bk admin init` for a code-only upgrade. If verification fails, stop the
new processes and reinstall the recorded version, for example:

```bash
sudo /opt/gpubk/bin/python -m pip install 'gpubk[gpu]==PREVIOUS_VERSION'
```

The `/usr/local/bin/bk` symbolic link does not need to be recreated during a
routine upgrade. If it already resolves to `/opt/gpubk/bin/bk`, leave it in
place; do not use a force-link command over an unknown existing path.

Then restart and verify again. The root-owned install manifest, configuration,
reservations, audit log, and usage history remain in place throughout. Running
`bk admin services install --yes` after the package update refreshes tracked
system units only when their current checksums still match the manifest. Per-user
worker units remain separate and can be refreshed with
`bk service install worker --force` when release notes require it.

To change the account that runs the broker and monitor, do not copy the ledger or
edit numeric UIDs by hand. Stop both processes and use the recoverable ownership
transaction:

```bash
sudo systemctl stop gpubk-broker.service gpubk-monitor.service
sudo bk admin transfer NEWUSER --dry-run
sudo bk admin transfer NEWUSER --yes
sudo systemctl daemon-reload
sudo systemctl start gpubk-broker.service gpubk-monitor.service
```

If it was interrupted, leave `transfer.json` in place and run
`sudo bk admin transfer --recover --yes`. The transfer preserves reservation
owners and history; only the trusted service identity and managed filesystem
ownership change. If system units are tracked, their numeric `User=` and `Group=`
values change in the same recoverable transaction.

To change GPU eligibility or preference, use the managed transaction instead of
editing `/etc/gpubk/config.json`:

```bash
sudo systemctl stop gpubk-broker.service gpubk-monitor.service
sudo bk admin gpu-policy --disabled-gpus 7 --gpu-priority 6=10 --dry-run
sudo bk admin gpu-policy --disabled-gpus 7 --gpu-priority 6=10 --yes
sudo systemctl start gpubk-broker.service gpubk-monitor.service
```

An interrupted update leaves `config-update.json` beside the trusted config and
blocks normal startup. Do not remove it manually. With both services stopped,
run `sudo bk admin gpu-policy --recover --dry-run` and then repeat with `--yes`.
Recovery restores the checksummed configuration and install manifest as one
reviewed pair.

## Before upgrading a shared server

1. Read the target release notes and run `bk doctor --json` with the installed
   version.
2. Record the canonical data, trusted configuration, and per-user job-log paths
   reported by `bk config --json` and `bk service show`.
3. Back up the shared data directory without following symbolic links. Keep its
   ownership, group, modes, and ACLs.
4. Stop the one trusted monitor and ask users to stop their GPUBK workers. Do
   not stop GPU workloads or unrelated services.
5. Test the new wheel in a separate virtual environment and an isolated
   `BK_DATA_DIR` before changing the shared installation.

Do not use `bk reset` to prepare a shared upgrade. It is disabled for shared
directory modes; preserve and migrate the existing data instead.

New 0.2 shared-server deployments use a local Unix-socket broker: one existing,
non-root service account owns and writes the ledger, while ordinary users receive
read-only file access and submit mutations through the broker. `bk admin init`
records every path it creates so a test deployment can be reviewed and removed
with `bk admin uninstall --dry-run` followed by an explicit uninstall.

Do not point `bk admin init` at a non-empty legacy shared directory. It deliberately
refuses to change ownership or replace policy around live data. For a legacy direct
deployment, schedule maintenance, preserve an ownership-retaining backup, stop all
GPUBK writers, and migrate the reviewed ledger into a separately initialized broker
deployment before reopening bookings. Keep the original data untouched until
`bk doctor --probe --strict` passes as both the service account and an ordinary user.

If that older deployment keeps `config.json` inside its group-writable data
directory, first move a reviewed copy to a non-writable administrator directory:

```bash
sudo install -d -m 0755 -o root -g root /etc/gpubk
sudo install -m 0644 -o root -g root /data2/shared/bk/config.json \
  /etc/gpubk/config.json
```

For a legacy direct-mode transition only, add the canonical shared data directory
and numeric UID of the one telemetry account to that reviewed file before starting
the 0.2 monitor:

```json
{
  "data_dir": "/data2/shared/bk",
  "monitor_uid": 1001
}
```

Replace `1001` with `id -u <monitor-account>`. This is required when the
configured data-directory mode is group-writable. With the standard path,
GPUBK discovers `/etc/gpubk/config.json` without shell exports. An alternate
trusted path still requires `BK_CONFIG_FILE`.

Confirm `bk config` reports the new canonical path and a matching ledger policy.
Reinstall monitor and worker units with `--force` so they capture it. GPUBK rejects
an existing configuration whose directory can be replaced by shared-group members.
Both daemons now return `78` before normal startup when their effective policy
does not match the ledger, and the bundled units deliberately do not restart
that persistent error. Stop the service, correct the trusted configuration,
reinstall the unit if captured paths or overrides changed, then start and verify
it again. Do not alter ledger-bound limits merely to clear the exit code.

GPUBK does not require an in-place ledger migration. It preserves unknown
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
Restart every worker after upgrading. A lease written by an older worker has no
data-directory instance binding, so current clients intentionally report it as
`unverified` until the worker restarts and rewrites the lease.
New scheduled commands use a signed v2 private spec that snapshots only the
submitting process's `PATH`; a pre-upgrade worker cannot read that format. Stop
or restart old workers before accepting new scheduled commands. Current workers
continue to execute queued v1 specs, and exact v1 operation-ID retries and
duplicates remain recognizable without rewriting their private files.
Restart the monitor during this upgrade. A legacy `gpubk.collector.v1` heartbeat
without the additive stable-device-identifier or process-identity capability is
intentionally shown as degraded until the current monitor replaces it.
After starting the services, verify the monitor with
`bk doctor --require-monitor --strict` and each user's worker with
`bk doctor --require-worker --strict`.
GPUBK now ignores empty or relative XDG base-directory values instead of
resolving them against the caller's working directory. Before restarting,
inspect `bk config --json`; if an old shell used a relative XDG value, move or
select the intended absolute data and private job directories deliberately,
then reinstall the affected user units with `--force`.
If those user services must survive logout or start at boot, have an
administrator verify selective `loginctl enable-linger <user>` state; GPUBK
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

Version 0.2 also requires configured modes on managed write targets, rejects
files with hard-linked aliases, and in setgid mode requires every managed path to
keep the data directory's numeric GID. Run plain `bk doctor --json --strict`
after the upgrade. If it reports a mode, GID, or link-count issue, stop GPUBK
writers and have an administrator repair that named path and its ownership
deliberately; GPUBK does not silently `chmod` or `chgrp` existing shared data.
Existing setgid configurations may omit `storage_gid`. To enable the stronger
root-group binding, obtain the numeric lab-group GID with
`getent group <group> | cut -d: -f3`, add it to the trusted configuration, and
run `bk doctor --probe --strict` before restarting writers. The next successful
ledger write upgrades a legacy policy with this GID. After that point, clients
which omit it or configure another GID are rejected.

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

Stop GPUBK monitor and worker services before reinstalling the previous
version. Do not run a 0.1 worker against job state already claimed by a 0.2
worker.

Plain 0.1 reservations remain readable by 0.2. The reverse scheduling behavior
is not equivalent: 0.1 does not understand weighted `--share` capacity and can
overbook a GPU if such reservations are active. Roll back to 0.1 only after all
weighted reservations and 0.2-managed jobs have reached a terminal state.
Keep the newer telemetry directory for a future upgrade; 0.1 ignores it.
