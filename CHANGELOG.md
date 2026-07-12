# Changelog

All notable changes are documented here. The project follows Semantic Versioning once a public release is published.

## 0.2.0 - Unreleased

- Enforce scheduled-command reservation boundaries by sending TERM during a configurable
  pre-deadline grace window and KILL at the exact deadline, while retaining the same bounded grace
  after cancellation or worker shutdown and aligning the bundled systemd stop timeout.
- Preserve explicitly active, non-secret `BK_*` configuration overrides in generated systemd
  units so unattended monitor and worker services cannot silently revert to defaults or hit a
  ledger-policy mismatch after the installing shell exits.
- Let the scheduled-command worker run legal same-GPU shared reservations concurrently instead
  of serializing them by physical GPU count, with a configurable safety cap and topology-bounded
  effective limit exposed to operators and Agents.
- Align read-only recommendations, TUI previews, and writes around the same wall-clock
  expiration boundary: ignore legacy records that already ended, still queue behind live
  non-aligned records, and reject new exact starts before the current booking slice without
  breaking idempotent retries.
- Make contextual CLI help side-effect free, add an explicit `bk book` alias and
  `bk help COMMAND`, and expose the usage-command overview instead of accidentally
  entering guided, TUI, MCP, or default-query execution paths.
- Extend the backward-compatible collector v1 heartbeat with per-GPU stable-device-identifier
  capability, degraded-state propagation, immediate recovery updates, and strict post-start
  doctor checks so monitor health cannot overstate guarded job readiness.
- Bind scheduled commands to stable CUDA-compatible GPU UUIDs from the same NVML snapshot that
  passed the live launch guard, fail closed when real devices lack an identifier or process list,
  and expose the capability to deployment checks and Agents instead of assuming NVML indices equal
  CUDA ordinals.
- Preserve known zero VRAM usage in Agent context instead of collapsing it into unavailable
  telemetry, and document the stable zero-versus-null contract.
- Surface per-UID worker readiness in plain status and the TUI only while a scheduled command may
  still run, using a read-only rate-limited probe that stops after the job reaches a terminal state;
  make the TUI refresh key invalidate both monitor and worker status caches immediately.
- Bind scheduled-command create/edit results to an immediate per-UID worker liveness check, with
  actionable human warnings and the same structured evidence in JSON CLI and MCP responses.
- Add a read-only, versioned per-UID worker liveness probe backed by the existing kernel lease,
  with strict post-start verification and consistent CLI, jobs, Agent/MCP, Skill, and service
  installation visibility.
- Publish an atomic, versioned collector heartbeat with capability-aware degraded states,
  crash staleness and policy-topology detection, graceful-stop reporting, and consistent
  Usage API, Agent, doctor, CLI, and TUI visibility.
- Add an explicit read-only `bk doctor --require-monitor` post-start check so deployment
  acceptance cannot pass before the long-running collector has produced a healthy heartbeat.
