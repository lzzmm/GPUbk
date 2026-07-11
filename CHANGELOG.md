# Changelog

All notable changes are documented here. The project follows Semantic Versioning once a public release is published.

## 0.1.0 - Unreleased

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
