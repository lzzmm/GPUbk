# BK Agent Protocol

## JSON CLI

All machine responses use `schema_version: "bk.agent.v1"`.

```bash
bk agent context --compact
bk agent recommend COUNT DURATION [--mode s|x] [--start ISO] [--gpu 0,1] [--mem 12g] --compact
bk COUNT DURATION [--mem 12g] --op-id ID --json
bk l --json
bk j --json
```

Omitting `--start` permits earliest-slot queueing. Providing `--start` means exact placement.

Recommendation fields:

- `available`: whether the requested semantics have a legal slot.
- `recommendation.gpus`, `start_at`, `end_at`, `queued`, `confidence`.
- `gpu_details`: live status, predicted recent load, reservation pressure, physical free VRAM, and projected reservation headroom.
- `nearest_available`: suggestion only when an exact request is unavailable.
- `warnings`: incomplete history, live-busy device, memory assumption, or allocator fallback.

## MCP Tools

- `get_gpu_context`: privacy-safe policy, telemetry, forecast, and reservations.
- `recommend_gpu_booking`: read-only recommendation.
- `create_gpu_booking`: idempotent write; `operation_id` is required.
- `list_gpu_reservations`: active global or current-UID reservations.
- `cancel_my_gpu_booking`: current UID only.
- `read_my_job_log`: bounded current-UID private log tail.

The MCP server runs over local stdio and inherits the launching user's UID. It never accepts UID as a tool argument.

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
