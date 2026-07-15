# Changelog

All notable changes are documented here. The project follows Semantic Versioning once a public release is published.

## 0.2.1 - 2026-07-15

- Add non-destructive cluster node disable/enable operations, skip maintenance nodes
  without SSH delays or placement, retain their identity mappings and history, and add
  `bk cluster check` for per-user reachability, identity, clock, GPU, and write-capability
  validation. Human cluster times now use the local timezone while JSON remains UTC.
- Make `bk admin install` idempotent for an existing managed deployment: preserve
  configuration and data, reject accidental policy changes, reconcile tracked command
  and unit files, perform a controlled restart, and recover service availability after
  a post-stop failure.
- Make `sudo bk admin install` safely create and track `/usr/local/bin/bk`, preserve an
  identical link that predates GPUBK, reject unknown command paths, resume interrupted
  link creation, and remove only GPUBK-owned links during the tracked uninstall.
- Add `sudo bk admin install`, a guided one-command shared-server setup that writes the
  trusted configuration, installs tracked boot services, optionally installs the login
  reminder, and keeps preview, unattended, and install-without-start modes.
- Turn `bk u` into a colored personal terminal dashboard with a shared 7-day/4-week trend
  model, add the same summary behind `u` in the TUI, and keep future reservations excluded.
- Make plain `bk w` a safe read-only worker status command; use `bk w start` or `bk w once`
  for explicit execution, with full long forms retained for services and automation.
- Add concise booking and usage flags, color-aware help and status output, and a reliable
  login warning when a fresh monitor sees the user still occupying a GPU after expiry.
- Persist a versioned stable node identity and GPU UUID in new usage records, keep legacy
  history readable, prevent same-index GPUs from different nodes merging during rollups,
  and expose the migration boundary without claiming multi-host scheduling support.
- Publish a GitHub Release with the exact distributions and checksums already verified on
  PyPI, after the production tag workflow succeeds.
- Add stopped-service administrator data backup, verification, backup-required atomic clear,
  and empty-target restore commands with streaming SHA-256 checks and safe-tree validation.
- Add optional cluster federation with trusted node catalogs, bounded SSH queries, parallel
  status and recommendation, single-node remote booking, node-qualified edits/cancels,
  usage aggregation, and administrator `(node_id, UID)` principal mappings. It remains
  hidden without a catalog and never turns NFS into a shared live ledger.
- Bound cluster subprocess output while it is produced, cancel pending TUI refreshes on exit,
  gate remote writes by advertised capabilities, and recover ambiguous writes by querying and
  replaying the same operation ID only on the originally selected node.
- Make cluster cancellation idempotent, keep its operation record in the existing version-1
  ledger, and compare implicit cross-node starts by relative wait when clocks are skewed.
- Add optional per-node cluster history archives with daily public API payloads, gzip,
  checksummed immutable generations, atomic publication, incremental export, full verification,
  and read-only principal aggregation; live ledgers and telemetry writers remain per-host.
- Pin an explicit cluster operation replay to the node where its operation ID already exists,
  reject ambiguous cross-node IDs and unknown-node retries, and verify recovered write actions.
- Add a no-production-state multi-host acceptance runner that installs the candidate wheel in
  private temporary SSH directories, uses simulated GPUs and isolated ledgers, exercises routing,
  replay, and cancellation, writes a private report, and cleans every remote stage.
- Keep all cluster and subcommand help available before a catalog exists, provide an actionable
  first-setup error, add short structured-output flags, and make edit/cancel return versioned
  cluster JSON with caller-supplied retry-safe cancellation operation IDs.
- Isolate cluster catalog and history administration from the shared administrator module, keep
  its root and telemetry-owner boundaries intact, list identity members in human status, add a
  safe `unmap` correction path, and package-test the new module explicitly.
- Normalize cluster booking intent once for recommendation and commit, and ignore a malformed
  recommendation from one node when another node still has a valid legal placement.
- Repair the GPU-focused TUI reservation and process tables: align every column, put the row
  number and booking ID first, show expected VRAM and scheduled job summaries, and distinguish
  reservation IDs from live process commands.
