# GPUbk Agent Protocol

## JSON CLI

All machine responses use `schema_version: "bk.agent.v1"`.

```bash
bk agent context --compact
bk agent recommend COUNT DURATION [--mode s|x] [--start ISO] [--gpu 0,1] [--mem 12g] [--share 1/2] --compact
bk COUNT DURATION [--mem 12g] [--share 1/2] --op-id ID --json
bk agent edit RESERVATION --duration 2h [--share 1/2] --op-id ID --compact
bk agent cancel RESERVATION --compact
bk l --json
bk j --json
bk j --cleanup --json
bk usage me --since 24h --json --compact
bk usage samples --since 2d --resolution 5m --json --compact
```

Omitting `--start` uses the active 5-minute interval when possible, then permits earliest-slot queueing. Providing `--start` means exact placement. Human CLI users may use `--at`; Agents should keep using explicit ISO 8601 and structured fields.
The ledger binds its scheduling and storage policy on first write. Agents must surface policy-mismatch errors instead of retrying with altered local limits.

Recommendation fields:

- `available`: whether the requested semantics have a legal slot.
- `recommendation.gpus`, `start_at`, `end_at`, `queued`, `confidence`.
- Context GPU entries include model name, temperature, live status, physical VRAM, and recent load history.
- `gpu_details`: live status, predicted recent load, reservation pressure, physical free VRAM, and projected reservation headroom.
- `nearest_available`: suggestion only when an exact request is unavailable.
- `share_units_per_gpu` and `share_fraction_per_gpu`: admission capacity requested on each GPU. Context policy supplies `shared_capacity_units_per_gpu`. Missing fields on legacy reservations mean one unit.
- `warnings`: incomplete history, live-busy device, memory assumption, or allocator fallback.
- Scheduled job objects may include `launch_guard_state=waiting`, `waiting_since`, and a
  privacy-safe `message`; exit status `3` from `bk worker --once` means due work is waiting for
  a safe live GPU state, not that the command ran.

Create and edit return the same `kind=booking_result` shape through JSON CLI and MCP: `status`, a
privacy-safe `reservation`, per-GPU `allocation.selected` explanations, allocator source/reason,
and warnings. Status is `created`, `updated`, `queued`, or retry-safe `exists`.

Cancellation returns `kind=cancellation_result`, the cancelled reservation, and
`private_job_cleanup`. A non-null cleanup warning means cancellation committed but the owning UID
could not remove one or more private command specs; do not repeat the destructive cancellation.
`bk j --cleanup --json` is the retry-safe cleanup operation. It retains runnable/retryable specs,
applies a 24-hour grace period to unreferenced files, and never removes private job logs.
`private_job_cleanup` reports `removed`, `retained`, `deferred_orphans`, `failed`, and `warnings`.

`share` accepts whole units, an exactly representable fraction, or a percentage. `share_with=N` reserves all but `N` minimum units. These values control scheduling admission and inferred VRAM, not hardware-enforced compute bandwidth. Explicit `expected_memory` remains the actual per-GPU estimate and is not multiplied by share units.

## MCP Tools

- `get_gpu_context`: privacy-safe policy, telemetry, forecast, and reservations.
- `recommend_gpu_booking`: read-only recommendation.
- `create_gpu_booking`: idempotent write; `operation_id` is required.
- `list_gpu_reservations`: active global or current-UID reservations.
- `edit_my_gpu_booking`: idempotent current-UID edit; `operation_id` is required.
- `cancel_my_gpu_booking`: current UID only.
- `cleanup_my_job_specs`: idempotently prune only this UID's non-runnable private command specs.
- `read_my_job_log`: bounded current-UID private log tail.
- `get_my_gpu_usage`: versioned current-UID summaries, samples, and optional audit events.

The MCP server runs over local stdio and inherits the launching user's UID. It never accepts UID as a tool argument.
Historical usage also has a read-only `bk://usage/me/recent` resource. External visualizers should consume
`gpubk.usage.v1` through `bk.usage_api.UsageQueryService` rather than parse compact storage partitions.
Tools expose standard MCP annotations: context, recommendation, listing, and log reads are
read-only; create and edit are idempotent writes because they require operation IDs; cancel is
destructive and non-idempotent; private-spec cleanup is destructive but idempotent; all tools are
closed-world local operations.

An operation ID identifies one immutable write intent for the current UID. Exact retries return `status=exists`; reusing that ID with another reservation or different fields returns a structured error. Edit start times remain exact unless `allow_queue=true` is explicitly supplied.
Each retained reservation keeps at most 256 idempotent edit intents so malformed automation cannot grow one hot record without bound. Recreate an unusually long-lived reservation before exceeding that limit.

## External Allocator

Input uses `schema_version: "bk.allocator.v1"` and includes a privacy-safe request, policy, built-in scores, per-GPU telemetry/history, and active reservation windows.

Return exactly one JSON object:

```json
{
  "schema_version": "bk.allocator.v1",
  "gpu_order": [0, 1, 2, 3],
  "reason": "short privacy-safe rationale"
}
```

`gpu_order` must be a complete permutation. It is blended into local scores and cannot bypass deterministic placement validation.
