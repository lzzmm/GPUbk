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
```

Read [references/protocol.md](references/protocol.md) when implementing an integration or interpreting every field.

## Plan A Reservation

1. Determine GPU count, duration, shared/exclusive mode, earliest or exact start, expected VRAM per GPU, and any requested shared capacity.
2. For shared work, ask for expected VRAM and share intent when they materially affect placement. `share=3/4` reserves three capacity units on a four-unit server; `share_with=1` is the same asymmetric reservation. Two equal users should each request `1/2`.
3. If expected VRAM is unknown, state that GPUbk derives it from the requested share. Share units constrain admission; they do not physically enforce GPU compute bandwidth without MIG/MPS.
4. Inspect context immediately before recommending. Current processes can change quickly.
5. Run a read-only recommendation. Explain queued start, selected GPUs, confidence, live-busy warnings, and projected memory headroom.
6. Treat explicit start as exact. Do not silently convert it to queueing.
7. Let GPUbk enforce conflicts and memory limits. Never infer that an unsafe placement is acceptable.

Use shared mode for workloads that can coexist within both capacity-unit and VRAM limits. Use exclusive mode when the experiment needs the whole device, has unpredictable memory behavior, or must avoid interference.

## Create Safely

- Generate one stable operation ID for each create or edit intent and reuse it only for exact retries.
- Never reuse an operation ID for changed fields; GPUbk rejects mismatched reuse instead of silently applying it.
- Never pass, invent, or override a UID. GPUbk derives identity from the local process.
- Do not retry a write with a new operation ID after an ambiguous response; inspect reservations first.
- Do not cancel or edit another user's reservation.
- Do not expose secrets in command arguments. GPUbk stores commands privately, but process environments or user scripts are preferable for credentials.

To schedule a command:

```bash
bk 2 1h30m --mem 12g --op-id <stable-id> -- python train.py --config exp.yaml
```

GPUbk sets `CUDA_VISIBLE_DEVICES`; do not add physical GPU IDs to the training command.

## Handle Results

- `created`: reservation starts at the returned time.
- `updated`: the approved edit was applied.
- `queued`: reservation was moved to the earliest legal future slot because start was implicit.
- `exists`: the operation ID was already applied; treat this as idempotent success.
- JSON exit `2`: invalid request or write conflict. Inspect `error.message`.
- Recommendation exit `3`: no legal exact slot; present `nearest_available` without booking it.
- `uncertain` job: it may already be running. Check processes and logs before using duplicate-risk retry.
- `pending` with `launch_guard_state=waiting`: the reservation is active, but a live process or VRAM check blocked launch. Report `message`; do not bypass the guard unless the user explicitly accepts that collision risk.
- Cancellation results include `private_job_cleanup`; cancellation remains committed when cleanup
  reports a warning. Surface that warning instead of retrying the destructive operation.

For edits, an explicit `start` is exact and does not move unless `allow_queue=true` was explicitly
requested. Keep `bk worker` running for scheduled commands. Use `list_gpu_reservations`,
`bk j --json`, or the bounded job-log tool to inspect state. `cleanup_my_job_specs` and
`bk j --cleanup --json` prune only terminal/expired private command specs; they deliberately retain
job logs and runnable or retryable specs.

## Respect Safety Boundaries

- External AI allocator output is advisory ordering only. Local time, capacity, VRAM, identity, and transaction checks remain authoritative.
- Treat live utilization as a soft forecast because running processes have no reliable end time.
- Do not delete journal or lock files manually.
- Do not enable a worker, monitor, or service on a shared server without the user's or administrator's approval.
- Do not disable `worker_live_guard` merely to make a scheduled command start sooner.
- Before an approved service deployment, run `bk doctor --probe --json --strict`; do not treat a simulation or single-host NFS lock check as proof of the complete production boundary.
