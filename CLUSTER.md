# Cluster mode design

GPUBK cluster mode is a federation of independently safe single-host schedulers.
Each GPU host keeps one local broker, ledger, monitor, and node identity. A client
queries those hosts in parallel and submits a reservation to exactly one selected
host. The selected host remains the only authority for its GPUs.

This keeps single-host behavior unchanged and avoids treating NFS locks as a
distributed consensus protocol.

## Deployment shape

Single-host GPUBK uses a local Unix socket between each user's CLI and the
service-owned broker. Cluster mode adds only a client-side catalog and outbound
SSH calls; it does not add a central scheduler or a listening network service.
Every GPU host can therefore continue operating locally when the cluster client,
another node, or NFS is unavailable.

The supported first deployment is:

1. Install and validate ordinary GPUBK independently on every GPU host.
2. Configure key-based, host-key-verified SSH for the users who need federation.
3. Create one root-owned catalog on each machine where users run cluster commands.
4. Map node-local numeric UIDs to a global principal only when aggregate reporting
   is desired.

The help is available before a catalog exists. On a GPU host, include that host and
then add the remote node:

```bash
bk cluster -h
sudo bk admin cluster init gpu-a --yes
bk c probe gpu-b gpu-b
# Review and run the printed sudo bk admin cluster add ... command.
sudo bk admin cluster status
bk cluster
bk cluster check
bk cluster check --jobs
bk c rec 1 30m
```

On a login node, workstation, or other client that does not schedule local GPUs, skip
`cluster init`. Probe the first GPU host and run the reviewed command it prints:

```bash
bk c probe gpu-a gpu-a
sudo bk admin cluster add gpu-a gpu-a STABLE_NODE_ID --yes
bk c probe gpu-b gpu-b
sudo bk admin cluster add gpu-b gpu-b STABLE_NODE_ID --yes
bk c check
```

The first `cluster add` atomically creates a remote-only catalog when none exists; it
never overwrites a catalog that appears concurrently. Run the catalog commands on each
machine from which users need a cluster view. A local entry is created from that host's
stable identity only when `cluster init` is used. `bk c probe` validates the remote
Agent context and prints the stable `node.id` in an exact add command. It is read-only,
runs as the ordinary caller, and keeps strict SSH host-key checking enabled. The catalog
contains no credentials.
Each ordinary user still needs non-interactive SSH access with a previously verified
host key. `bk cluster status` reports one unreachable account without disabling other
healthy nodes.

A shared root-owned catalog must use a username-free host or SSH alias such as `gpu-b`,
never `alice@gpu-b`. Otherwise every caller would be forced to act as Alice's remote
numeric UID. When account names differ between nodes, each user maps the common alias to
their own remote account in `~/.ssh/config`. A custom user-owned catalog selected through
`BK_CLUSTER_CONFIG` may use `user@host`, but it must not be shared. Legacy root catalogs
with pinned users fail closed for ordinary commands; repair them with `sudo bk admin
cluster status` followed by `sudo bk admin cluster set NODE --target HOST --yes`.

Before booking, every ordinary user can validate their own route with `bk cluster
check`. It checks endpoint reachability, stable identity, actor attribution, clock
skew, schedulable GPUs, and the capabilities needed for retry-safe book/edit/cancel
operations. Telemetry degradation is reported as a warning because the scheduler can
still operate conservatively.

Use `bk cluster check --jobs` when scheduled commands are part of the deployment.
It additionally requires the scheduled-job capabilities and that the current SSH
identity's worker holds its lease on every enabled node. The ordinary check remains
valid for reservation-only users and warns if that user already has a pending command
on a node whose worker is stopped or unseen.

For maintenance, disable a node instead of deleting it:

```bash
sudo bk admin cluster disable gpu-b --yes
sudo bk admin cluster enable gpu-b --yes
```

Update a changed SSH alias, candidate path, timeout, or tie-break priority in place.
This keeps the stable node identity, global-principal mappings, and archived history:

