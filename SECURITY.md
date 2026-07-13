# Security Policy

## Scope

GPUbk is a cooperative POSIX scheduler. It protects ledger integrity and enforces application-level UID ownership when every writer uses `bk`. It is not a kernel GPU access-control system and cannot prevent a user with direct device permission from launching CUDA outside GPUbk.

Supported security boundaries:

- MCP, CLI, TUI, and worker identity comes from the local process UID.
- Shared-ledger writes use an advisory lock, WAL journal, atomic replacement, and idempotent audit events.
- Ledger and WAL reads validate reservation identity, devices, mode, status, and time ordering
  before scheduling. Unknown extension fields are preserved, but unknown current semantics fail
  closed; backup fallback accepts only a document that passes the same validation.
- Shared data files reject symbolic links, hard-linked aliases, FIFOs, devices, and other
  unsafe leaf files before reading or writing.
- Write paths require the configured file and directory modes exactly. Permission drift fails
  closed without silently running `chmod`; `bk doctor` reports the path for administrator repair.
- For setgid shared storage, read-only health checks report managed ledger, backup, and telemetry
  paths whose numeric GID differs from the data directory; the deployment probe also verifies that
  a newly atomically replaced file inherits that GID. Ledger transactions, audit appends, telemetry
  writes, compaction, migration, and retention cleanup enforce the same GID before mutating data
  and never repair it implicitly.
- Administrators may bind the data root to a numeric group with file-only `storage_gid`. This
  closes the case where the complete tree is internally consistent but belongs to the wrong Unix
  group. Ordinary environment variables cannot replace the trusted value; once enabled, the
  ledger policy also rejects clients which omit or change it.
- Scheduled command arguments live in UID-owned `0600` specs, not the shared ledger.
- Terminal and expired private command specs are pruned by the owning UID; unreferenced specs
  receive a 24-hour race-safety grace period.
- Direct job stdout/stderr uses UID-owned rolling logs with configurable per-job, age, and
  per-UID limits. Cleanup retains active/retryable logs and refuses unsafe file types or ownership.
- Full launch diagnostics remain in the owning UID's private log; shared job state uses
  path-free failure reasons. The worker supervises the launched process group, not processes
  that deliberately escape into a new session.
- A UID-private kernel lease prevents concurrent workers. After an unclean exit, Linux recovery
  matches same-UID processes by the exact reservation environment marker and rechecks identity
  before TERM/KILL. Cross-host or unverifiable processes are never signalled and remain uncertain.
- Worker status probes the same UID-private kernel lock without creating or modifying storage.
  Lease PID, hostname, and timestamps are diagnostics only and never replace lock ownership as
  liveness evidence.
- Scheduled jobs re-check live process authorization and physical VRAM immediately before
  launch; this reduces races but cannot replace kernel device access control.
- Scheduled process groups receive TERM during the configured final grace window and KILL at the
  reservation deadline, so graceful shutdown time is charged to the current booking rather than
  leaking into the next one. Cancellation and worker shutdown use a bounded post-event grace.
- Fatal worker cleanup force-kills and reaps supervised process groups without depending on a
  successful ledger read. If durable state cannot be updated, restart recovery keeps the job
  uncertain instead of claiming completion or retrying automatically.
- The default launch guard passes the stable UUIDs from that same NVML snapshot to
  `CUDA_VISIBLE_DEVICES`, because NVML indices are not guaranteed to match CUDA ordinals.
  Missing identifiers fail closed on real NVML devices; disabling the guard explicitly accepts
  numeric-device compatibility risk.
- Shared capacity units enforce ledger admission and inferred memory budgets only. They do
  not enforce proportional SM time, memory isolation, or performance without MIG/MPS or
  another administrator-controlled GPU partitioning mechanism.
- External allocators can advise ordering but cannot bypass deterministic validation.
- External allocator output is bounded and allocator timeouts terminate the isolated process group.
- Telemetry stores only sanitized workload labels and keyed identities, not raw arguments,
  environments, stdout, or absolute script paths.
- Closed telemetry partitions carry record counts and SHA-256 checksums. Unknown future
  fields block compaction instead of being discarded.
- Open telemetry partitions validate complete batches before append, roll back detected
  partial writes, and repair only the final interrupted JSONL fragment after a crash.
- Audit events use the same validated append and rollback path. `bk log` scans at most
  64 MiB from the tail, filters by the process UID, bounds output, and treats malformed
  records as warnings; read-only doctor checks report an interrupted tail without repairing it.