- Let administrators atomically switch an existing deployment between required and implicit
  shared-memory estimates with `bk admin gpu-policy`, including the existing lock, journal,
  manifest-integrity, rollback, and recovery protections.
- Add `bk g` / `bk gpu`, a read-only one-glance command that shows the current user's active
  GPUs with live utilization and free VRAM, or recommends one legal short-task GPU.
- Clarify that usage totals are sampled history only, explain the daily and weekly charts, and
  make shared-server install, upgrade, policy, service, and uninstall examples explicit about
  administrator privileges.

## 0.2.0 - 2026-07-15

- Add `bk run`: it can launch a command on the current UID's active reservation with stable
  CUDA device binding and a hard reservation-end deadline, or book the earliest legal slot and
  hand an immediate/future command to the bounded worker.
- Make guided booking default to one GPU for 30 minutes, expose fixed and excluded GPU choices,
  and let strict shared-memory policy resolve `auto` into a conservative integer MiB/GPU budget
  from live capacity telemetry instead of rejecting it after review.
- Expand compact status reservations into aligned Slot, GPU, user, VRAM/GPU, job, start, end, and
  duration columns on wide terminals; render fixed-position GPU maps with muted empty cells and
  align TUI utilization percentages at the column boundary.
- Label usage summaries as historical sampled data that excludes future reservations, and add
  compact seven-day daily and eight-week weekly active/reserved GPU-hour charts to `bk usage`.
- Split ordinary automation help from administrator-only operations, hide the administrator block
  for other UIDs, retain read-only `bk doctor` for users, and document six/eight-character IDs as
  collision-checked prefixes of the same stored UUID.

- Add an optional, bounded interactive-login notice for each UID's active and nearest
  reservation. It is read-only, silent when empty, protected by a one-second shell timeout,
  refuses unknown profile files, and is removed by the tracked administrator uninstall.
- Separate compact TUI utilization and free-memory fields with a fixed column boundary so
  one-, two-, and three-digit utilization values remain visually aligned.
- Package the self-cleaning live telemetry acceptance workload as `bk usage demo`, with an
  interactive confirmation, active-environment CUDA detection, idle-GPU admission, and automatic
  reservation cleanup.
- Stabilize scheduled-worker tests across configured booking-slice boundaries by fixing their
  injected scheduler clock.
- Fail release workflows explicitly when a TestPyPI or PyPI publication gate is disabled,
  instead of reporting a successful run that silently skipped every publish job.
- Limit the system monitor's writable systemd paths to its telemetry/ledger data directory; the
  broker socket directory remains writable only by the broker service.
- Keep the monitor compatible with NVIDIA NVML under systemd by avoiding `ProtectClock` and its
  implicit device cgroup allowlist. The service remains non-root and retains its other filesystem,
  privilege, kernel, and namespace protections; normal Unix device permissions still apply.
- Treat unavailable per-process SM utilization as an explicit optional capability rather than a
  deployment blocker when device telemetry, process discovery, UID attribution, and stable GPU
  identifiers are all available. Existing degraded collector documents remain readable.
- Compare every TestPyPI and PyPI wheel and source distribution SHA-256 against the original CI
  build before accepting an index verification or continuing a production release.
- Make the no-GPU demo portable to systems where `/tmp` is a symbolic link, while retaining the
  fail-closed directory policy and reporting the rejected path clearly.
- Exercise the real update path in every release build by creating a ledger with the latest public
  package, installing the candidate wheel over it, proving read-only compatibility, and performing
  a new weighted-capacity write.
- Keep version queries on a small process entrypoint instead of importing scheduling, monitoring,
  worker, and TUI modules before printing the package version.
- Tighten the compact TUI with aligned GPU capacity/utilization/VRAM fields, responsive action
  hints, cursor-following GPU and reservation views, and read-only details for any visible
  reservation.
- Add administrator-controlled disabled GPU and preference-tier scheduling, per-request GPU
  exclusion, and a stopped-service `bk admin gpu-policy` transaction with atomic rollback and
  explicit crash recovery for the trusted configuration and install manifest.
- Keep broker worker updates bounded on large ledgers by sending only changed reservations under
  digest compare-and-swap, while preserving the old full-ledger operation for rolling upgrades.
