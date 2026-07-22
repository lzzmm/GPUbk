# GPUBK Telemetry

GPUBK keeps collection, storage, and presentation separate:

- A collector produces process events and per-user minute records.
- A `TelemetrySink` validates and stores those records.
- `UsageQueryService` returns a stable public model.
- The CLI, TUI, MCP server, and external visualizers consume the query model.

Code outside GPUBK should not parse files under `usage/`. Their short field names,
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

`bk.telemetry.summarize_public_rollups()` can rebuild the same per-user metrics from
already exported public sample records without reopening GPUBK's internal store. This
is the supported path for an external archive browser or visualization service.

The public payload uses `schema_version: "gpubk.usage.v1"`. Missing fields mean
the collector did not provide that metric. `null` means it attempted collection
but the value was unavailable. A numeric zero is a measured zero.

A separate trusted collector can implement `bk.telemetry.TelemetrySink`, or use
`open_usage_store()` as the reference sink. Exactly one writer must hold
`store.lock()` for its lifetime. Readers do not take that lock.
Collectors may also implement `bk.telemetry.CollectorStatusSink`. The reference
store writes a versioned `gpubk.collector.v1` health document by atomic replace.
Every `UsageQueryService` response includes its classified `collector` view, so
clients never need to read the status file directly.
Sample and user-summary responses also include an additive `coverage` object.
`first_sample_at` and `last_sample_at` describe the span containing returned
records, while `record_count` reports how many stored records contributed.
`continuous` is currently always false: an empty or sparse interval remains
unknown and must never be interpreted as measured zero.
For an empty UID-filtered user summary, `store_has_samples` reports only whether
the interval contains any stored samples, without exposing their owners, and
`matching_process_event` reports whether that UID has an audit event awaiting or
missing a finalized summary. These additive diagnostics let clients distinguish
an idle account from UID/container attribution drift.
For group-writable deployments, the bundled writer additionally requires a
root-owned explicit configuration and matching numeric `monitor_uid`.
Applied `bk u maintain --yes` and `bk u migrate --yes` operations require that
same UID; their dry-run forms remain read-only.

The bundled monitor samples every 2 seconds and emits 60-second rollups by
default. `monitor_interval_seconds` and `monitor_rollup_seconds` are versioned
configuration fields with `BK_MONITOR_INTERVAL_SECONDS` and
`BK_MONITOR_ROLLUP_SECONDS` overrides. A rollup must be at least one sample long
and an exact multiple of the sampling interval, so accumulated observed time
cannot exceed its storage window.

NVML device, stable-device-identifier, process-list, and per-process-utilization
capabilities are tracked separately. A transient NVML failure closes stale handles
and retries after a short backoff. Device metrics may temporarily come from
`nvidia-smi`, but that fallback is not treated as an empty process list: the
collector preserves the last observed process state and emits a deduplicated warning until process
telemetry returns. This prevents telemetry gaps from becoming false process-stop
and process-start audit events. Per-user SM values remain missing when only the
process list is available.

The bundled collector writes its first heartbeat after a complete sample, then
at a bounded low frequency; capability changes are published immediately. A
graceful exit records `stopped`. An unclean exit leaves the last document in
place and readers classify it as `stale` after the greater of 30 seconds or
three heartbeat intervals. The crash path attempts to flush partial rollups,
but a flush failure never replaces the original collector error. `degraded`
means collection is alive but at least one
configured GPU lacks device telemetry, a stable CUDA-compatible identifier, a
process list, numeric UID attribution for an observed process, or per-process
utilization telemetry. A legacy v1 heartbeat without the additive stable-ID or
process-identity capability remains readable but is classified as degraded until
a current monitor replaces it. `process_identity_gap` is empty on an idle GPU;
it becomes populated only when process telemetry is unavailable or a currently
observed process cannot be attributed.
`clock-skew`, `invalid`, and `incompatible` remain explicit rather than being
treated as fresh data. A fresh heartbeat covering a different number of GPUs
than the active policy is `topology-mismatch` and is also not current. These
states are health evidence only, never an authorization or scheduling lock.
Ledger-policy drift is handled more strictly than a sampling crash: validation
runs before maintenance and sampling, buffered rollups are discarded without a
flush, and the monitor exits `78` after releasing its writer lock.

## Query Interfaces

```bash
bk u                              # current UID, last 24 hours
bk u users --since 30d           # all visible UIDs
bk u samples --since 2d --resolution 5m --json
bk u events --user me --since 7d --json
bk u capabilities --json
bk u storage --json
```

