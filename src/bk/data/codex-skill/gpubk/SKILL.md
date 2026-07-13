---
name: gpubk
description: Inspect, recommend, create, edit, cancel, and monitor GPU reservations with GPUbk. Use when an agent needs to plan a GPU experiment, choose shared versus exclusive access, account for current and recent GPU load or expected VRAM, attach a command for scheduled execution, inspect job status, or safely automate GPUbk through its JSON CLI or MCP tools.
---

# GPUbk

Use GPUbk's structured interfaces. Do not scrape the TUI or human-readable tables.

## Choose An Interface

Prefer GPUbk MCP tools when available:

1. Call `get_gpu_context` for policy and live state.
2. Call `recommend_gpu_booking` before any write.
3. Call `create_gpu_booking` only when the user requested or approved the reservation.
4. Use `edit_my_gpu_booking` with a new stable operation ID when the user approves a change.
5. Use `get_my_gpu_usage` for current-UID historical utilization; do not infer it from reservation duration.

Otherwise use the JSON CLI:

```bash
bk agent context --compact
bk agent recommend 2 1h30m --mode shared --mem 12g --share 1/2 --compact
bk 2 1h30m --mem 12g --share 1/2 --op-id <stable-id> --json
bk agent edit <short-id> --duration 2h --op-id <stable-edit-id> --compact
bk agent cancel <short-id> --compact
bk log --limit 100 --json
```

Read [references/protocol.md](references/protocol.md) when implementing an integration or interpreting every field.

## Plan A Reservation

1. Determine GPU count, duration, shared/exclusive mode, earliest or exact start, expected VRAM per GPU, and any requested shared capacity.
2. For shared work, ask for expected VRAM and share intent when they materially affect placement. `share=3/4` reserves three capacity units on a four-unit server; `share_with=1` is the same asymmetric reservation. Two equal users should each request `1/2`.
3. If expected VRAM is unknown, state that GPUbk derives it from the requested share. Share units constrain admission; they do not physically enforce GPU compute bandwidth without MIG/MPS.
4. Inspect context immediately before recommending. Current processes can change quickly.
5. Run a read-only recommendation. Explain queued start, selected GPUs, confidence, live-busy warnings, and projected memory headroom.
6. Treat explicit start as exact. It may use the active slice boundary or a future boundary,
   never an older historical slice. Do not silently convert it to queueing.
7. Let GPUbk enforce conflicts and memory limits. Never infer that an unsafe placement is acceptable.

Use shared mode for workloads that can coexist within both capacity-unit and VRAM limits. Use exclusive mode when the experiment needs the whole device, has unpredictable memory behavior, or must avoid interference.

## Create Safely

- Generate one stable operation ID for each create or edit intent and reuse it only for exact retries.
- Never reuse an operation ID for changed fields, command arguments, working directory, or
  submission `PATH`; GPUbk verifies the private command digest and rejects mismatched reuse.
- Never pass, invent, or override a UID. GPUbk derives identity from the local process.
- Do not retry a write with a new operation ID after an ambiguous response; inspect reservations first.
- After an interrupted scheduled-command submission, retry with the same operation ID and exact
  command. GPUbk retains a committed referenced spec and prunes only a verified unreferenced one.
- When an exact retry returns `allocator.source=idempotent-replay`, treat it as committed-write
  confirmation. GPUbk skipped new live probes, external allocation, and private-spec creation;
  `unknown` live fields are intentional and do not prove that the command can launch now.
- Do not cancel or edit another user's reservation.
- GPUbk snapshots only `PATH` for a scheduled command, not the rest of the Agent environment.
  Put required variables and credentials in a user-owned wrapper or configuration file; do not
  expose secrets in command arguments.

To schedule a command:

```bash
bk 2 1h30m --mem 12g --op-id <stable-id> -- python train.py --config exp.yaml
```

GPUbk sets `CUDA_VISIBLE_DEVICES`; do not add physical GPU IDs to the training command.
For unattended work, require `capabilities.stable_device_identifier=true` on every recommended
GPU. The default live guard uses those stable identifiers and leaves the job pending if a real
NVML device cannot be bound safely.

## Handle Results

- `created`: reservation starts at the returned time.
- `updated`: the approved edit was applied.
- `queued`: reservation was moved to the earliest legal future slot because start was implicit.
- `exists`: the operation ID was already applied; treat this as idempotent success. Check worker
  state separately before promising that a scheduled command remains launchable.
- JSON exit `2`: invalid request or write conflict. Inspect `error.message`.
- Recommendation exit `3`: no legal exact slot; present `nearest_available` without booking it.
- Daemon exit `78`: trusted configuration does not match the ledger. Stop retrying and surface an
  operator repair action; never change policy limits to bypass it.