- Default new shared-server deployments to automatic share-weighted VRAM estimates. In strict
  memory mode, `auto` resolves to an integer per-GPU budget from live capacity telemetry and fails
  closed when capacity is unavailable; an explicit `--mem` value remains accepted.
- Standardize the public brand as GPUBK and add `bk info`, TUI `i`, JSON, and Agent context
  access to the responsible Linux administrator account and its sanitized GECOS contact fields.
- Render systemd path directives without surrounding quotes so system services load correctly on
  releases that treat quoted `WorkingDirectory=` values as non-absolute paths.
- Create the broker socket with its final configured mode at bind time, closing a
  startup race where an early client could observe and reject a transient `0700` mode.
- Add a replayable, read-only CLI tutorial, a first-launch TUI tour, and private
  per-user onboarding markers that never touch shared reservation data.
- Add resumable, root-tracked systemd units for boot-persistent broker and monitor operation.
  Units run under the selected non-root administrator UID, use service hardening, refresh safely
  during upgrades, follow recoverable administrator transfers, and restore prior files on removal.
- Default shared-server initialization to the non-root account that invoked `sudo`, and add a
  dry-runnable, recoverable `bk admin transfer` transaction for handing broker and monitor
  ownership to another existing account without rewriting reservations, user UIDs, audit events,
  usage history, or scheduling policy.
- Put shared-server mutations behind a local Unix-socket broker owned by one existing service
  account. Linux kernel peer credentials bind each request to the connecting UID, while normal
  users retain read-only access to the ledger and cannot submit a forged identity.
- Restrict broker-backed scheduled-command updates to the caller's own immutable reservation and
  an explicit job-state allowlist, with compare-and-swap retries for concurrent workers.
- Track administrator initialization in a root-only install manifest and add a dry-runnable
  `bk admin uninstall` that restores replaced configuration and pre-existing empty-directory
  metadata, refuses drift or active services, and purges only validated GPUBK data on request.
- Teach `bk doctor --probe` to verify broker connectivity for ordinary users and retain direct
  durability probes for the service account that owns shared state.
- Add `bk admin init`, a guided, dry-runnable, idempotent shared-server initializer with atomic
  root configuration writes. It defaults to group-free access for all local users, keeps an
  existing Unix group as an optional trust boundary, and refuses silent group creation, user
  membership changes, background-service activation, or policy replacement on non-empty data.
- Make bundled Codex Skill installation resolve to an absolute per-user directory, reject
  force-replacing symbolic links or an active working tree, and restore the previous Skill when
  staged replacement fails.
- Exercise the built zero-dependency wheel through a complete simulated scheduled-command flow
  in package CI, including booking, worker execution, GPU environment injection, terminal state,
  private logging, and command-spec cleanup.
- Replace production runtime assertions with explicit fail-closed handling so optimized Python
  preserves workload-dictionary and private-job cleanup safety; exercise the full suite under
  `python -O` in CI.
- Resolve data, private job, and user-unit defaults through one XDG-compliant absolute-directory
  policy. Empty or relative XDG values now fall back to HOME instead of drifting with the current
  working directory, and an explicit private `job_log_dir` must be absolute at config-load time.
- Snapshot the submitting process's `PATH` in a signed, UID-private v2 scheduled-command spec so
  bare executables keep the same lookup semantics under a restarted systemd worker. No other
  environment variable is captured; v1 specs, operation-ID replays, and exact duplicates remain
  compatible, while changing `PATH` is treated as a different new command intent.
- Add a read-only Linux procfs deployment probe for cross-UID process ownership visibility.
  Strict preflight now requires the configured monitor account and refuses to claim production
  readiness when process attribution is blocked or has not been demonstrated on the target host.
- Degrade collector health when an observed GPU process cannot be attributed to a numeric UID.
  The additive `process_identity_gap` field reaches doctor, Agent, TUI, and usage APIs; legacy v1
  heartbeats remain readable but are conservative until a current monitor replaces them.