- Durable writes fsync both file contents and the containing directory. Directory-sync
  errors are never silently ignored: a valid WAL remains for idempotent deferred recovery,
  while telemetry, private job files, and service installation fail visibly.
- Trusted configuration can live outside the writable ledger through the automatically
  discovered `/etc/gpubk/config.json` or an explicit `BK_CONFIG_FILE`. GPUbk canonicalizes
  its parent, pins every directory component and the leaf by file descriptor, rejects
  replaceable non-sticky directories, and never follows a leaf symlink.
- A monitor targeting a group-writable data directory requires a trusted root-owned system
  or external configuration and matching numeric `monitor_uid`. This prevents accidental or
  misconfigured telemetry writers that still use GPUbk; it does not stop a group member
  from bypassing GPUbk and modifying group-writable files directly.
- `usage/collector.json` is an atomic, versioned liveness hint. Its PID, hostname, and
  freshness are operator diagnostics, not proof of identity, authorization, or lock ownership;
  its capability gaps include stable device identifiers as well as process telemetry. A group
  member who can modify the data directory can also replace this advisory file.
- Fatal collector exits never overwrite the last heartbeat with a graceful `stopped` state.
  Partial rollups are flushed best-effort, the original error remains the service exit cause,
  and the kernel-backed single-writer lease is released in all cases.
- Applied telemetry maintenance and migration commands use the same writer role; dry-run
  inspection and public usage queries remain available to ordinary users.
- `bk reset` is disabled whenever the configured data-directory mode is writable by group
  or other users. Shared data removal requires an administrator-controlled offline procedure
  after stopping writers and taking a backup.

Administrator responsibilities:

- Configure a dedicated Unix group and correct setgid directory permissions.
- On a shared deployment, keep the system or external configuration in a root-owned
  directory such as `/etc/gpubk`, outside the group-writable ledger directory. Put one
  absolute `data_dir` in that file so every invocation resolves the same ledger. File mode
  alone cannot prevent rename or deletion by a user who can write its parent directory.
- Verify `flock` and atomic rename behavior on the actual NFS/FUSE mount.
- Control `/dev/nvidia*` access separately if hard enforcement is required.
- Run MCP over per-user local stdio unless an authenticated remote transport is deliberately engineered.
- Treat allocator commands as trusted code running with the configuring user's privileges.
- Run exactly one trusted telemetry writer per server. Do not expose `TelemetrySink` as an
  unauthenticated network write endpoint or allow users to submit records for arbitrary UIDs.
- Put the selected telemetry account's numeric UID in `monitor_uid`; do not reuse a username
  as an identity. Exit status 77 is a persistent role/configuration error, not a retry signal.
- The bundled monitor service bounds other failure restarts to three attempts per 60 seconds,
  allowing brief I/O recovery without retrying a persistent failure indefinitely.
- Review generated user units before enabling them. `bk service install` captures absolute data
  and private job-log paths, an explicit trusted config path, and validated values for explicitly
  active non-secret configuration overrides. It never captures the allocator command. Reinstall
  the unit after those paths or overrides change; prefer a trusted config file on shared hosts.
- Enabling systemd linger allows an account's user manager and background services to run
  without an active login. Grant it only to the selected monitor account and users who need
  unattended workers; disable it when that requirement ends.
- Keep one canonical `BK_JOB_LOG_DIR` per UID. The worker lease cannot coordinate invocations
  deliberately pointed at different private directories.
- Keep `worker_live_guard=true` on shared servers. Disabling it restores direct launch behavior
  and accepts collisions with activity that appeared after booking.
- Run `bk doctor --probe --strict` on the target mount before enabling services. Its lock check is
  cross-process on one host; shared NFS/FUSE deployments still require a second-host lock test.
- After enabling the monitor, run `bk doctor --require-monitor --strict`. Preflight intentionally
  permits a missing heartbeat because the service may not have started yet; post-start verification
  must not.
- After enabling each per-user worker, run `bk worker --status --require-running`. The probe is
  authoritative only for invocations resolving the same private job directory and lock-capable
  mount as that worker.
- Use plain `bk doctor --json --strict` for side-effect-free inspection. It does not recover a
  pending transaction or follow symbolic links at managed paths; `--probe` is the explicit
  temporary-write mode.
- Back up the complete `usage/` directory, including `workload.key`; losing only that key
  prevents stable workload identity from continuing and intentionally fails closed.

## Reporting

Do not include credentials, private command lines, production ledger contents, or user data in a public issue. Contact the package maintainers through the private security-reporting channel associated with the source repository or distribution. A public repository should enable private vulnerability reporting before the first release.
