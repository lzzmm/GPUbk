# GPUbk Agent Protocol

## JSON CLI

Booking and allocation responses use `schema_version: "bk.agent.v1"`. The personal
audit-tail response uses `schema_version: "gpubk.audit.v1"`; worker liveness uses
`schema_version: "gpubk.worker.v1"`.

```bash
bk agent context --compact
bk agent recommend COUNT DURATION [--mode s|x] [--start ISO] [--gpu 0,1] [--mem 12g] [--share 1/2] --compact
bk COUNT DURATION [--mem 12g] [--share 1/2] --op-id ID --json
bk agent edit RESERVATION --duration 2h [--share 1/2] --op-id ID --compact
bk agent cancel RESERVATION --compact
bk l --json
bk j --json
bk j --cleanup --json
bk worker --status --json
bk log --limit 100 --json
bk usage me --since 24h --json --compact
bk usage samples --since 2d --resolution 5m --json --compact
```

Omitting `--start` uses the active configured booking interval when possible, then permits earliest-slot queueing. Providing `--start` means exact placement. Read `policy.granularity_minutes` from context instead of assuming five minutes. Human CLI users may use `--at`; Agents should keep using explicit ISO 8601 and structured fields.
The ledger binds its scheduling and storage policy on first write. Agents must surface policy-mismatch errors instead of retrying with altered local limits.

Recommendation fields:

- `available`: whether the requested semantics have a legal slot.
- `recommendation.gpus`, `start_at`, `end_at`, `queued`, `confidence`.
- Context GPU entries include model name, temperature, live status, physical VRAM, recent load
  history, and additive `capabilities.process_telemetry` /
  `capabilities.process_utilization` booleans. Treat missing process capability as unknown state,
  never as proof that a GPU has no processes.
- Context `policy.monitoring` reports the effective sample and rollup cadence; consumers must
  not infer finer telemetry precision. `writer_uid` identifies the configured telemetry role,
  not the Agent caller.
- Context `policy.monitoring.collector` and every `gpubk.usage.v1` response report collector
  freshness. Treat live values as current only when `fresh=true`. `degraded` identifies explicit
  device/process capability gaps; `stale`, `stopped`, `not-seen`, `clock-skew`, `invalid`, and
  `incompatible` are never evidence that a GPU has no processes. `topology-mismatch` means the
  fresh monitor covers a different GPU count than the active policy and is also not current.
- `gpu_details`: live status, predicted recent load, reservation pressure, physical free VRAM, and projected reservation headroom.
- `nearest_available`: suggestion only when an exact request is unavailable.
- `share_units_per_gpu` and `share_fraction_per_gpu`: admission capacity requested on each GPU. Context policy supplies `shared_capacity_units_per_gpu`. Missing fields on legacy reservations mean one unit.
- `warnings`: incomplete history, live-busy device, memory assumption, or allocator fallback.
- Scheduled job objects may include `launch_guard_state=waiting`, `waiting_since`, and a
  privacy-safe `message`; exit status `3` from `bk worker --once` means due work is waiting for
  a safe live GPU state, not that the command ran.
- Scheduled job objects may include `recovery_state` and `recovered_at`. `terminated` means a
  same-UID process group was stopped after worker loss, but job status remains `uncertain` because
  earlier side effects cannot be disproved. `remote-unverified`, `unverified`, and
  `termination-unverified` must never trigger automatic retry.
- Context capabilities advertise `single_worker_lease`, `scheduled_job_crash_recovery`, and
  `worker_liveness` plus `collector_liveness`.
  Worker exit `75` means the UID-private lease is already held; do not retry in a tight loop.
- Context `worker` and `bk jobs --json` embed `gpubk.worker.v1`. Only `state=running` with
  `running=true` is a positive liveness result, based on the UID-private kernel lock. `lease`
  metadata is diagnostic and may be absent or stale. `bk worker --status --require-running`
  returns exit 2 for every non-running state without starting a worker or writing storage.