- Recover NVML after transient initialization or stale-handle failures, parse `nvidia-smi` as bounded CSV, preserve UID attribution when process commands are restricted, and prevent fallback telemetry gaps from creating false process lifecycle events.
- Require deployment probes to match the complete configured GPU topology and expose independent process-list and per-process-utilization capabilities to operators and Agents.
- Exercise the optional `nvidia-ml-py` dependency and no-GPU degradation path in a dedicated CI job so hardware-adapter API drift cannot hide behind core-only tests.
- Separate trusted `BK_CONFIG_FILE` from the group-writable ledger, pin and validate its complete directory chain, and capture the canonical path in generated services.
- Make monitor sampling and rollup cadence configurable with validated timing relationships, CLI overrides, and Agent-visible effective policy.
- Apply the configured scheduling-load window to historical placement scores instead of silently truncating prediction input to 30 minutes.
- Drive curses polling and every TUI refresh label from configurable `tui_refresh_seconds` while retaining the one-second default.
- Remove hard-coded monitor timing flags from the bundled systemd unit so services honor the trusted runtime configuration on every start.
- Require a root-owned shared-server configuration and matching file-only `monitor_uid` before monitor startup, service installation, or applied telemetry maintenance/migration, with doctor and Agent visibility.
- Bound monitor service recovery to three attempts per 60 seconds while keeping duplicate-writer and role failures non-restartable.
- Apply the same bounded startup-failure recovery to per-user worker services without treating ordinary child-command failures as daemon failures.
- Disable global `bk reset` for shared data-directory modes so ordinary users cannot erase reservations, audit logs, backups, or telemetry.
- Explain selective systemd linger in service-install output and deployment docs so unattended user services survive logout without silently changing host policy.
- Reject explicit past edit starts even when queueing is enabled, and reject zero duration or GPU count instead of treating them as unchanged Agent input.
- Auto-discover a trusted `/etc/gpubk/config.json` with a required absolute `data_dir`, preventing new SSH, MCP, and service sessions from silently splitting onto per-user ledgers.
- Validate scheduling-critical reservation fields before ledger or WAL use, preserve unknown extension fields, and accept backup fallback only after the same semantic validation.
- Fail closed with an actionable installer upgrade message when an old Debian/Ubuntu pip ignores the required isolated setuptools and would otherwise build an unusable `UNKNOWN` source package.
- Validate configuration with a versioned closed schema, bounded finite values, typo hints, and a read-only redacted `bk config` report that detects ledger-policy drift.
- Make booking granularity configurable through policy-bound `slot_minutes`/`BK_SLOT_MINUTES`, with consistent scheduler, CLI, TUI, Agent, and MCP behavior while retaining a 5-minute default.
- Propagate directory-fsync failures across WAL, ledger, telemetry, private job files, and systemd unit installation instead of reporting uncertain writes as durable.
- Reject permission drift and hard-linked aliases before managed writes, validate telemetry directory modes component by component, and report both conditions read-only through `bk doctor`.
- Stream chronological telemetry queries without materializing a daily partition, while verifying closed gzip data before parsing it from the same pinned inode.
- Preserve a valid renamed journal for deferred idempotent recovery and surface the warning even in quiet CLI booking flows.
- Repair interrupted telemetry and audit JSONL tails, roll back failed append batches, and reject files beyond reader limits.
- Tail-read recent per-UID audit events with bounded memory, machine-readable output, corruption warnings, and read-only doctor checks.
- Keep version, help, and bundled Skill commands usable when shared configuration is broken.
- Require the patched pip 26.1.2 security floor in dependency-audit jobs.
- Document the draft-first workflow required by GitHub immutable releases.
- Start implicit `now` reservations in the active 5-minute interval instead of delaying them to the next boundary.
- Promote the plain CLI with natural `--at` times, recoverable guided Add/Edit, compact status, fixed-cell timelines, and copyable read-only `slots` alternatives.
- Align compact status with allocator live-state rules so display-server contexts do not make idle GPUs look busy.
- Replace the reservation table's numbered header and dot map with a plain `GPU` header and position-aligned device numbers.
- Compact TUI GPU labels, neutral header focus, auto-framed Add/Edit zoom, quick duration controls, and reliable speed levels.
- Add a versioned, sparse telemetry store with checksummed daily partitions, explicit legacy migration, and 1-minute through daily retention tiers.
- Separate collection, storage, workload classification, and `gpubk.usage.v1` querying behind public Python, JSON CLI, and UID-bound MCP interfaces.
- Classify common Python, distributed, service, notebook, container, and native workloads without storing raw arguments or absolute paths.
- Recover process-event and state updates through an idempotent journal, and fail closed before unknown future fields can be lost during compaction.
- Capture absolute data and private job-log paths in generated systemd units so unattended services cannot silently fall back to another ledger.
- Make duplicate telemetry monitors fail quickly with a dedicated exit status and prevent systemd restart loops while retaining kernel-released single-writer locking.
- Release file-lock descriptors immediately when lock metadata persistence fails, avoiding transient false deadlocks during storage errors.
- Add `bk doctor --probe --json --strict` for cleanup-safe atomic-replace, fsync, process-lock, permission, disk-space, and real GPU telemetry deployment checks.
- Recheck process authorization and physical VRAM immediately before scheduled commands launch; unsafe jobs remain pending with a stable reason instead of colliding with live work.
- Add backward-compatible weighted shared capacity with `--share`/`--share-with`, share-weighted VRAM inference, Agent/MCP fields, proportional TUI subcells, and atomic concurrency tests.
- Use adaptive six-character reservation prefixes in the TUI, preserve booking links in narrow process tables, and distinguish per-booking Share from current GPU capacity use.
- Show total occupied capacity in CLI timeline slices, mark current exclusive use in the aligned TUI GPU metrics, and keep the `NOW` label from leaving partial minute ticks on narrow terminals.
- Fail closed without crashing Agent context when a legacy reservation contains malformed shared-capacity metadata.
- Keep plain `bk doctor` strictly read-only: do not recover pending transactions or follow unsafe managed paths, and report malformed records and backup fallback as structured issues.
- Preserve subsecond precision before aligning relative and queued future starts, preventing `--at +30m` from starting fractionally early at an exact slot boundary.
- Add UID-local scheduled-command lifecycle cleanup across CLI, TUI, Agent, MCP, and workers, while retaining runnable/retryable specs and protecting concurrent submissions with a 24-hour orphan grace period.
- Bound direct scheduled-job output with rolling private logs, age and per-UID quotas, process-group tracking, and explicit CLI/MCP cleanup reports.
- Prevent duplicate per-UID workers and recover abandoned claimed/running jobs with same-UID process verification, bounded TERM/KILL cleanup, cross-host fail-closed behavior, and explicit uncertain state.

