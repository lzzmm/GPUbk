# GPUbk Telemetry

GPUbk keeps collection, storage, and presentation separate:

- A collector produces process events and per-user minute records.
- A `TelemetrySink` validates and stores those records.
- `UsageQueryService` returns a stable public model.
- The CLI, TUI, MCP server, and external visualizers consume the query model.

Code outside GPUbk should not parse files under `usage/`. Their short field names,
partitioning, and compression are storage details and may change behind compatible
readers.

## Public Python API

```python
from datetime import timedelta

from bk.telemetry import open_usage_query
from bk.timeparse import utc_now

api = open_usage_query()
end = utc_now()
payload = api.users(start=end - timedelta(days=7), end=end)
```

The public payload uses `schema_version: "gpubk.usage.v1"`. Missing fields mean
the collector did not provide that metric. `null` means it attempted collection
but the value was unavailable. A numeric zero is a measured zero.

A separate trusted collector can implement `bk.telemetry.TelemetrySink`, or use
`open_usage_store()` as the reference sink. Exactly one writer must hold
`store.lock()` for its lifetime. Readers do not take that lock.

## Query Interfaces

```bash
bk u                              # current UID, last 24 hours
bk u users --since 30d           # all visible UIDs
bk u samples --since 2d --resolution 5m --json
bk u events --user me --since 7d --json
bk u capabilities --json
bk u storage --json
```

MCP exposes `get_my_gpu_usage` and `bk://usage/me/recent`. Both are bound to the
MCP process UID and cannot request another UID.

There is deliberately no unauthenticated HTTP listener. A dashboard can call the
Python API locally, consume the JSON CLI, or place its own authenticated service
in front of `UsageQueryService`.

## Workload Model

Workloads have independent launcher, entrypoint, purpose, framework, execution,
source, confidence, and safe-label fields. `unknown` is a valid result. GPUbk does
not pretend that every `main.py` is training.

Raw arguments, environment variables, stdout, secrets, and absolute paths are not
stored. A per-install HMAC gives the same UID and entrypoint a stable numeric
`workload_id` without exposing its original path. Managed jobs can provide a
higher-confidence safe summary.

## Storage And Retention

The versioned store is rooted at `BK_DATA_DIR/usage/`:

```text
usage/
  store.json
  state.json
  load.json
  users.json
  workloads.v1.jsonl
  events/YYYY/MM/YYYY-MM-DD.v1.jsonl[.gz]
  minute/YYYY/MM/YYYY-MM-DD.v1.jsonl[.gz]
  five-minute/YYYY/MM/YYYY-MM-DD.v1.jsonl.gz
  ten-minute/YYYY/MM/YYYY-MM-DD.v1.jsonl.gz
  hourly/YYYY/MM/YYYY-MM-DD.v1.jsonl.gz
  daily/YYYY/MM/YYYY-MM-DD.v1.jsonl.gz
```

Defaults are 120 minutes of scheduling load, 30 days of minute records, 365 days
of 5-minute records, 1095 days of 10-minute records, 1500 days of hourly records,
unlimited daily summaries, and 365 days of process events. Only user activity or
reserved-user rows are retained; empty GPUs and system display processes are not
written to long-term user history.

Closed partitions use deterministic gzip plus record-count and SHA-256 metadata.
Maintenance creates all coarser levels before removing a finer partition. Unknown
future fields stop compaction and deletion instead of being silently discarded.
Open partitions are append-only. Each batch is validated before writing and is
truncated back to its original size if a detected write or fsync fails. After an
unclean stop, the next writer preserves a complete final JSON record missing only
its newline, or discards only the malformed trailing fragment before appending.
Per-record and per-file safety limits prevent the writer from creating data that
the bounded reader would later refuse.

```bash
bk u maintain             # dry run
bk u maintain --yes       # apply compaction and retention
bk u migrate              # inspect legacy usage-*.jsonl migration
bk u migrate --yes        # copy legacy data; originals remain untouched
```

## Compatibility Rules

- Field meanings and units never change in place.
- Additive fields use a schema minor revision.
- Type, unit, or semantic changes require a new major record schema.
- Unknown enum numbers remain visible as `unknown(NUMBER)`.
- New readers support legacy files and mixed partition versions.
- A writer encountering a newer store major version refuses to write.
- Migration is copy-on-write and retry-safe; legacy files are retained.
- Agents and visualizers use the public API version, not storage versions.

The legacy `usage-events.jsonl`, `usage-rollups.jsonl`, `usage-state.json`, and
`usage-load.json` files remain readable. They are not automatically deleted.

## Attribution Limits

Per-process SM utilization and process GPU memory can be attributed to a UID.
Whole-device utilization cannot be divided accurately among simultaneous shared
users, so the user API does not manufacture an equal split. CUDA MPS, restricted
`/proc`, and some containers can also prevent exact process attribution; such
records remain explicit `unknown` or `unattributed` data rather than guessed data.
