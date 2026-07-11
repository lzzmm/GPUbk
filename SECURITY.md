# Security Policy

## Scope

BK is a cooperative POSIX scheduler. It protects ledger integrity and enforces application-level UID ownership when every writer uses BK. It is not a kernel GPU access-control system and cannot prevent a user with direct device permission from launching CUDA outside BK.

Supported security boundaries:

- MCP, CLI, TUI, and worker identity comes from the local process UID.
- Shared-ledger writes use an advisory lock, WAL journal, atomic replacement, and idempotent audit events.
- Scheduled command arguments live in UID-owned `0600` specs, not the shared ledger.
- External allocators can advise ordering but cannot bypass deterministic validation.

Administrator responsibilities:

- Configure a dedicated Unix group and correct setgid directory permissions.
- Verify `flock` and atomic rename behavior on the actual NFS/FUSE mount.
- Control `/dev/nvidia*` access separately if hard enforcement is required.
- Run MCP over per-user local stdio unless an authenticated remote transport is deliberately engineered.
- Treat allocator commands as trusted code running with the configuring user's privileges.

## Reporting

Do not include credentials, private command lines, production ledger contents, or user data in a public issue. Contact the package maintainers through the private security-reporting channel associated with the source repository or distribution. A public repository should enable private vulnerability reporting before the first release.