## 0.1.0 - 2026-07-12

- Use a concise English README by default and ship a matching Simplified Chinese guide in source distributions.
- Adopt GPUbk as the public project brand and `gpubk` as the PyPI distribution while retaining the `bk` command and protocol namespace.
- Add 5-minute shared/exclusive scheduling with atomic queueing and VRAM admission.
- Add compact curses TUI, date/weekday timeline, shared lanes, and interactive Add/Edit.
- Add NVML monitoring, privacy-safe usage audits, historical load forecasts, and live-aware placement.
- Add per-user scheduled job workers with private specs and automatic `CUDA_VISIBLE_DEVICES`.
- Add WAL recovery, configurable private/shared file modes, backups, and concurrent-process tests.
- Add stable JSON agent context/recommendation, advisory external allocator protocol, MCP server, and bundled Codex Skill.
- Bind audit display names to the process UID and render user-level systemd units with the active Python installation.
- Reuse a parsed per-GPU reservation index, tail-read audit logs, and bound the hot-ledger retention window.
- Expose GPU model and temperature in privacy-safe Agent and MCP context.
- Auto-detect visible GPUs when no administrator count is configured, while preserving explicit limits.
- Keep TUI headers and keyboard hints complete on terminals as narrow as 72 columns.
- Pin all third-party GitHub Actions to immutable commits and test that release invariant.
- Fail closed on an unreadable ledger without a valid backup, while still allowing durable journal recovery.
- Add machine-readable MCP risk annotations for read-only, idempotent, destructive, and closed-world tools.
- Bind shared ledgers to one scheduling and storage policy so per-user environment overrides cannot silently change capacity rules.
- Keep ordinary read-only CLI, Agent, and MCP calls free of empty-directory initialization side effects while preserving durable WAL recovery.
- Reject untrusted shared configuration, symbolic-link and special-file redirection across ledger, audit, and private job paths.
- Bound external allocator output and terminate its whole process group on timeout or protocol overflow.
- Adopt Apache-2.0 and publish maintainer and GitHub project metadata for the `gpubk` distribution.
- Add a paged colorized TUI help center, explicit `f`/`g`/`r` guidance, and an embedded Quick Tour.
- Center the live timeline around a visible NOW marker, permit read-only reservation history browsing, and keep GPU capacity/utilization/memory columns aligned.
- Reject edits after a reservation starts or when an exact replacement start is in the past across both TUI and scheduler APIs.
- Add auto-detected dark/light TUI themes, a live theme toggle, and terminal-default neutral text for readable black and white backgrounds.
- Normalize editable and installed coverage paths in CI so subprocess data cannot count the same package twice and falsely fail the coverage gate.
- Make secure-config tests independent of the host umask used on shared lab servers.
- Show allocatable free VRAM in compact GPU rows and never render partially truncated trailing metrics.
- Add token-free Trusted Publishing through TestPyPI verification and protected PyPI promotion of one immutable artifact.
- Add package-structure, metadata, and medium/high-severity static security gates to CI.
- Replace optimization-sensitive production assertions with explicit fail-closed runtime checks.
- Keep Agent recommendations and telemetry-history reads side-effect free when the data directory does not exist.
- Add retry-safe structured Agent and MCP edits, structured Agent cancellation, capability discovery, and operation-intent mismatch rejection.