```bash
bk c probe gpu-b new-gpu-b
# Confirm the probe reports the same stable node ID, then update the endpoint.
sudo bk admin cluster set gpu-b --target new-gpu-b --timeout 12 --yes
sudo bk admin cluster set gpu-b --priority 10 --yes
bk c check
```

A disabled node is not contacted and cannot receive new cluster writes. Its endpoint,
stable identity, principal mappings, and archived history remain in the version-1
catalog. Old catalogs have no `enabled` field and therefore continue to treat every
node as enabled.

To stop using federation on one client, explicitly delete its routing catalog:

```bash
sudo bk admin cluster delete --yes
```

This removes only that client's catalog. It does not connect to GPU hosts or change
reservations, ledgers, workers, usage history, the optional archive, or SSH settings.
The parent configuration directory is retained because it may predate cluster setup or
contain the independently managed single-host configuration.

## User model

- With no cluster catalog, GPUBK stays in single-host mode. Node selectors, node
  columns, cluster commands in general help, and cluster TUI controls are hidden;
  explicit `bk cluster -h` remains available for setup.
- `bk cluster` shows reachable nodes, current reservations, and GPU availability.
- `bk c rec 2 1h` compares legal placements on every enabled node.
- `bk c 2 1h` submits to the node with the earliest legal start; `bk c x 2 1h`
  requests exclusive mode. The longer `bk cluster book ...` form remains valid. A
  reservation never spans hosts.
- `-t/--at` accepts the same local-friendly exact times as single-host booking. The
  client parses the value once and sends one canonical UTC instant to every node;
  `--start` remains the exact ISO form for Agents and scripts.
- `bk c 1 2h -- python /absolute/path/train.py` attaches a private scheduled command
  to the automatically selected node. GPUBK options stay before `--`; every argument
  after it is preserved for the workload. Nodes without scheduled-job and private-spec
  capabilities remain readable but cannot receive this write.
  The destination's non-interactive SSH session supplies the working directory and
  `PATH`; use executable and script paths that are valid on that host. GPUBK does not
  pretend that the submitting machine's current directory exists remotely.
- `bk @NODE 2 1h` explicitly books one node using the ordinary booking syntax.
- A successful human booking also prints every distinct destination warning with its
  node name. In particular, do not ignore a stopped or unseen scheduled-command worker:
  the reservation exists, but the command cannot launch until that user's worker runs.
  JSON keeps the same warnings inside the destination `result.warnings` array.
- `bk c` keeps the reservation table compact for reservation-only use and adds a `Job`
  column automatically when any scheduled command is present.
- Node-qualified IDs use `NODE/SHORT_ID`; the stored booking UUID is unchanged.
- Ties are resolved by start time, configured node priority, live-load confidence,
  then node name. A remote broker performs the final locked validation.
- `bk c tui` pages large node lists; `Tab` focuses reservations and `Enter` shows
  ownership, capacity, VRAM, job, and node-qualified edit/cancel commands.

`recommend`, `book`, `status`, `usage`, `history`, `edit`, and `cancel` accept
`-h` without requiring an installed catalog. Structured commands accept `-j`; cluster
edit and cancel wrap the destination response in `gpubk.cluster.v1`. Automation should
reuse `--op-id` for an exact retry of book, edit, or cancel.

## Transport

The first production transport is non-interactive OpenSSH:

- no new listening port or GPUBK network daemon;
- SSH authenticates the user on the destination host;
- the destination `bk` obtains the real remote UID and talks to its local broker;
- host-key checking remains enabled, password prompts and TTY allocation are disabled;
- commands use versioned Agent JSON and never parse colored human output;
- calls have bounded timeouts and run with forwarding disabled;
- remote error text is stripped of terminal controls and bounded before it reaches
  human output or retry diagnostics.

An HTTPS/mTLS transport can implement the same client interface later without
changing reservation or history schemas. It is useful for larger installations,
but requires certificate issuance, revocation, rate limiting, and an identity
provider; it is not required for the SSH federation.

## Identity

SSH authorization answers who may act on each node. Cross-node reporting uses a
separate administrator-owned identity map:

