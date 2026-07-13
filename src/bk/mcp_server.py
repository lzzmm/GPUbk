from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import Config, load_config
from .fileio import open_existing_regular
from .identity import current_actor
from .joblogs import (
    JobLogCleanupResult,
    cleanup_job_logs,
    job_log_paths,
    read_job_log_tail,
)
from .models import MODE_EXCLUSIVE, MODE_SHARED, Actor, BookingError
from .scheduler import list_active
from .sharing import normalize_share_units
from .service import (
    booking_result_payload,
    build_agent_context,
    public_reservation,
    recommend_booking,
    submit_booking,
    submit_cancellation,
    submit_edit,
)
from .storage import LedgerStore
from .timeparse import parse_duration_seconds, parse_memory_mb, parse_start, to_iso, utc_now
from .usage_api import UsageQueryService
from .worker import JobSpecCleanupResult, cleanup_job_specs


class BkMcpBackend:
    """MCP-safe application facade. Identity always comes from the server process."""

    def __init__(
        self, config: Optional[Config] = None, store: Optional[LedgerStore] = None
    ):
        self.config = config or load_config()
        if store is None:
            from .broker import ledger_store_for_config

            store = ledger_store_for_config(self.config)
        self.store = store

    @property
    def actor(self) -> Actor:
        return current_actor()

    def context(self) -> dict:
        return build_agent_context(self.config, self.store, self.actor)

    def recommend(
        self,
        count: int,
        duration: str,
        mode: str = "shared",
        start: Optional[str] = None,
        gpus: Optional[List[int]] = None,
        expected_memory: Optional[str] = None,
        share: Optional[int] = None,
    ) -> dict:
        normalized_mode = _normalize_mode(mode)
        return recommend_booking(
            self.config,
            self.store,
            self.actor,
            count=count,
            duration_seconds=parse_duration_seconds(duration),
            start_at=parse_start(start or "now"),
            mode=normalized_mode,
            preferred_gpus=gpus,
            expected_memory_mb=parse_memory_mb(expected_memory) if expected_memory else None,
            share_units=_mcp_share_units(self.config, normalized_mode, share),
            allow_queue=start is None,
        )

    def book(
        self,
        count: int,
        duration: str,
        operation_id: str,
        mode: str = "shared",
        start: Optional[str] = None,
        gpus: Optional[List[int]] = None,
        expected_memory: Optional[str] = None,
        command: Optional[List[str]] = None,
        working_directory: Optional[str] = None,
        share: Optional[int] = None,
    ) -> dict:
        if not operation_id:
            raise BookingError("operation_id is required for retry-safe MCP writes")
        normalized_mode = _normalize_mode(mode)
        submission = submit_booking(
            self.config,
            self.store,
            self.actor,
            count=count,
            duration_seconds=parse_duration_seconds(duration),
            start_at=parse_start(start or "now"),
            mode=normalized_mode,
            preferred_gpus=gpus,
            expected_memory_mb=parse_memory_mb(expected_memory) if expected_memory else None,
            share_units=_mcp_share_units(self.config, normalized_mode, share),
            allow_queue=start is None,
            operation_id=operation_id,
            command_argv=command,
            working_directory=working_directory,
        )
        result = submission.result
        status = "exists" if not result.created else ("queued" if result.queued else "created")
        return booking_result_payload(status, submission, self.actor, self.store.last_warning)

    def list_reservations(self, mine_only: bool = False) -> dict:
        active = list_active(self.store.load())
        if mine_only:
            active = [item for item in active if int(item.get("uid", -1)) == self.actor.uid]
        return {
            "schema_version": "bk.agent.v1",
            "kind": "reservations",
            "reservations": [
                public_reservation(item, self.actor, self.config.max_shared_users)
                for item in active
            ],
        }

    def usage(
        self,
        since: str = "24h",
        resolution: str = "auto",
        include_events: bool = False,
        limit: int = 1000,
    ) -> dict:
        seconds = parse_duration_seconds(since)
        end = utc_now()
        start = end - timedelta(seconds=seconds)
        api = UsageQueryService(self.config)
        payload = {
            "schema_version": "gpubk.usage.v1",
            "kind": "my-usage",
            "generated_at": to_iso(end),
            "summary": api.users(
                start=start,
                end=end,
                resolution=resolution,
                uid=self.actor.uid,
                limit=1,
            ),
            "samples": api.samples(
                start=start,
                end=end,
                resolution=resolution,
                uid=self.actor.uid,
                limit=limit,
            ),
        }
        if include_events:
            payload["events"] = api.events(
                start=start,
                end=end,
                uid=self.actor.uid,
                limit=min(limit, 5000),
            )
        return payload

    def edit(
        self,
        reservation_id: str,
        operation_id: str,
        duration: Optional[str] = None,
        mode: Optional[str] = None,
        start: Optional[str] = None,
        gpus: Optional[List[int]] = None,
        count: Optional[int] = None,
        expected_memory: Optional[str] = None,
        allow_queue: bool = False,
        share: Optional[int] = None,
    ) -> dict:
        if not operation_id:
            raise BookingError("operation_id is required for retry-safe MCP writes")
        if all(
            value is None
            for value in (
                duration,
                mode,
                start,
                gpus,
                count,
                expected_memory,
                share,
            )
        ):
            raise BookingError("edit requires at least one changed field")
        reservation = self._resolve_own_reservation(reservation_id)
        normalized_mode = _normalize_mode(mode) if mode is not None else None
        update_memory = expected_memory is not None
        memory_mb = None
        if expected_memory not in {None, "-"}:
            memory_mb = parse_memory_mb(expected_memory)
        share_units = _mcp_share_units(self.config, normalized_mode, share)
        submission = submit_edit(
            self.config,
            self.store,
            self.actor,
            str(reservation["id"]),
            duration_seconds=parse_duration_seconds(duration) if duration is not None else None,
            start_at=parse_start(start) if start is not None else None,
            mode=normalized_mode,
            preferred_gpus=gpus,
            count=count,
            expected_memory_mb=memory_mb,
            update_expected_memory=update_memory,
            share_units=share_units,
            update_share_units=share is not None,
            allow_queue=allow_queue,
            operation_id=operation_id,
        )
        result = submission.result
        status = "exists" if not result.created else ("queued" if result.queued else "updated")
        return booking_result_payload(status, submission, self.actor, self.store.last_warning)

    def cancel(self, reservation_id: str) -> dict:
        reservation = self._resolve_own_active(reservation_id)
        cancellation = submit_cancellation(
            self.config,
            self.store,
            self.actor,
            str(reservation["id"]),
        )
        return {
            "schema_version": "bk.agent.v1",
            "kind": "cancellation_result",
            "reservation": public_reservation(
                cancellation.reservation,
                self.actor,
                self.config.max_shared_users,
            ),
            "private_job_cleanup": cancellation.cleanup.as_dict(),
            "warning": self.store.last_warning,
        }

    def cleanup_private_job_specs(self) -> dict:
        try:
            cleanup = cleanup_job_specs(self.config, self.store, self.actor)
        except (BookingError, OSError, ValueError) as exc:
            cleanup = JobSpecCleanupResult(
                failed=1,
                warnings=(f"private job spec cleanup failed: {exc}",),
            )
        return {
            "schema_version": "bk.agent.v1",
            "kind": "job-spec-cleanup",
            "private_job_cleanup": cleanup.as_dict(),
        }

    def cleanup_private_job_logs(self) -> dict:
        try:
            cleanup = cleanup_job_logs(
                self.config,
                self.store.load(),
                self.actor,
            )
        except (BookingError, OSError, ValueError) as exc:
            cleanup = JobLogCleanupResult(
                failed=1,
                warnings=(f"private job log cleanup failed: {exc}",),
            )
        return {
            "schema_version": "bk.agent.v1",
            "kind": "job-log-cleanup",
            "private_job_log_cleanup": cleanup.as_dict(),
        }

    def read_job_log(self, reservation_id: str, max_chars: int = 32000) -> dict:
        if max_chars < 1 or max_chars > 128000:
            raise BookingError("max_chars must be between 1 and 128000")
        reservation = self._resolve_own_job(reservation_id)
        paths = job_log_paths(self.config, str(reservation["id"]))
        text = read_job_log_tail(self.config, str(reservation["id"]), max_chars) if paths else ""
        return {
            "schema_version": "bk.agent.v1",
            "kind": "job_log",
            "reservation_id": reservation["id"],
            "status": reservation["job"].get("status"),
            "available": bool(paths),
            "segments": len(paths),
            "text": text,
            "truncated_to_chars": max_chars,
        }

    def _resolve_own_active(self, token: str) -> dict:
        mine = [item for item in list_active(self.store.load()) if int(item.get("uid", -1)) == self.actor.uid]
        return _resolve_token(mine, token)

    def _resolve_own_reservation(self, token: str) -> dict:
        mine = [
            item
            for item in self.store.load().get("reservations", [])
            if int(item.get("uid", -1)) == self.actor.uid
        ]
        return _resolve_token(mine, token)

    def _resolve_own_job(self, token: str) -> dict:
        mine = [
            item
            for item in self.store.load().get("reservations", [])
            if int(item.get("uid", -1)) == self.actor.uid and isinstance(item.get("job"), dict)
        ]
        return _resolve_token(mine, token)