- Context VRAM values distinguish known zero from unknown: `memory.used_mb=0` means the GPU is
  known empty, while `null` means telemetry is unavailable. Never coerce one into the other.
- `uncertain` job: it may have run or produced partial side effects. Inspect `recovery_state`,
  processes, and private logs before using duplicate-risk retry. A recovered `terminated` group is
  still uncertain; never present it as safe automatic retry.
- `pending` with `launch_guard_state=waiting`: the reservation is active, but a live process or VRAM check blocked launch. Report `message`; do not bypass the guard unless the user explicitly accepts that collision risk.
- Cancellation results include `private_job_cleanup`; cancellation remains committed when cleanup
  reports a warning. Surface that warning instead of retrying the destructive operation.
- `bk log --json` returns only the current UID's bounded recent audit tail. Surface a non-null
  warning because it may indicate a damaged tail or that the 64 MiB scan ceiling was reached.

For edits, reject started reservations and explicit starts in the past. A valid explicit `start`
is exact and does not move unless `allow_queue=true` was explicitly requested to resolve a
resource conflict. Keep `bk worker` running for scheduled commands. Use `list_gpu_reservations`,
`bk j --json`, or the bounded job-log tool to inspect state. `cleanup_my_job_specs` and
`cleanup_my_job_logs` expose separate idempotent private cleanup operations. `bk j --cleanup
--json` runs both. Never remove runnable/retryable specs or logs; report cleanup warnings and
quota excess to the user.
Only one worker may hold a UID's private job directory. Exit `75` means another worker already
holds the lease; do not loop or start a second worker.
Capability `daemon_policy_guard=true` means worker transactions and monitor sampling revalidate
the bound policy. Context `policy.daemon_policy_exit_code` supplies the persistent-error status.
`policy.worker_effective_max_parallel` is the default scheduled-command concurrency after the
configured safety cap is bounded by GPU shared capacity. Do not interpret it as extra capacity.
Budget around `policy.worker_termination_grace_seconds`: scheduled commands receive TERM before
`end_at` and cannot consume that warning window as guaranteed compute time.
Before promising unattended execution, check `context.worker.running` or run
`bk worker --status --json`. Only `state=running` with `running=true`, `lease_held=true`, and
`instance_match=true` proves that the kernel lease belongs to this data directory; PID, hostname,
and acquisition time are diagnostic metadata. `stopped`, `not-seen`, `other-instance`,
`unverified`, `invalid`, and `unavailable` do not prove that a scheduled command will launch.
Create and edit `booking_result` payloads include this same `worker` document when the reservation
has a command, and `null` otherwise. Surface any worker warning immediately; booking success is not
evidence of unattended command execution.

## Respect Safety Boundaries

- External AI allocator output is advisory ordering only. Local time, capacity, VRAM, identity,
  ledger-policy, and transaction checks remain authoritative and run before edit-time allocator
  invocation where applicable.
- Treat live utilization as a soft forecast because running processes have no reliable end time.
- Check `policy.monitoring.collector.fresh` before treating recent telemetry as current. A
  degraded, stale, stopped, topology-mismatched, missing, or invalid collector is never proof
  that a GPU is idle.
- For unattended commands, also require an empty
  `policy.monitoring.collector.stable_device_identifier_gap` and
  `policy.monitoring.collector.process_identity_gap`; a fresh degraded collector is not sufficient.
- Do not delete journal or lock files manually.
- Never apply `bk admin init` without an explicit administrator request. Use its `--dry-run`
  form first. `access=all` exposes the broker socket to local accounts; it does not grant direct
  ledger write permission.
- Never apply `bk admin transfer` without an explicit administrator request. Require a dry-run,
  a stopped broker and monitor, and preserve its recovery journal after any interrupted handoff;
  never rewrite reservation UIDs or copy the live ledger as a substitute.
- Do not enable a worker, monitor, or service on a shared server without the user's or administrator's approval.
- Do not disable `worker_live_guard` merely to make a scheduled command start sooner.
- Before an approved service deployment, run `bk doctor --probe --json --strict` as a normal user
  to verify broker connectivity, then as the configured monitor UID to verify writes and process
  attribution. Do not treat a simulation or single-host NFS lock check as proof of the complete
  production boundary.
- After starting the monitor service, run `bk doctor --require-monitor --json --strict`; a
  preflight without a heartbeat does not prove the long-running collector is alive.
- After starting a per-user worker service, run `bk doctor --require-worker --json --strict`;
  only the current UID and exact data-directory instance may satisfy it.