```text
global principal -> (node_id, numeric UID), ...
```

Inspect and correct mappings without editing JSON directly:

```bash
sudo bk admin cluster status
sudo bk admin cluster map lab-user gpu-b 2042 --yes
sudo bk admin cluster unmap gpu-b 2042 --yes
```

Numeric UIDs are node-local and usernames are display labels only. The client checks
that each response's stable node ID matches the configured endpoint. SSH determines
the actual remote actor; the client records that actor and applies explicit mappings
for reporting. Unknown mappings remain separate instead of being guessed or merged.

## Storage and NFS

Live ledgers are never shared between independent brokers. If NFS is available,
each node may export history only into its own immutable namespace:

```text
cluster-history/<node_id>/<generation>/...
```

One node cannot write another node's namespace. Readers merge versioned public API
records, not internal JSON files. Reservations still travel through the owning
node's broker. This supports backup, offline reporting, and later migration without
making NFS availability part of the booking commit path.

The optional archive is implemented. Configure the same absolute mount path in the
catalog on every client that should read it:

```bash
sudo install -d -m 0755 /srv/gpubk-cluster-history
sudo bk admin cluster history-root /srv/gpubk-cluster-history --yes
sudo bk admin cluster status
```

Export only completed UTC days. The first command below keeps 10-minute samples and
per-user summaries for the previous three years; later runs start at the newest
published end and therefore write only new days:

```bash
sudo bk admin cluster export-history --since 1095d --resolution 10m --yes
sudo bk admin cluster verify-history
bk c history --since 30d
bk c history --since 365d --all
```

Each generation is deterministic for its range, gzip compressed, checksummed, fsynced,
published by one same-directory rename, and then mode `0555` with `0444` payloads.
Repeating an identical export verifies and returns the existing generation. A new
generation that overlaps published data is rejected. Daily `usage-users` and
`usage-samples` payloads remain `gpubk.usage.v1`; raw ledgers, process arguments,
environment variables, job specs, logs, secrets, and internal storage files are never
copied. `bk c history` verifies a payload before reading it and aggregates identities
through the catalog's `(node_id, UID)` mappings. It reads complete UTC-day chunks only.

The exporter may run as root or as the configured monitor/data owner. On a normal
local filesystem, root creates the node namespace for that service UID. With NFS
`root_squash`, have the NFS administrator pre-create exactly
`ROOT/<20-character-node-id>` as mode `0755`, owned by that node's service UID, then run:

```bash
sudo -u SERVICE_USER /usr/local/bin/bk admin cluster export-history --yes
```

Use a dedicated archive root. If its top level must be writable by several node
owners, it must have the sticky bit (for example `1777`); each node namespace itself
must stay owner-writable only. A missing or unavailable archive never blocks booking,
monitoring, or live `bk c usage`. There is deliberately no import into a live usage
store: portable readers consume the public payloads in place, which preserves node
identity and avoids creating a second writer.

## Failure rules

- An unreachable node is shown as unavailable; healthy nodes remain usable.
- A disabled node is skipped without waiting for SSH and retains its catalog and history
  metadata until an administrator enables or removes it.
- Read-only comparison may become stale. The destination broker always revalidates
  under its local transaction lock before committing.
- The client rejects a recommendation whose duration, exact start, or echoed request
  differs from the submitted intent; one malformed node does not hide healthy nodes.
- Explicit `@NODE` requests never fail over to another host.
- Automatic cluster booking chooses one write-compatible node after read-only
  comparison. Read-only recommendations remain visible during a rolling upgrade, but
  cannot displace a write-compatible `best` node. Once a write is attempted, it never
  switches to another node.
- Every federated write has a stable operation ID. After a transport or protocol
  failure, the client queries that ID on the same node. It may replay the same write
  once on that node only when the query confirms that no operation is visible.
- If both the write and operation query are unavailable, the result is reported as
  unresolved with its operation ID; the client does not guess or submit elsewhere and
  tells the user to retry against the original `@NODE` only.