- Add `bk doctor --require-worker` for a read-only, instance-bound deployment check of the
  current UID's scheduled-command worker. Doctor JSON now includes the privacy-safe worker status,
  while ordinary checks do not require the optional per-user service.
- Resolve exact create and edit operation-ID replays before GPU probing, external allocation, or
  private-spec writes. Replay responses preserve the committed allocation with explicit
  `idempotent-replay` provenance, while concurrent first submissions still converge through the
  locked scheduler transaction and new commands validate their working directory before allocation.
- Pin private scheduled-command spec creation, reads, and deletion to validated UID-owned
  directory descriptors; remove partial files on process interrupts, reject linked aliases, and
  recheck the recovered ledger before rollback so an ambiguous interruption cannot delete a
  committed command. Idempotent operation IDs now also verify the stable private-command digest.
- Validate ledger policy before edit-time allocator invocation, and terminate the allocator's
  isolated process group on interrupts as well as ordinary errors and timeouts so rejected edits
  and Ctrl+C cannot leave external side effects running.
- Fail worker and monitor startup closed when their trusted configuration disagrees with the
  ledger, revalidate every daemon cycle and each worker transaction, discard buffered telemetry
  on runtime policy drift, and reserve non-restarting exit status `78` for operator repair.
- Bind each UID-private worker lease to a privacy-safe data-directory instance ID, so a worker
  serving another ledger or an old worker without the matching instance lock cannot falsely
  satisfy scheduled-command readiness checks in CLI, TUI, Agent, or MCP views.
- Verify setgid GID inheritance during deployment preflight, report numeric group drift across
  existing ledger, backup, and telemetry paths, and fail every shared write path before mutation
  when an existing or newly created managed path does not retain the data directory's group; add
  optional file-only, ledger-bound `storage_gid` to bind the data root to the intended Unix group
  as well.
- Require production release tags to be annotated and point exactly at the current `main` tip,
  preventing a valid-looking version from being published from an older main-history commit.
- Keep collector crash evidence honest: fatal sampling failures attempt a partial rollup flush
  without publishing a graceful `stopped` heartbeat, preserve the original failure for systemd,
  and always release the single-writer lease.
- Force-kill and reap locally supervised job groups even when a worker crashes through an
  unavailable ledger reconciliation path, leaving durable state for explicit uncertain recovery
  instead of allowing a TERM-ignoring command to escape the failed worker.
- Let the plain CLI timeline select past intervals and render retained expired reservations,
  matching the TUI history view while continuing to hide cancelled bookings.
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
- Add integer weighted shared slots with `--share SLOTS`, slot-weighted VRAM inference,
  integer Agent/MCP fields, proportional TUI subcells, and atomic concurrency tests.
- Use adaptive six-character reservation prefixes in the TUI, preserve booking links in narrow process tables, and distinguish per-booking slots from current GPU capacity use.
- Show total occupied capacity in CLI timeline slices, mark current exclusive use in the aligned TUI GPU metrics, and keep the `NOW` label from leaving partial minute ticks on narrow terminals.
- Fail closed without crashing Agent context when a legacy reservation contains malformed shared-capacity metadata.
- Keep plain `bk doctor` strictly read-only: do not recover pending transactions or follow unsafe managed paths, and report malformed records and backup fallback as structured issues.
- Preserve subsecond precision before aligning relative and queued future starts, preventing `--at +30m` from starting fractionally early at an exact slot boundary.
- Add UID-local scheduled-command lifecycle cleanup across CLI, TUI, Agent, MCP, and workers, while retaining runnable/retryable specs and protecting concurrent submissions with a 24-hour orphan grace period.
- Bound direct scheduled-job output with rolling private logs, age and per-UID quotas, process-group tracking, and explicit CLI/MCP cleanup reports.
- Prevent duplicate per-UID workers and recover abandoned claimed/running jobs with same-UID process verification, bounded TERM/KILL cleanup, cross-host fail-closed behavior, and explicit uncertain state.

## 0.1.0 - 2026-07-12

- Use a concise English README by default and ship a matching Simplified Chinese guide in source distributions.
- Adopt GPUBK as the public project brand and `gpubk` as the PyPI distribution while retaining the `bk` command and protocol namespace.
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
