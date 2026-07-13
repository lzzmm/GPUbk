# Security Policy

## Scope

GPUbk is a local POSIX scheduler. In a shared installation, one non-root service
account owns the ledger and ordinary clients submit closed operations over a Unix
socket. It is not a kernel GPU access-control system and cannot prevent a user
with direct device permission from launching CUDA outside GPUbk.

Supported security boundaries:

- The broker takes client identity from Linux `SO_PEERCRED`; client-supplied UID and username
  fields are not authoritative. Private/direct mode uses the local process UID.
- Shared-ledger files are writable only by the configured service account. Broker writes use an
  advisory lock, WAL journal, atomic replacement, and idempotent audit events.
- Broker operations use a bounded length-prefixed JSON protocol, strict field allowlists, bounded
  clients, and timeouts. Booking, edit, and cancellation are revalidated server-side. Per-user
  workers may update only their own job state; scheduling fields, job binding, top-level policy,
  and every other UID's records remain immutable.
- Ledger and WAL reads validate reservation identity, devices, mode, status, and time ordering
  before scheduling. Unknown extension fields are preserved, but unknown current semantics fail
  closed; backup fallback accepts only a document that passes the same validation.
- Shared data files reject symbolic links, hard-linked aliases, FIFOs, devices, and other
  unsafe leaf files before reading or writing.
- Write paths require the configured file and directory modes exactly. Permission drift fails
  closed without silently running `chmod`; `bk doctor` reports the path for administrator repair.
- Legacy direct setgid storage remains supported for compatible private deployments. Its read-only health checks report managed ledger, backup, and telemetry
  paths whose numeric GID differs from the data directory; the deployment probe also verifies that
  a newly atomically replaced file inherits that GID. Ledger transactions, audit appends, telemetry
  writes, compaction, migration, and retention cleanup enforce the same GID before mutating data
  and never repair it implicitly.
- Legacy direct deployments may bind the data root to a numeric group with file-only `storage_gid`. This
  closes the case where the complete tree is internally consistent but belongs to the wrong Unix
  group. Ordinary environment variables cannot replace the trusted value; once enabled, the
  ledger policy also rejects clients which omit or change it.
- Scheduled command arguments, working directory, and a bounded `PATH` snapshot live in signed,
  UID-owned `0600` specs, not the shared ledger. No other submission environment is captured;
  `PATH` may reveal private directory names and therefore remains private with the command.
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
  A second kernel lease named by a privacy-safe instance digest binds the worker to the configured
  data directory. Positive readiness requires both the global and matching instance lock; lease
  PID, hostname, timestamps, and digest text are diagnostics only.
- Scheduled jobs re-check live process authorization and physical VRAM immediately before
  launch; this reduces races but cannot replace kernel device access control.
- A process listed by NVML without a readable numeric UID is never treated as authorized. The
  collector publishes `process_identity_gap`, guarded launches fail closed, and strict monitor
  verification remains degraded until attribution recovers. Prefer a narrowly assigned procfs
  visibility group over running the monitor as root.
- Scheduled process groups receive TERM during the configured final grace window and KILL at the
  reservation deadline, so graceful shutdown time is charged to the current booking rather than
  leaking into the next one. Cancellation and worker shutdown use a bounded post-event grace.
- Fatal worker cleanup force-kills and reaps supervised process groups without depending on a
  successful ledger read. If durable state cannot be updated, restart recovery keeps the job
  uncertain instead of claiming completion or retrying automatically.
- Worker and monitor startup, daemon cycles, and locked worker mutations validate the ledger-bound
  policy. Exit `78` is a persistent operator error: the bundled units do not restart it. A runtime
  mismatch terminates locally supervised commands without committing shared completion state and
  discards buffered monitor rollups instead of writing under an untrusted policy.
- The default launch guard passes the stable UUIDs from that same NVML snapshot to
  `CUDA_VISIBLE_DEVICES`, because NVML indices are not guaranteed to match CUDA ordinals.
  Missing identifiers fail closed on real NVML devices; disabling the guard explicitly accepts
  numeric-device compatibility risk.
- Shared capacity units enforce ledger admission and inferred memory budgets only. They do
  not enforce proportional SM time, memory isolation, or performance without MIG/MPS or
  another administrator-controlled GPU partitioning mechanism.
- External allocators can advise ordering but cannot bypass deterministic validation.
- Ledger policy is validated before allocator invocation. Output is bounded, and timeouts,
  exceptions, or process interrupts terminate the isolated allocator process group.
- Exact operation-ID replays are matched against the committed request before GPU probes,
  external allocators, or new private-spec files run. Concurrent first submissions still pass
  through the locked transaction; a replay reports unknown live state instead of presenting
  unobserved telemetry as current.
- Telemetry stores only sanitized workload labels and keyed identities, not raw arguments,
  environments, stdout, or absolute script paths.
