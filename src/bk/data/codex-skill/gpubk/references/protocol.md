# GPUbk Agent Protocol

## JSON CLI

All machine responses use `schema_version: "bk.agent.v1"`.

```bash
bk agent context --compact
bk agent recommend COUNT DURATION [--mode s|x] [--start ISO] [--gpu 0,1] [--mem 12g] --compact
bk COUNT DURATION [--mem 12g] --op-id ID --json
bk agent edit RESERVATION --duration 2h --op-id ID --compact
bk agent cancel RESERVATION --compact
bk l --json
bk j --json
```

Omitting `--start` permits earliest-slot queueing. Providing `--start` means exact placement.
The ledger binds its scheduling and storage policy on first write. Agents must surface policy-mismatch errors instead of retrying with altered local limits.

Recommendation fields:

- `available`: whether the requested semantics have a legal slot.
- `recommendation.gpus`, `start_at`, `end_at`, `queued`, `confidence`.
- Context GPU entries include model name, temperature, live status, physical VRAM, and recent load history.
- `gpu_details`: live status, predicted recent load, reservation pressure, physical free VRAM, and projected reservation headroom.
- `nearest_available`: suggestion only when an exact request is unavailable.
- `warnings`: incomplete history, live-busy device, memory assumption, or allocator fallback.

Create and edit return the same `kind=booking_result` shape through JSON CLI and MCP: `status`, a privacy-safe `reservation`, per-GPU `allocation.selected` explanations, allocator source/reason, and warnings. Status is `created`, `updated`, `queued`, or retry-safe `exists`.

## MCP Tools

- `get_gpu_context`: privacy-safe policy, telemetry, forecast, and reservations.
- `recommend_gpu_booking`: read-only recommendation.
- `create_gpu_booking`: idempotent write; `operation_id` is required.
- `list_gpu_reservations`: active global or current-UID reservations.
- `edit_my_gpu_booking`: idempotent current-UID edit; `operation_id` is required.
- `cancel_my_gpu_booking`: current UID only.
- `read_my_job_log`: bounded current-UID private log tail.

The MCP server runs over local stdio and inherits the launching user's UID. It never accepts UID as a tool argument.
Tools expose standard MCP annotations: context, recommendation, listing, and log reads are read-only; create and edit are idempotent writes because they require operation IDs; cancel is destructive; all tools are closed-world local operations.

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