- Clock skew is reported. Exact starts are rejected when node clocks differ beyond
  policy. Implicit earliest-slot requests compare node-reported waiting durations,
  so a fixed clock offset does not by itself change the selected node.
- Cluster configuration or identity-map errors fail closed for cluster commands and
  never break ordinary local booking.

## Candidate acceptance

From a trusted checkout, test the exact candidate wheel on at least two SSH-reachable
hosts before creating the production catalog:

```bash
python3 tools/cluster_acceptance.py user@gpu-a user@gpu-b
```

The `user@host` values above are private transport arguments for this isolated test;
the runner writes only a temporary user-owned catalog and never copies them into the
shared root catalog.

The runner installs the wheel below each account's private temporary cache and points
every candidate process at a simulated GPU file, a private data directory, and a private
job directory. It verifies pre-catalog discovery, distinct stable node identities,
parallel context, legal recommendation, routing two exclusive reservations to separate hosts, cross-node-safe
operation replay, and cancellation. It then removes the stages and writes a private local
JSON report. It never connects to a production broker, ledger, monitor, NVML library, or
GPU device. Use SSH aliases for non-default ports, jump hosts, or identity files; the
cluster transport intentionally keeps batch mode and strict host-key checking enabled.

This is a transport and packaging acceptance test. Final production approval still needs
the single-host acceptance on every node, a second real UID authorization check, one
approved live workload, and restart/reboot checks.

## Security boundary

- The system catalog and identity map are root-owned, non-group-writable files.
- The standard system catalog also requires a root-owned, non-group-writable parent
  directory; a caller-owned file cannot impersonate `/etc/gpubk/cluster.json`.
- System catalog SSH targets never contain a username; OpenSSH resolves each caller's
  own account, and the destination broker authorizes that resulting numeric UID.
- Per-user SSH configuration may choose credentials but cannot redefine node IDs,
  policy, or identity mappings.
- Remote executable paths and node names are validated; no user string is evaluated
  as a local shell command.
- Agent writes remain idempotent and destination-UID scoped.
- Cluster status is no more privileged than running the same local GPUBK command on
  each destination account.
- Secrets, command arguments, and private job specs are not copied into shared
  history or NFS exports.

## Compatibility

The optional catalog has its own `gpubk.cluster.v1` schema. Existing configuration,
ledger, reservation, and usage records remain valid. New public objects add node
fields through versioned extensions. Unknown extensions continue to round-trip.

Rolling upgrades are fail-closed for writes. Read-only views tolerate an older node;
booking, edit, and cancel are enabled only when the selected node advertises the
required idempotency, operation-status, and node-identity capabilities.

## Delivery checklist

- [x] Validate a root-owned optional cluster catalog and identity map.
- [x] Add a bounded SSH transport with strict JSON, timeout, and host verification.
- [x] Add parallel node context/status and node-qualified reservation display.
- [x] Add cross-node recommendation with deterministic ranking and clock-skew checks.
- [x] Add explicit-node and automatic single-node booking with ambiguous-commit recovery.
- [x] Add node-qualified edit, cancel, and personal usage queries.
- [x] Add cluster-mode CLI help; keep all cluster UI hidden in single-host mode.
- [x] Add TUI node switcher and aggregate personal summary only when configured.
- [x] Add administrator catalog/identity inspection and safe update commands.
- [x] Add non-destructive node maintenance state and a per-user cluster readiness check.
- [x] Gate writes by advertised capabilities and recover ambiguous writes by
      querying the same operation ID on the same node.
- [x] Add optional per-node history export, verification, and read-only aggregation
      through the public usage API; never import into a live writer.
- [x] Test unreachable nodes, stale reads, concurrent and replayed writes, mismatched
      IDs, hostile config values, mixed versions, and simulated clock skew.
- [x] Test two isolated local node processes with independent ledgers before any
      GPU-server rollout.
- [x] Add an exact-wheel, isolated multi-host SSH acceptance runner that cannot touch
      production state.
- [ ] Verify two real GPU hosts manually before marking the release stable.