def create_mcp_server(backend: Optional[BkMcpBackend] = None):
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise RuntimeError("MCP support is optional; install with: pip install 'gpubk[mcp]'") from exc

    api = backend or BkMcpBackend()
    mcp = FastMCP(
        "GPUbk",
        json_response=True,
        instructions=(
            "Inspect context or call recommend before booking. "
            "Create and edit writes require a stable operation_id. "
            "Never invent a UID; identity is the MCP process UID."
        ),
    )

    @mcp.resource("bk://context")
    def resource_context() -> str:
        """Current privacy-safe GPU allocation context."""
        return json.dumps(api.context(), ensure_ascii=False, sort_keys=True)

    @mcp.resource("bk://usage/me/recent")
    def resource_my_usage() -> str:
        """Current UID's versioned 24-hour usage summary."""
        return json.dumps(api.usage(), ensure_ascii=False, sort_keys=True)

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    idempotent_write = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    destructive_write = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
    idempotent_cleanup = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(annotations=read_only, structured_output=True)
    def get_gpu_context() -> dict[str, object]:
        """Read policy, live GPU state, memory, forecast load, and active reservations."""
        return api.context()

    @mcp.tool(annotations=read_only, structured_output=True)
    def recommend_gpu_booking(
        count: int,
        duration: str,
        mode: str = "shared",
        start: Optional[str] = None,
        gpus: Optional[List[int]] = None,
        expected_memory: Optional[str] = None,
        share: Optional[int] = None,
    ) -> dict[str, object]:
        """Read-only recommendation. Omit start to allow earliest-slot queueing; explicit start is exact."""
        return api.recommend(
            count=count,
            duration=duration,
            mode=mode,
            start=start,
            gpus=gpus,
            expected_memory=expected_memory,
            share=share,
        )

    @mcp.tool(annotations=idempotent_write, structured_output=True)
    def create_gpu_booking(
        count: int,
        duration: str,
        operation_id: str,
        mode: str = "shared",
        start: Optional[str] = None,
        gpus: Optional[List[int]] = None,
        expected_memory: Optional[str] = None,
        command: Optional[List[str]] = None,
        working_directory: Optional[str] = None,
        share: Optional[int] = None,
    ) -> dict[str, object]:
        """Create an idempotent booking as this MCP process UID; optionally attach an argv command."""
        return api.book(
            count=count,
            duration=duration,
            operation_id=operation_id,
            mode=mode,
            start=start,
            gpus=gpus,
            expected_memory=expected_memory,
            command=command,
            working_directory=working_directory,
            share=share,
        )

    @mcp.tool(annotations=read_only, structured_output=True)
    def list_gpu_reservations(mine_only: bool = False) -> dict[str, object]:
        """List active reservations using the stable privacy-safe schema."""
        return api.list_reservations(mine_only)

    @mcp.tool(annotations=read_only, structured_output=True)
    def get_my_gpu_usage(
        since: str = "24h",
        resolution: str = "auto",
        include_events: bool = False,
        limit: int = 1000,
    ) -> dict[str, object]:
        """Read the current UID's historical GPU usage; never accepts another UID."""
        return api.usage(since, resolution, include_events, limit)

    @mcp.tool(annotations=idempotent_write, structured_output=True)
    def edit_my_gpu_booking(
        reservation_id: str,
        operation_id: str,
        duration: Optional[str] = None,
        mode: Optional[str] = None,
        start: Optional[str] = None,
        gpus: Optional[List[int]] = None,
        count: Optional[int] = None,
        expected_memory: Optional[str] = None,
        allow_queue: bool = False,
        share: Optional[int] = None,
    ) -> dict[str, object]:
        """Idempotently edit this UID's future booking; queue never repairs past starts."""
        return api.edit(
            reservation_id=reservation_id,
            operation_id=operation_id,
            duration=duration,
            mode=mode,
            start=start,
            gpus=gpus,
            count=count,
            expected_memory=expected_memory,
            allow_queue=allow_queue,
            share=share,
        )

    @mcp.tool(annotations=destructive_write, structured_output=True)
    def cancel_my_gpu_booking(reservation_id: str) -> dict[str, object]:
        """Cancel only a reservation owned by this MCP process UID; short IDs are accepted when unique."""
        return api.cancel(reservation_id)

    @mcp.tool(annotations=idempotent_cleanup, structured_output=True)
    def cleanup_my_job_specs() -> dict[str, object]:
        """Prune only this UID's terminal, expired, or old orphaned private command specs."""
        return api.cleanup_private_job_specs()

    @mcp.tool(annotations=idempotent_cleanup, structured_output=True)
    def cleanup_my_job_logs() -> dict[str, object]:
        """Apply this UID's age and quota policy to terminal private job logs."""
        return api.cleanup_private_job_logs()

    @mcp.tool(annotations=read_only, structured_output=True)
    def read_my_job_log(reservation_id: str, max_chars: int = 32000) -> dict[str, object]:
        """Read a bounded tail of this UID's private scheduled-job log."""
        return api.read_job_log(reservation_id, max_chars)

    @mcp.prompt()
    def plan_gpu_experiment(count: int, duration: str, expected_memory: str = "unknown") -> str:
        return (
            f"Plan a GPU experiment needing {count} GPU(s) for {duration}, expected memory {expected_memory} per GPU. "
            "First inspect bk://context, then call recommend_gpu_booking. Explain live-load and memory warnings. "
            "Only call create_gpu_booking after the user approves, and reuse one stable operation_id on retries."
        )

    return mcp


