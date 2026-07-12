# Security Policy

## Scope

GPUbk is a cooperative POSIX scheduler. It protects ledger integrity and enforces application-level UID ownership when every writer uses `bk`. It is not a kernel GPU access-control system and cannot prevent a user with direct device permission from launching CUDA outside GPUbk.

Supported security boundaries:

- MCP, CLI, TUI, and worker identity comes from the local process UID.
- Shared-ledger writes use an advisory lock, WAL journal, atomic replacement, and idempotent audit events.
- Shared data files reject symbolic links, FIFOs, devices, and other non-regular leaf files before reading or writing.
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
- Scheduled jobs re-check live process authorization and physical VRAM immediately before
  launch; this reduces races but cannot replace kernel device access control.
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

Administrator responsibilities:

- Configure a dedicated Unix group and correct setgid directory permissions.
- On a shared deployment, make `config.json` root-owned and not writable by group or other users; GPUbk rejects untrusted configuration files.
- Verify `flock` and atomic rename behavior on the actual NFS/FUSE mount.
- Control `/dev/nvidia*` access separately if hard enforcement is required.
- Run MCP over per-user local stdio unless an authenticated remote transport is deliberately engineered.
- Treat allocator commands as trusted code running with the configuring user's privileges.
- Run exactly one trusted telemetry writer per server. Do not expose `TelemetrySink` as an
  unauthenticated network write endpoint or allow users to submit records for arbitrary UIDs.
- Review generated user units before enabling them. `bk service install` captures absolute data
  and private job-log paths; reinstall the unit after those paths change.
- Keep one canonical `BK_JOB_LOG_DIR` per UID. The worker lease cannot coordinate invocations
  deliberately pointed at different private directories.
- Keep `worker_live_guard=true` on shared servers. Disabling it restores direct launch behavior
  and accepts collisions with activity that appeared after booking.
- Run `bk doctor --probe --strict` on the target mount before enabling services. Its lock check is
  cross-process on one host; shared NFS/FUSE deployments still require a second-host lock test.
- Use plain `bk doctor --json --strict` for side-effect-free inspection. It does not recover a
  pending transaction or follow symbolic links at managed paths; `--probe` is the explicit
  temporary-write mode.
- Back up the complete `usage/` directory, including `workload.key`; losing only that key
  prevents stable workload identity from continuing and intentionally fails closed.

## Reporting

Do not include credentials, private command lines, production ledger contents, or user data in a public issue. Contact the package maintainers through the private security-reporting channel associated with the source repository or distribution. A public repository should enable private vulnerability reporting before the first release.
