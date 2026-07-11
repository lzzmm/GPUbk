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

Otherwise use the JSON CLI:

```bash
bk agent context --compact
bk agent recommend 2 1h30m --mode shared --mem 12g --compact
bk 2 1h30m --mem 12g --op-id <stable-id> --json
bk agent edit <short-id> --duration 2h --op-id <stable-edit-id> --compact
bk agent cancel <short-id> --compact
```

Read [references/protocol.md](references/protocol.md) when implementing an integration or interpreting every field.

## Plan A Reservation

1. Determine GPU count, duration, shared/exclusive mode, earliest or exact start, and expected VRAM per GPU.
2. For shared work, ask for expected VRAM when it materially affects placement. If unknown, state that GPUbk will use its conservative equal-share estimate.
3. Inspect context immediately before recommending. Current processes can change quickly.
4. Run a read-only recommendation. Explain queued start, selected GPUs, confidence, live-busy warnings, and projected memory headroom.
5. Treat explicit start as exact. Do not silently convert it to queueing.
6. Let GPUbk enforce conflicts and memory limits. Never infer that an unsafe placement is acceptable.

Use shared mode for workloads that can coexist within both record and VRAM limits. Use exclusive mode when the experiment needs the whole device, has unpredictable memory behavior, or must avoid interference.

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

For edits, an explicit `start` is exact and does not move unless `allow_queue=true` was explicitly requested. Keep `bk worker` running for scheduled commands. Use `list_gpu_reservations`, `bk j --json`, or the bounded job-log tool to inspect state.

## Respect Safety Boundaries

- External AI allocator output is advisory ordering only. Local time, capacity, VRAM, identity, and transaction checks remain authoritative.
- Treat live utilization as a soft forecast because running processes have no reliable end time.
- Do not delete journal or lock files manually.
- Do not enable a worker, monitor, or service on a shared server without the user's or administrator's approval.