The capabilities response exposes `writer_policy` so external visualizers and
administration tools can discover the configured writer UID without parsing
the private configuration file.
It also exposes `topology`: the current stable node identity, the
`gpubk.node` record extension, the local API's single-node scheduling boundary,
and availability of the optional federated cluster client. Consumers must not
infer any capability from a shared filesystem path.

MCP exposes `get_my_gpu_usage` and `bk://usage/me/recent`. Both are bound to the
MCP process UID and cannot request another UID.

There is deliberately no unauthenticated HTTP listener. A dashboard can call the
Python API locally, consume the JSON CLI, or place its own authenticated service
in front of `UsageQueryService`.

## Workload Model

Workloads have independent launcher, entrypoint, purpose, framework, execution,
source, confidence, and safe-label fields. `unknown` is a valid result. GPUBK does
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
  collector.json
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
File and containing-directory fsync errors are propagated; a monitor must not report
an append as durable when the filesystem could not persist its directory entry.
Per-record and per-file safety limits prevent the writer from creating data that
the bounded reader would later refuse.

New event and rollup records carry a `gpubk.node` extension. Its `id` is a
truncated SHA-256 derived from the local machine ID (or sanitized hostname as a
fallback), so the raw machine ID is never persisted. The extension also keeps a
display hostname and the stable GPU UUID when the collector has one. Rollup IDs,
deduplication keys, and aggregation keys include this node identity. Legacy
records without the extension are assigned to the synthetic node `legacy` and
remain readable without an in-place migration.

This is an export and migration boundary, not a cluster writer protocol. Exactly
one host owns each ledger and telemetry writer. Independent brokers or monitors
must not share one NFS-backed data directory. Optional federation queries the
public JSON API on each owning host, while a root-owned catalog maps
`(node_id, uid)` to a global principal; it never merges users by mutable username.
Optional NFS export uses disjoint immutable node namespaces and the same public API.
`bk admin cluster export-history` publishes compressed daily user summaries and samples;
`bk admin cluster verify-history` validates their bounded manifests, file modes, sizes,
record counts, and SHA-256 digests. `bk c history` reads those payloads without importing
them into this store. The archive is therefore a portable read model, not another
collector, broker, or telemetry writer. See [CLUSTER.md](CLUSTER.md) for permissions and commands.

Chronological queries stream open partitions and stop as soon as their record
limit is satisfied. A closed gzip partition is scanned against its SHA-256 and
record-count metadata, then rewound and parsed through the same open file
descriptor, so an atomic path replacement cannot swap in unverified data between
those steps. Reverse-order views may still buffer one daily partition.

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
- Consumers check `collector.fresh` before treating recent telemetry as current.
- Unknown additive collector fields are accepted; incompatible schema versions
  remain visible and are never rewritten by readers.
- Node identity is additive metadata; old node-less records remain queryable as
  `legacy`, and imports must preserve `gpubk.node` rather than renumber GPUs.

The legacy `usage-events.jsonl`, `usage-rollups.jsonl`, `usage-state.json`, and
`usage-load.json` files remain readable. They are not automatically deleted.

## Attribution Limits

Per-process SM utilization and process GPU memory can be attributed to a UID.
Whole-device utilization cannot be divided accurately among simultaneous shared
users, so the user API does not manufacture an equal split. CUDA MPS, restricted
`/proc`, and some containers can also prevent exact process attribution; such
records remain explicit `unknown` or `unattributed` data rather than guessed data.
For a rootful Docker process, GPUBK reads the host cgroup to retain the runtime
and container ID. If exactly one UID with access to the Docker socket has an
active reservation on that GPU, the process is attributed to that UID with
`identity_source=container-reservation`; human views append `*` to the owner.
If more than one eligible UID is present, the process remains
`container-ambiguous` and `unknown`. A root container with no eligible
reservation remains `unreserved`. The original host UID is retained in every
case, and container inference is never presented as kernel-proven ownership.
Rootless containers normally retain their mapped host UID and need no inference.
The collector publishes those active gaps through `process_identity_gap`, and a
strict post-start doctor check rejects the degraded heartbeat.
If command-line access is restricted but `/proc/<pid>` ownership is visible,
GPUBK retains the numeric UID with an empty command label instead of discarding
the known owner. Process command reads are bounded to 4096 bytes.