def main(argv: Optional[List[str]] = None, *, prog: str = "bk-mcp") -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run the GPUbk Model Context Protocol server over stdio.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"gpubk {__version__}",
    )
    parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        server = create_mcp_server()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    server.run(transport="stdio")
    return 0


def _normalize_mode(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"s", MODE_SHARED}:
        return MODE_SHARED
    if normalized in {"x", MODE_EXCLUSIVE}:
        return MODE_EXCLUSIVE
    raise BookingError("mode must be s/shared or x/exclusive")


def _mcp_share_units(
    config: Config,
    mode: Optional[str],
    share: Optional[int],
) -> Optional[int]:
    if share is None:
        return None
    if mode == MODE_EXCLUSIVE:
        raise BookingError("share applies only to shared reservations")
    try:
        return normalize_share_units(share, config.max_shared_users)
    except (TypeError, ValueError) as exc:
        raise BookingError(str(exc)) from exc


def _resolve_token(reservations: List[dict], token: str) -> dict:
    if not token:
        raise BookingError("reservation ID is required")
    matches = [item for item in reservations if str(item.get("id", "")).startswith(token)]
    if not matches:
        raise BookingError("reservation not found for current UID")
    if len(matches) > 1:
        raise BookingError("ambiguous reservation short ID")
    return matches[0]


def _read_tail(path: Path, max_chars: int) -> str:
    fd = open_existing_regular(path)
    with os.fdopen(fd, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_chars * 4))
        text = fh.read().decode("utf-8", errors="replace")
    return text[-max_chars:]


if __name__ == "__main__":
    raise SystemExit(main())
