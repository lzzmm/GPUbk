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

## User model

- With no cluster catalog, GPUBK stays in single-host mode. Node selectors, node
  columns, cluster help, and cluster TUI controls are hidden.
- `bk cluster` shows reachable nodes, current reservations, and GPU availability.
- `bk cluster recommend 2 1h` compares legal placements on every enabled node.
- `bk cluster book 2 1h` submits to the node with the earliest legal start. A
  reservation never spans hosts.
- `bk @NODE 2 1h` explicitly books one node using the ordinary booking syntax.
- Node-qualified IDs use `NODE/SHORT_ID`; the stored booking UUID is unchanged.
- Ties are resolved by start time, configured node priority, live-load confidence,
  then node name. A remote broker performs the final locked validation.

## Transport

The first production transport is non-interactive OpenSSH:

- no new listening port or GPUBK network daemon;
- SSH authenticates the user on the destination host;
- the destination `bk` obtains the real remote UID and talks to its local broker;
- host-key checking remains enabled, password prompts and TTY allocation are disabled;
- commands use versioned Agent JSON and never parse colored human output;
- calls have bounded timeouts and run with forwarding disabled.

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

## Failure rules

- An unreachable node is shown as unavailable; healthy nodes remain usable.
- Read-only comparison may become stale. The destination broker always revalidates
  under its local transaction lock before committing.
- Explicit `@NODE` requests never fail over to another host.
- Automatic cluster booking chooses one write-compatible node after read-only
  comparison. Once a write is attempted, it never switches to another node.
- Every federated write has a stable operation ID. After a transport or protocol
  failure, the client queries that ID on the same node. It may replay the same write
  once on that node only when the query confirms that no operation is visible.
- If both the write and operation query are unavailable, the result is reported as
  unresolved with its operation ID; the client does not guess or submit elsewhere.
- Clock skew is reported. Exact starts are rejected when node clocks differ beyond
  policy. Implicit earliest-slot requests compare node-reported waiting durations,
  so a fixed clock offset does not by itself change the selected node.
- Cluster configuration or identity-map errors fail closed for cluster commands and
  never break ordinary local booking.

## Security boundary

- The system catalog and identity map are root-owned, non-group-writable files.
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
- [x] Gate writes by advertised capabilities and recover ambiguous writes by
      querying the same operation ID on the same node.
- [ ] Add optional per-node history export/import through the public usage API.
- [x] Test unreachable nodes, stale reads, concurrent and replayed writes, mismatched
      IDs, hostile config values, mixed versions, and simulated clock skew.
- [x] Test two isolated local node processes with independent ledgers before any
      GPU-server rollout.
- [ ] Verify two real GPU hosts manually before marking the release stable.