Create and edit return the same `kind=booking_result` shape through JSON CLI and MCP: `status`, a
privacy-safe `reservation`, per-GPU `allocation.selected` explanations, allocator source/reason,
`worker`, and warnings. `worker` is the current `gpubk.worker.v1` result for a scheduled command
and `null` when the reservation has no command. Only `worker.running=true` proves that unattended
launch is currently available. Status is `created`, `updated`, `queued`, or retry-safe `exists`.

Cancellation returns `kind=cancellation_result`, the cancelled reservation, and
`private_job_cleanup`. A non-null cleanup warning means cancellation committed but the owning UID
could not remove one or more private command specs; do not repeat the destructive cancellation.
`bk j --cleanup --json` is the retry-safe cleanup operation. It retains runnable/retryable specs,
applies a 24-hour grace period to unreferenced specs, and applies the configured age/quota policy
only to terminal private job logs.
`private_job_cleanup` reports `removed`, `retained`, `deferred_orphans`, `failed`, and `warnings`.
`private_job_log_cleanup` additionally reports retained/removed bytes and unresolved quota excess.

`bk log --json` returns `kind=operation-log`, the process `uid`, the requested `limit`, recent
matching `events` in chronological order, and a nullable `warning`. It reads backward with bounded
memory, scans at most 64 MiB, skips malformed records, and never accepts a UID argument.

`share` accepts whole units, an exactly representable fraction, or a percentage. `share_with=N` reserves all but `N` minimum units. These values control scheduling admission and inferred VRAM, not hardware-enforced compute bandwidth. Explicit `expected_memory` remains the actual per-GPU estimate and is not multiplied by share units.

## MCP Tools

- `get_gpu_context`: privacy-safe policy, telemetry, forecast, and reservations.
- `recommend_gpu_booking`: read-only recommendation.
- `create_gpu_booking`: idempotent write; `operation_id` is required.
- `list_gpu_reservations`: active global or current-UID reservations.
- `edit_my_gpu_booking`: idempotent current-UID edit; `operation_id` is required.
- `cancel_my_gpu_booking`: current UID only.
- `cleanup_my_job_specs`: idempotently prune only this UID's non-runnable private command specs.
- `cleanup_my_job_logs`: idempotently apply this UID's terminal log retention and quota policy.
- `read_my_job_log`: bounded current-UID private log tail.
- `get_my_gpu_usage`: versioned current-UID summaries, samples, and optional audit events.

The MCP server runs over local stdio and inherits the launching user's UID. It never accepts UID as a tool argument.
Historical usage also has a read-only `bk://usage/me/recent` resource. External visualizers should consume
`gpubk.usage.v1` through `bk.usage_api.UsageQueryService` rather than parse compact storage partitions.
Tools expose standard MCP annotations: context, recommendation, listing, and log reads are
read-only; create and edit are idempotent writes because they require operation IDs; cancel is
destructive and non-idempotent; private-spec cleanup is destructive but idempotent; all tools are
closed-world local operations.

An operation ID identifies one immutable write intent for the current UID. Exact retries return `status=exists`; reusing that ID with another reservation or different fields returns a structured error. Started reservations and explicit edit starts in the past are rejected. A valid edit start remains exact unless `allow_queue=true` is explicitly supplied to resolve a resource conflict.
Each retained reservation keeps at most 256 idempotent edit intents so malformed automation cannot grow one hot record without bound. Recreate an unusually long-lived reservation before exceeding that limit.

## External Allocator

Input uses `schema_version: "bk.allocator.v1"` and includes a privacy-safe request, policy (including `granularity_minutes`), built-in scores, per-GPU telemetry/history, and active reservation windows.

Return exactly one JSON object:

```json
{
  "schema_version": "bk.allocator.v1",
  "gpu_order": [0, 1, 2, 3],
  "reason": "short privacy-safe rationale"
}
```

`gpu_order` must be a complete permutation. It is blended into local scores and cannot bypass deterministic placement validation.