- Bundled Skill installation ignores relative `CODEX_HOME` values, rejects force-replacing a
  symbolic link or the active working-directory tree, and rolls back a failed staged replacement
  before reporting failure.
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
- Private command specs are created, opened, scanned, and removed relative to validated
  UID-owned directory descriptors. Partial writes are removed on interrupts, linked aliases are
  rejected, and ambiguous booking rollback rereads the recoverable ledger before deleting only an
  unreferenced spec. Operation-ID retries compare the private command digest, not just its public
  summary; raw command arguments remain outside the shared ledger.
- Trusted configuration can live outside the writable ledger through the automatically
  discovered `/etc/gpubk/config.json` or an explicit `BK_CONFIG_FILE`. GPUbk canonicalizes
  its parent, pins every directory component and the leaf by file descriptor, rejects
  replaceable non-sticky directories, and never follows a leaf symlink.
- A shared monitor requires the trusted root-owned broker configuration and matching numeric
  `monitor_uid`. It runs as the service account and is the only usage-history writer.
- `usage/collector.json` is an atomic, versioned liveness hint. Its PID, hostname, and
  freshness are operator diagnostics, not proof of identity, authorization, or lock ownership;
  its capability gaps include stable device identifiers, process telemetry, and numeric process
  identity attribution. Only the service account can replace it in broker mode.
- Fatal collector exits never overwrite the last heartbeat with a graceful `stopped` state.
  Partial rollups are flushed best-effort, the original error remains the service exit cause,
  and the kernel-backed single-writer lease is released in all cases.
- Applied telemetry maintenance and migration commands use the same writer role; dry-run
  inspection and public usage queries remain available to ordinary users.
- `bk reset` is disabled for broker-backed storage. `bk admin uninstall` requires a root-owned
  install manifest, a stopped broker and monitor, idle writer locks, unchanged managed
  configuration, known managed paths, and explicit `--purge-data` before deleting non-empty state.
- `bk admin transfer` requires root and a stopped broker and monitor. It holds a maintenance
  socket plus both writer locks, rejects links, hard links, special files, unknown top-level paths,
  owner or mode drift, and records root-only checksummed snapshots before changing ownership. A
  failed operation rolls back; an interrupted operation blocks uninstall until explicit recovery.

Administrator responsibilities:

- Choose the local socket access policy explicitly. `bk admin init` defaults to `0666` on the
  broker socket so every local account can use GPUbk; ledger files remain service-owned `0644`
  inside service-owned `0755` directories. Use `--access group --group NAME` to restrict socket
  connections to an existing group.
- Run the broker and monitor under one selected non-root account. The account that invoked `sudo`
  is the default and is fully supported; a dedicated non-login account is optional operational
  isolation. GPUbk never creates, deletes, or changes accounts, groups, or memberships. Use
  `bk admin transfer` instead of copying state or editing identity fields during a handoff.
- On a shared deployment, keep the system or external configuration in a root-owned
  directory such as `/etc/gpubk`, outside the service-owned ledger directory. Put one
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
- Keep XDG base-directory variables absolute. GPUbk ignores empty or relative XDG values and uses
  the account's absolute HOME defaults, preventing ledger, private worker, or unit paths from
  changing with a caller's working directory. Explicit private job-log paths must be absolute.
- Enabling systemd linger allows an account's user manager and background services to run
  without an active login. Grant it only to the selected monitor account and users who need
  unattended workers; disable it when that requirement ends.
- Keep one canonical `BK_JOB_LOG_DIR` per UID. The worker lease binds its owner to one data
  directory, but cannot coordinate invocations deliberately pointed at different private
  directories.
- Keep `worker_live_guard=true` on shared servers. Disabling it restores direct launch behavior
  and accepts collisions with activity that appeared after booking.
- Treat daemon exit `78` as configuration drift. Compare the trusted file and `bk config` output
  with the ledger policy; never work around it by weakening capacity, storage, memory, or
  granularity settings in a user environment.
- Run `bk doctor --probe --strict` as a normal user after starting the broker to verify the socket
  and kernel-authenticated identity path. Run it again as `monitor_uid` to verify atomic writes,
  locks, GPU telemetry, and cross-user process attribution. A quiet host may leave the latter
  capability unproven. NFS/FUSE deployments still require a second-host lock test.
- After enabling the monitor, run `bk doctor --require-monitor --strict`. Preflight intentionally
  permits a missing heartbeat because the service may not have started yet; post-start verification
  must not.
- After enabling each per-user worker, run `bk doctor --require-worker --strict`. The check is
  authoritative only for invocations resolving the same data directory, private job directory,
  and lock-capable mount as that worker. `bk worker --status --require-running` remains the
  lower-level worker-only diagnostic.
- Use plain `bk doctor --json --strict` for side-effect-free inspection. It does not recover a
  pending transaction or follow symbolic links at managed paths; `--probe` is the explicit
  temporary-write mode.
- Back up the complete `usage/` directory, including `workload.key`; losing only that key
  prevents stable workload identity from continuing and intentionally fails closed.

## Reporting

Do not include credentials, private command lines, production ledger contents, or user data in a public issue. Contact the package maintainers through the private security-reporting channel associated with the source repository or distribution. A public repository should enable private vulnerability reporting before the first release.
