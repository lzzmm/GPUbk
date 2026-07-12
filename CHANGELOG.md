# Changelog

All notable changes are documented here. The project follows Semantic Versioning once a public release is published.

## Unreleased

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
