from __future__ import annotations

import errno
import gzip
import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Protocol, Sequence, Tuple

from .collector_status import (
    COLLECTOR_STATUS_SCHEMA_VERSION,
    CollectorStatusError,
    absent_collector_status,
    classify_collector_document,
    invalid_collector_status,
    validate_collector_document,
)
from .fileio import (
    ensure_directory,
    file_type_name,
    fsync_directory,
    open_existing_regular,
    setgid_directory_gid,
)
from .jsonl import JsonlFormatError, append_json_objects
from .storage import FileLock
from .timeparse import parse_iso, to_iso, utc_now
from .usage_schema import (
    EVENT_SCHEMA_VERSION,
    ROLLUP_SCHEMA_VERSION,
    STORE_FORMAT,
    STORE_FORMAT_MAJOR,
    STORE_FORMAT_MINOR,
    TIER_FOR_RESOLUTION,
    aggregate_rollups,
    decode_event,
    decode_rollup,
    decode_workload,
    encode_event,
    encode_rollup,
    encode_workload,
    unknown_storage_fields,
)
from .workload import WorkloadDescriptor, describe_workload


MAX_USAGE_LINE_BYTES = 1024 * 1024
MAX_DICTIONARY_BYTES = 64 * 1024 * 1024
MAX_PARTITION_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
PARTITION_SUFFIX = ".v1.jsonl"


class UsageFormatError(OSError):
    pass


class TelemetrySink(Protocol):
    """Stable writer boundary for the bundled or an external collector."""

    def append_events(self, events: Iterable[dict]) -> int: ...

    def append_rollups(self, rollups: Iterable[dict]) -> int: ...

    def register_workload(self, uid: Optional[int], descriptor: WorkloadDescriptor) -> int: ...

    def load_state(self) -> Dict[str, dict]: ...

    def save_state(self, processes: Dict[str, dict]) -> None: ...

    def commit_state_transition(self, events: Sequence[dict], processes: Dict[str, dict]) -> int: ...

    def load_load_history(self) -> dict: ...

    def save_load_history(self, history: dict) -> None: ...

    def maintain(
        self,
        policy: "UsageRetentionPolicy",
        *,
        now: Optional[datetime] = None,
        dry_run: bool = False,
    ) -> dict: ...


class CollectorStatusSink(Protocol):
    """Optional health extension for collectors that expose liveness."""

    def save_collector_status(self, payload: dict) -> None: ...

    def load_collector_status(
        self,
        *,
        now: Optional[datetime] = None,
        expected_gpu_count: Optional[int] = None,
    ) -> dict: ...


@dataclass(frozen=True)
class UsageRetentionPolicy:
    load_minutes: int = 120
    minute_days: int = 30
    five_minute_days: int = 365
    ten_minute_days: int = 1095
    hourly_days: int = 1500
    daily_days: int = 0
    event_days: int = 365

    @classmethod
    def from_config(cls, config) -> "UsageRetentionPolicy":
        return cls(
            load_minutes=int(config.usage_load_window_minutes),
            minute_days=int(config.usage_minute_retention_days),
            five_minute_days=int(config.usage_five_minute_retention_days),
            ten_minute_days=int(config.usage_ten_minute_retention_days),
            hourly_days=int(config.usage_hourly_retention_days),
            daily_days=int(config.usage_daily_retention_days),
            event_days=int(config.usage_event_retention_days),
        )


class UsageAuditStore:
    """Versioned, append-oriented telemetry store with legacy v1 readers."""

    def __init__(
        self,
        data_dir: Path,
        lock_timeout_seconds: float = 10.0,
        file_mode: int = 0o600,
        dir_mode: int = 0o700,
        storage_gid: Optional[int] = None,
    ):
        self.data_dir = data_dir
        self.lock_timeout_seconds = lock_timeout_seconds
        self.file_mode = file_mode
        self.dir_mode = dir_mode
        self.storage_gid = storage_gid
        self.lock_path = data_dir / "usage.lock"

        self.usage_dir = data_dir / "usage"
        self.meta_path = self.usage_dir / "store.json"
        self.state_path = self.usage_dir / "state.json"
        self.transition_journal_path = self.usage_dir / "state-transition.json"
        self.load_path = self.usage_dir / "load.json"
        self.collector_path = self.usage_dir / "collector.json"
        self.users_path = self.usage_dir / "users.json"
        self.workloads_path = self.usage_dir / "workloads.v1.jsonl"
        self.key_path = self.usage_dir / "workload.key"
        self.migrations_dir = self.usage_dir / "migrations"

        # Pre-versioned paths remain readable and are never silently deleted.
        self.legacy_state_path = data_dir / "usage-state.json"
        self.events_path = data_dir / "usage-events.jsonl"
        self.rollups_path = data_dir / "usage-rollups.jsonl"
        self.legacy_load_path = data_dir / "usage-load.json"

        self.last_warnings: List[str] = []
        self._users: Optional[dict] = None
        self._workloads_by_fingerprint: Optional[Dict[str, int]] = None
        self._workloads_by_id: Optional[Dict[int, dict]] = None
        self._workload_key: Optional[bytes] = None

    def ensure(self) -> None:
        ensure_directory(self.data_dir, self.dir_mode, require_mode=True)
        managed_gid = self._managed_gid()
        ensure_directory(
            self.usage_dir,
            self.dir_mode,
            require_mode=True,
            expected_gid=managed_gid,
        )
        ensure_directory(
            self.migrations_dir,
            self.dir_mode,
            require_mode=True,
            expected_gid=managed_gid,
        )
        self._validate_core_files_for_write(managed_gid)
        if os.path.lexists(self.meta_path):
            self._validate_meta(self._read_json(self.meta_path))
            return
        payload = {
            "format": STORE_FORMAT,
            "format_major": STORE_FORMAT_MAJOR,
            "format_minor": STORE_FORMAT_MINOR,
            "min_reader_major": 1,
            "min_reader_minor": 0,
            "min_writer_major": 1,
            "min_writer_minor": 0,
            "features": [
                "daily-partitions",
                "compact-json-objects",
                "gzip-closed-partitions",
                "partition-checksums",
                "workload-dictionary",
            ],
            "created_at": to_iso(utc_now()),
        }
        _atomic_write_json(
            self.meta_path,
            payload,
            self.file_mode,
            self.usage_dir,
            ".store.",
            expected_gid=managed_gid,
        )

    def lock(self, timeout_seconds: Optional[float] = None) -> FileLock:
        ensure_directory(self.data_dir, self.dir_mode, require_mode=True)
        timeout = self.lock_timeout_seconds if timeout_seconds is None else timeout_seconds
        return FileLock(
            self.lock_path,
            timeout,
            self.file_mode,
            self.dir_mode,
            self._managed_gid(),
        )

    def _managed_gid(self) -> Optional[int]:
        return setgid_directory_gid(
            self.data_dir,
            self.dir_mode,
            expected_gid=self.storage_gid,
        )

    def load_state(self) -> Dict[str, dict]:
        if self.transition_journal_path.exists():
            self._recover_state_transition()
        path = self.state_path if self.state_path.exists() else self.legacy_state_path
        if not path.exists():
            return {}
        try:
            payload = self._read_json(path)
            if payload.get("version") != 1 or not isinstance(payload.get("processes"), dict):
                return {}
            return payload["processes"]
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def save_state(self, processes: Dict[str, dict]) -> None:
        self.ensure()
        _atomic_write_json(
            self.state_path,
            {"version": 1, "processes": processes},
            self.file_mode,
            self.usage_dir,
            ".state.",
            expected_gid=self._managed_gid(),
        )

    def commit_state_transition(self, events: Sequence[dict], processes: Dict[str, dict]) -> int:
        self.ensure()
        sanitized_events = [dict(item) for item in events]
        for item in sanitized_events:
            item.pop("command", None)
        _atomic_write_json(
            self.transition_journal_path,
            {
                "version": 1,
                "created_at": to_iso(utc_now()),
                "events": sanitized_events,
                "processes": processes,
            },
            self.file_mode,
            self.usage_dir,
            ".state-transition.",
            expected_gid=self._managed_gid(),
        )
        written = self.append_events(sanitized_events)
        self.save_state(processes)
        self.transition_journal_path.unlink(missing_ok=True)
        fsync_directory(self.usage_dir)
        return written

    def load_load_history(self) -> dict:
        path = self.load_path if self.load_path.exists() else self.legacy_load_path
        if not path.exists():
            return {"version": 1, "updated_at": None, "gpus": {}}
        try:
            payload = self._read_json(path)
            if payload.get("version") != 1 or not isinstance(payload.get("gpus"), dict):
                raise ValueError("invalid load history")
            return payload
        except (OSError, ValueError, json.JSONDecodeError):
            return {"version": 1, "updated_at": None, "gpus": {}}

    def save_load_history(self, history: dict) -> None:
        self.ensure()
        _atomic_write_json(
            self.load_path,
            history,
            self.file_mode,
            self.usage_dir,
            ".load.",
            expected_gid=self._managed_gid(),
        )

    def save_collector_status(self, payload: dict) -> None:
        validate_collector_document(payload)
        self.ensure()
        _atomic_write_json(
            self.collector_path,
            payload,
            self.file_mode,
            self.usage_dir,
            ".collector.",
            expected_gid=self._managed_gid(),
        )

    def load_collector_status(
        self,
        *,
        now: Optional[datetime] = None,
        expected_gpu_count: Optional[int] = None,
    ) -> dict:
        if not os.path.lexists(self.collector_path):
            return absent_collector_status()
        try:
            payload = self._read_json(self.collector_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return invalid_collector_status(f"{type(exc).__name__}: {exc}")
        try:
            return classify_collector_document(
                payload,
                now=now,
                expected_gpu_count=expected_gpu_count,
            )
        except CollectorStatusError as exc:
            incompatible = payload.get("schema_version") != COLLECTOR_STATUS_SCHEMA_VERSION
            return invalid_collector_status(str(exc), incompatible=incompatible)

    def append_events(self, events: Iterable[dict]) -> int:
        items = [dict(item) for item in events if str(item.get("status", "")) != "system"]
        if not items:
            return 0
        self.ensure()
        grouped: Dict[date, List[dict]] = {}
        for item in items:
            timestamp = parse_iso(str(item["timestamp"]))
            grouped.setdefault(timestamp.date(), []).append(encode_event(item))
        for day in grouped:
            self._validate_append_target("events", day)
        self._remember_users(items)
        return sum(self._append_event_partition(day, records) for day, records in grouped.items())

    def append_rollups(self, rollups: Iterable[dict]) -> int:
        items = [
            dict(item)
            for item in rollups
            if item.get("uid") is not None and str(item.get("status", "")) not in {"idle", "system"}
        ]
        if not items:
            return 0
        self.ensure()
        grouped: Dict[date, List[dict]] = {}
        for item in items:
            start = parse_iso(str(item["window_start"]))
            grouped.setdefault(start.date(), []).append(encode_rollup(item))
        for day in grouped:
            self._validate_append_target("minute", day)
        self._remember_users(items)
        return sum(self._append_partition("minute", day, records) for day, records in grouped.items())

    def register_workload(self, uid: Optional[int], descriptor: WorkloadDescriptor) -> int:
        self.ensure()
        by_fingerprint, by_id = self._workload_registry()
        key = self._load_or_create_workload_key()
        material = f"{uid if uid is not None else -1}\x00{descriptor.signature}".encode("utf-8", errors="replace")
        fingerprint = hmac.new(key, material, hashlib.sha256).hexdigest()
        existing = by_fingerprint.get(fingerprint)
        if existing is not None:
            return existing
        workload_id = max(by_id, default=0) + 1
        record = encode_workload(workload_id, fingerprint, descriptor, utc_now())
        self._append_jsonl(self.workloads_path, [record])
        by_fingerprint[fingerprint] = workload_id
        by_id[workload_id] = record
        return workload_id

    def workloads(self) -> Dict[int, dict]:
        self._check_readable()
        _by_fingerprint, by_id = self._workload_registry()
        return {workload_id: decode_workload(record) for workload_id, record in by_id.items()}

    def recent_events(self, limit: int = 20) -> List[dict]:
        if limit < 1:
            return []
        if not self._all_partition_paths("events") and self._legacy_visible() and self.events_path.exists():
            return _recent_plain_jsonl(self.events_path, limit)
        records = list(self.iter_events(limit=limit, newest_first=True))
        records.reverse()
        return records

    def recent_rollups(self, limit: int = 20) -> List[dict]:
        if limit < 1:
            return []
        if not self._all_partition_paths("minute") and self._legacy_visible() and self.rollups_path.exists():
            return _recent_plain_jsonl(self.rollups_path, limit)
        records = list(self.iter_rollups("minute", limit=limit, newest_first=True))
        records.reverse()
        return records

    def rollups_since(self, cutoff: datetime, max_records: int = 200_000) -> Tuple[List[dict], bool]:
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        records = list(self.iter_rollups("minute", start=cutoff, limit=max_records + 1))
        return records[:max_records], len(records) > max_records

    def iter_events(
        self,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        uid: Optional[int] = None,
        gpu: Optional[int] = None,
        limit: Optional[int] = None,
        newest_first: bool = False,
    ) -> Iterator[dict]:
        self._check_readable()
        count = 0
        seen = set()
        sources = [(path, True) for path in self._partition_paths("events", start, end, newest_first)]
        if self._legacy_visible() and self.events_path.exists():
            sources.append((self.events_path, False))
        for path, versioned in sources:
            remaining = None if limit is None else limit - count
            if remaining is not None and remaining <= 0:
                return

            def decoder(raw: dict) -> Optional[Tuple[tuple, dict]]:
                return self._event_candidate(
                    raw, path, start=start, end=end, uid=uid, gpu=gpu, versioned=versioned
                )

            for key, record in self._ordered_candidates(
                path,
                newest_first=newest_first,
                max_records=remaining,
                seen=seen,
                decoder=decoder,
            ):
                seen.add(key)
                yield record
                count += 1
                if limit is not None and count >= limit:
                    return

    def iter_rollups(
        self,
        tier: str,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        uid: Optional[int] = None,
        gpu: Optional[int] = None,
        limit: Optional[int] = None,
        newest_first: bool = False,
    ) -> Iterator[dict]:
        if tier not in set(TIER_FOR_RESOLUTION.values()):
            raise ValueError(f"unknown usage tier: {tier}")
        self._check_readable()
        count = 0
        seen = set()
        sources = [(path, True) for path in self._partition_paths(tier, start, end, newest_first)]
        if tier == "minute" and self._legacy_visible() and self.rollups_path.exists():
            sources.append((self.rollups_path, False))
        for path, versioned in sources:
            remaining = None if limit is None else limit - count
            if remaining is not None and remaining <= 0:
                return

            def decoder(raw: dict) -> Optional[Tuple[tuple, dict]]:
                return self._rollup_candidate(
                    raw, path, start=start, end=end, uid=uid, gpu=gpu, versioned=versioned
                )

            for key, record in self._ordered_candidates(
                path,
                newest_first=newest_first,
                max_records=remaining,
                seen=seen,
                decoder=decoder,
            ):
                seen.add(key)
                yield record
                count += 1
                if limit is not None and count >= limit:
                    return

    def username_for_uid(self, uid: Optional[int]) -> str:
        if uid is None:
            return "?"
        users = self._load_users().get("users", {})
        item = users.get(str(uid), {})
        return str(item.get("username", uid))

    def maintain(
        self,
        policy: UsageRetentionPolicy,
        *,
        now: Optional[datetime] = None,
        dry_run: bool = False,
    ) -> dict:
        if dry_run:
            if self.meta_path.exists():
                self._validate_meta(self._read_json(self.meta_path))
        else:
            self.ensure()
            self._validate_usage_tree_for_write()
        current = (now or utc_now()).astimezone(timezone.utc)
        today = current.date()
        report = {"generated": [], "sealed": [], "removed": [], "blocked": [], "dry_run": dry_run}
        unsafe_dates = set()

        source_targets = {
            "minute": ("five-minute", "ten-minute", "hourly", "daily"),
            "five-minute": ("ten-minute", "hourly", "daily"),
            "ten-minute": ("hourly", "daily"),
            "hourly": ("daily",),
        }
        for source_tier, targets in source_targets.items():
            for day in self._tier_dates(source_tier):
                if day >= today:
                    continue
                try:
                    records, compactable = self._records_for_day(source_tier, day)
                except (OSError, ValueError) as exc:
                    report["blocked"].append(f"{source_tier}/{day}: {exc}")
                    unsafe_dates.add((source_tier, day))
                    continue
                if not records:
                    continue
                if not compactable:
                    report["blocked"].append(f"{source_tier}/{day}: unknown fields require a newer compactor")
                    unsafe_dates.add((source_tier, day))
                    continue
                for target_tier in targets:
                    if self._partition_exists(target_tier, day):
                        continue
                    target_seconds = _resolution_for_tier(target_tier)
                    try:
                        generated = aggregate_rollups(records, target_seconds)
                    except ValueError as exc:
                        report["blocked"].append(f"{source_tier}/{day}->{target_tier}: {exc}")
                        unsafe_dates.add((source_tier, day))
                        continue
                    report["generated"].append(f"{target_tier}/{day}:{len(generated)}")
                    if not dry_run:
                        self._write_closed_partition(target_tier, day, [encode_rollup(item) for item in generated])

        for tier in ("events", "minute", "five-minute", "ten-minute", "hourly", "daily"):
            for path in self._plain_partition_paths(tier):
                day = _partition_day(path)
                if day is None or day >= today:
                    continue
                report["sealed"].append(str(path.relative_to(self.usage_dir)))
                if not dry_run:
                    self._seal_partition(path)

        retention = {
            "events": policy.event_days,
            "minute": policy.minute_days,
            "five-minute": policy.five_minute_days,
            "ten-minute": policy.ten_minute_days,
            "hourly": policy.hourly_days,
            "daily": policy.daily_days,
        }
        for tier, days in retention.items():
            if days <= 0:
                continue
            cutoff = current - timedelta(days=days)
            for path in self._all_partition_paths(tier):
                day = _partition_day(path)
                if day is None or _day_end(day) > cutoff:
                    continue
                if (tier, day) in unsafe_dates:
                    report["blocked"].append(f"{tier}/{day}: retained because compaction was blocked")
                    continue
                if tier != "events" and not self._safe_to_remove_tier(tier, day):
                    report["blocked"].append(f"{tier}/{day}: derived history is incomplete")
                    continue
                relative = str(path.relative_to(self.usage_dir))
                report["removed"].append(relative)
                if not dry_run:
                    self._ensure_usage_directory(path.parent)
                    source_fd = open_existing_regular(
                        path,
                        expected_mode=self.file_mode,
                        expected_gid=self._managed_gid(),
                    )
                    os.close(source_fd)
                    metadata_path = _meta_path_for(path)
                    if os.path.lexists(metadata_path):
                        metadata_fd = open_existing_regular(
                            metadata_path,
                            expected_mode=self.file_mode,
                            expected_gid=self._managed_gid(),
                        )
                        os.close(metadata_fd)
                    path.unlink(missing_ok=True)
                    metadata_path.unlink(missing_ok=True)
                    fsync_directory(path.parent)
        return report

    def migrate_legacy(self, *, dry_run: bool = True) -> dict:
        if not dry_run:
            self.ensure()
            self._validate_usage_tree_for_write()
        marker = self.migrations_dir / "legacy-v1.json"
        sources = [path for path in (self.events_path, self.rollups_path) if path.exists()]
        marker_payload = self._read_json(marker) if marker.exists() else None
        source_changed = bool(marker_payload) and _legacy_sources_changed(marker_payload, sources)
        report = {
            "migration": "legacy-v1",
            "dry_run": dry_run,
            "already_migrated": marker.exists() and not source_changed,
            "source_changed": source_changed,
            "sources": [str(path) for path in sources],
            "events": 0,
            "rollups": 0,
        }
        if report["already_migrated"] or not sources:
            return report
        events = [decode_event(item, self.username_for_uid) for item in self._read_jsonl(self.events_path)] if self.events_path.exists() else []
        rollups = [decode_rollup(item, self.username_for_uid) for item in self._read_jsonl(self.rollups_path)] if self.rollups_path.exists() else []
        report["events"] = len(events)
        report["rollups"] = len(rollups)
        if dry_run:
            return report
        for event in events:
            if event.get("workload_id") is None and event.get("command") and event.get("uid") is not None:
                event["workload_id"] = self.register_workload(
                    int(event["uid"]),
                    describe_workload(str(event["command"])),
                )
        for rollup in rollups:
            legacy_labels = rollup.pop("workloads", [])
            if not legacy_labels or rollup.get("uid") is None:
                continue
            workload_ids = [
                self.register_workload(int(rollup["uid"]), describe_workload(str(label)))
                for label in legacy_labels
            ]
            rollup["workload_ids"] = sorted(set(workload_ids))
            observed = float(rollup.get("active_observed_seconds", 0))
            rollup["workload_observed_seconds"] = {str(workload_id): observed for workload_id in workload_ids}
        self._remember_users([*events, *rollups])
        event_groups: Dict[date, List[dict]] = {}
        for item in events:
            if str(item.get("status", "")) == "system":
                continue
            event_groups.setdefault(parse_iso(str(item["timestamp"])).date(), []).append(encode_event(item))
        for day, records in event_groups.items():
            self._merge_plain_partition("events", day, records, _event_storage_key)
        rollup_groups: Dict[date, List[dict]] = {}
        for item in rollups:
            if item.get("uid") is None or str(item.get("status", "")) in {"idle", "system"}:
                continue
            rollup_groups.setdefault(parse_iso(str(item["window_start"])).date(), []).append(encode_rollup(item))
        for day, records in rollup_groups.items():
            self._merge_plain_partition("minute", day, records, _rollup_storage_key)
        digest = hashlib.sha256()
        for path in sources:
            digest.update(path.name.encode("utf-8"))
            digest.update(_file_digest(path).encode("ascii"))
        _atomic_write_json(
            marker,
            {
                "version": 1,
                "completed_at": to_iso(utc_now()),
                "source_digest": digest.hexdigest(),
                "source_files": _legacy_source_metadata(sources),
                "events": len(events),
                "rollups": len(rollups),
                "legacy_files_retained": True,
            },
            self.file_mode,
            self.migrations_dir,
            ".legacy-migration.",
            expected_gid=self._managed_gid(),
        )
        return report

    def storage_info(self, *, include_collector: bool = True) -> dict:
        meta = self._read_json(self.meta_path) if self.meta_path.exists() else None
        tiers = {}
        for tier in ("events", "minute", "five-minute", "ten-minute", "hourly", "daily"):
            paths = self._all_partition_paths(tier)
            tiers[tier] = {
                "partitions": len(paths),
                "bytes": sum(path.stat().st_size for path in paths),
                "oldest": str(min((_partition_day(path) for path in paths if _partition_day(path)), default="")),
                "newest": str(max((_partition_day(path) for path in paths if _partition_day(path)), default="")),
            }
        result = {
            "format": meta,
            "path": str(self.usage_dir),
            "tiers": tiers,
            "legacy": {
                "events": self.events_path.exists(),
                "rollups": self.rollups_path.exists(),
                "migrated": (self.migrations_dir / "legacy-v1.json").exists(),
                "changed_after_migration": (
                    (self.migrations_dir / "legacy-v1.json").exists() and self._legacy_visible()
                ),
            },
            "warnings": list(self.last_warnings),
        }
        if include_collector:
            result["collector"] = self.load_collector_status()
        return result

    def health_issues(self) -> List[dict]:
        issues = []
        if self.storage_gid is not None and os.path.lexists(self.data_dir):
            try:
                root_metadata = self.data_dir.lstat()
            except OSError as exc:
                issues.append(
                    {
                        "type": "usage-path-stat",
                        "path": str(self.data_dir),
                        "message": str(exc),
                    }
                )
            else:
                if (
                    stat.S_ISDIR(root_metadata.st_mode)
                    and root_metadata.st_gid != self.storage_gid
                ):
                    issues.append(
                        {
                            "type": "usage-directory-gid",
                            "path": str(self.data_dir),
                            "expected_gid": self.storage_gid,
                            "actual_gid": root_metadata.st_gid,
                            "message": "data directory does not match configured storage_gid",
                        }
                    )
        managed_gid = _setgid_directory_gid(self.data_dir, self.dir_mode)
        usage_issue, usage_dir_safe = _usage_path_health(
            self.usage_dir,
            "directory",
            self.dir_mode,
            expected_gid=managed_gid,
        )
        if usage_issue is not None:
            issues.append(usage_issue)
        usage_tree_safe = usage_dir_safe
        if usage_dir_safe:
            walk_errors = []
            for root, directories, files in os.walk(
                self.usage_dir,
                topdown=True,
                onerror=walk_errors.append,
                followlinks=False,
            ):
                root_path = Path(root)
                for name in directories:
                    issue, safe = _usage_path_health(
                        root_path / name,
                        "directory",
                        self.dir_mode,
                        expected_gid=managed_gid,
                    )
                    usage_tree_safe = usage_tree_safe and safe
                    if issue is not None:
                        issues.append(issue)
                for name in files:
                    path = root_path / name
                    expected_mode = 0o600 if path == self.key_path else self.file_mode
                    issue, safe = _usage_path_health(
                        path,
                        "regular-file",
                        expected_mode,
                        expected_gid=managed_gid,
                    )
                    usage_tree_safe = usage_tree_safe and safe
                    if issue is not None:
                        issues.append(issue)
            for exc in walk_errors:
                usage_tree_safe = False
                issues.append(
                    {
                        "type": "usage-path-scan",
                        "path": str(getattr(exc, "filename", self.usage_dir)),
                        "message": str(exc),
                    }
                )

        for path in (
            self.lock_path,
            self.legacy_state_path,
            self.events_path,
            self.rollups_path,
            self.legacy_load_path,
        ):
            issue, _safe = _usage_path_health(
                path,
                "regular-file",
                self.file_mode,
                expected_gid=managed_gid,
            )
            if issue is not None:
                issues.append(issue)

        if not usage_tree_safe:
            return issues
        if os.path.lexists(self.transition_journal_path):
            issues.append(
                {
                    "type": "usage-pending-journal",
                    "path": str(self.transition_journal_path),
                    "message": "the monitor will recover this state transition while holding usage.lock",
                }
            )
        if os.path.lexists(self.meta_path):
            try:
                self._validate_meta(self._read_json(self.meta_path))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                issues.append({"type": "usage-format", "path": str(self.meta_path), "message": str(exc)})
        if os.path.lexists(self.collector_path):
            try:
                validate_collector_document(self._read_json(self.collector_path))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                issues.append(
                    {
                        "type": "usage-collector-status",
                        "path": str(self.collector_path),
                        "message": str(exc),
                    }
                )
        if os.path.lexists(self.workloads_path) and not os.path.lexists(self.key_path):
            issues.append(
                {
                    "type": "usage-key-missing",
                    "path": str(self.key_path),
                    "message": "workload identities cannot continue safely",
                }
            )
        for tier in ("events", "minute", "five-minute", "ten-minute", "hourly", "daily"):
            try:
                paths = self._all_partition_paths(tier)
            except OSError as exc:
                issues.append({"type": "usage-tier", "path": str(self.usage_dir / tier), "message": str(exc)})
                continue
            for path in paths:
                if path.name.endswith(".gz") and not _meta_path_for(path).exists():
                    issues.append(
                        {
                            "type": "usage-partition-metadata",
                            "path": str(path),
                            "message": "closed partition is missing checksum metadata",
                        }
                    )
        return issues

    def clear_unlocked(self) -> dict:
        result = {
            "usage_events": _line_count(self.events_path),
            "usage_rollups": _line_count(self.rollups_path),
            "usage_state": int(self.legacy_state_path.exists() or self.state_path.exists()),
            "usage_load": int(self.legacy_load_path.exists() or self.load_path.exists()),
        }
        for tier in ("events", "minute", "five-minute", "ten-minute", "hourly", "daily"):
            result["usage_events" if tier == "events" else "usage_rollups"] += sum(
                _partition_record_count(path) for path in self._all_partition_paths(tier)
            )
        if self.usage_dir.exists():
            metadata = self.usage_dir.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError(f"refusing non-directory usage store: {self.usage_dir}")
            shutil.rmtree(self.usage_dir)
        for path in (self.events_path, self.rollups_path, self.legacy_state_path, self.legacy_load_path):
            path.unlink(missing_ok=True)
        fsync_directory(self.data_dir)
        self._users = None
        self._workloads_by_fingerprint = None
        self._workloads_by_id = None
        self._workload_key = None
        return result

    def _validate_meta(self, payload: dict) -> None:
        if payload.get("format") != STORE_FORMAT:
            raise UsageFormatError(f"unsupported usage store: {self.meta_path}")
        major = int(payload.get("format_major", -1))
        min_writer = int(payload.get("min_writer_major", major))
        min_writer_minor = int(payload.get("min_writer_minor", 0))
        if (
            major != STORE_FORMAT_MAJOR
            or min_writer > STORE_FORMAT_MAJOR
            or (min_writer == STORE_FORMAT_MAJOR and min_writer_minor > STORE_FORMAT_MINOR)
        ):
            raise UsageFormatError(
                f"usage store format {major} requires a newer gpubk; refusing to write"
            )

    def _check_readable(self) -> None:
        if not self.meta_path.exists():
            return
        payload = self._read_json(self.meta_path)
        if payload.get("format") != STORE_FORMAT:
            raise UsageFormatError(f"unsupported usage store: {self.meta_path}")
        min_reader = int(payload.get("min_reader_major", payload.get("format_major", -1)))
        min_reader_minor = int(payload.get("min_reader_minor", 0))
        if min_reader > STORE_FORMAT_MAJOR or (
            min_reader == STORE_FORMAT_MAJOR and min_reader_minor > STORE_FORMAT_MINOR
        ):
            raise UsageFormatError("usage store requires a newer gpubk reader")

    def _append_partition(self, tier: str, day: date, records: Sequence[dict]) -> int:
        path = self._partition_path(tier, day)
        self._ensure_usage_directory(path.parent)
        return self._append_jsonl(path, records)

    def _validate_append_target(self, tier: str, day: date) -> None:
        path = self._partition_path(tier, day)
        self._ensure_usage_directory(path.parent)
        closed = path.with_suffix(path.suffix + ".gz")
        metadata_path = _meta_path_for(closed)
        if os.path.lexists(closed) != os.path.lexists(metadata_path):
            raise UsageFormatError(
                f"closed usage partition and metadata must exist together: {closed}"
            )
        candidates = (path, closed, metadata_path)
        for candidate in candidates:
            if not os.path.lexists(candidate):
                continue
            fd = open_existing_regular(
                candidate,
                expected_mode=self.file_mode,
                expected_gid=self._managed_gid(),
            )
            os.close(fd)

    def _append_event_partition(self, day: date, records: Sequence[dict]) -> int:
        existing_ids = set()
        plain = self._partition_path("events", day)
        candidates = (plain, plain.with_suffix(plain.suffix + ".gz"))
        for path in candidates:
            if not path.exists():
                continue
            for item in self._read_jsonl(path):
                event_id = str(item.get("id", ""))
                if event_id:
                    existing_ids.add(event_id)
        filtered = []
        for record in records:
            event_id = str(record.get("id", ""))
            if event_id and event_id in existing_ids:
                continue
            filtered.append(record)
            if event_id:
                existing_ids.add(event_id)
        return self._append_partition("events", day, filtered)

    def _append_jsonl(self, path: Path, records: Sequence[dict]) -> int:
        if not records:
            return 0
        try:
            result = append_json_objects(
                path,
                records,
                file_mode=self.file_mode,
                dir_mode=self.dir_mode,
                max_line_bytes=MAX_USAGE_LINE_BYTES,
                max_file_bytes=(
                    MAX_DICTIONARY_BYTES
                    if path == self.workloads_path
                    else MAX_PARTITION_UNCOMPRESSED_BYTES
                ),
                record_name="usage",
                expected_gid=self._managed_gid(),
            )
        except JsonlFormatError as exc:
            raise UsageFormatError(str(exc)) from exc
        self.last_warnings.extend(result.warnings)
        return result.count

    def _merge_plain_partition(self, tier: str, day: date, records: Sequence[dict], key_fn) -> None:
        path = self._partition_path(tier, day)
        closed = path.with_suffix(path.suffix + ".gz")
        if closed.exists():
            raise UsageFormatError(f"cannot merge legacy data into an already closed partition: {closed}")
        existing = list(self._read_jsonl(path)) if path.exists() else []
        merged = {key_fn(item): item for item in existing}
        for record in records:
            merged.setdefault(key_fn(record), record)
        self._ensure_usage_directory(path.parent)
        _atomic_write_jsonl(
            path,
            list(merged.values()),
            self.file_mode,
            path.parent,
            expected_gid=self._managed_gid(),
        )

    def _partition_path(self, tier: str, day: date) -> Path:
        return self.usage_dir / tier / f"{day:%Y}" / f"{day:%m}" / f"{day.isoformat()}{PARTITION_SUFFIX}"

    def _partition_paths(
        self,
        tier: str,
        start: Optional[datetime],
        end: Optional[datetime],
        newest_first: bool,
    ) -> List[Path]:
        paths = self._all_partition_paths(tier)
        filtered = []
        for path in paths:
            day = _partition_day(path)
            if day is None:
                continue
            if start is not None and _day_end(day) <= start.astimezone(timezone.utc):
                continue
            if end is not None and _day_start(day) >= end.astimezone(timezone.utc):
                continue
            filtered.append(path)
        return sorted(filtered, reverse=newest_first)

    def _all_partition_paths(self, tier: str) -> List[Path]:
        root = self.usage_dir / tier
        if not os.path.lexists(root):
            return []
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise UsageFormatError(f"usage tier must be a real directory: {root}")
        return sorted(
            path
            for path in root.rglob("*")
            if _is_regular_path(path) and (path.name.endswith(".jsonl") or path.name.endswith(".jsonl.gz"))
        )

    def _plain_partition_paths(self, tier: str) -> List[Path]:
        return [path for path in self._all_partition_paths(tier) if path.name.endswith(".jsonl")]

    def _tier_dates(self, tier: str) -> List[date]:
        return sorted({_partition_day(path) for path in self._all_partition_paths(tier) if _partition_day(path)})

    def _partition_exists(self, tier: str, day: date) -> bool:
        path = self._partition_path(tier, day)
        return path.exists() or path.with_suffix(path.suffix + ".gz").exists()

    def _records_for_day(self, tier: str, day: date) -> tuple[List[dict], bool]:
        paths = [path for path in self._all_partition_paths(tier) if _partition_day(path) == day]
        records = []
        seen = set()
        compactable = True
        for path in paths:
            for raw in self._read_jsonl(path):
                if unknown_storage_fields(raw, "rollups"):
                    compactable = False
                if int(raw.get("v", ROLLUP_SCHEMA_VERSION)) != ROLLUP_SCHEMA_VERSION:
                    compactable = False
                    continue
                key = _rollup_storage_key(raw)
                if key in seen:
                    continue
                seen.add(key)
                records.append(decode_rollup(raw, self.username_for_uid))
        return records, compactable

    def _write_closed_partition(self, tier: str, day: date, records: Sequence[dict]) -> Path:
        plain = self._partition_path(tier, day)
        target = plain.with_suffix(plain.suffix + ".gz")
        self._ensure_usage_directory(target.parent)
        if target.exists() and _meta_path_for(target).exists():
            for existing in (target, _meta_path_for(target)):
                fd = open_existing_regular(
                    existing,
                    expected_mode=self.file_mode,
                    expected_gid=self._managed_gid(),
                )
                os.close(fd)
            return target
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        tmp_path = Path(tmp_name)
        digest = hashlib.sha256()
        count = 0
        uncompressed_bytes = 0
        try:
            os.fchmod(fd, self.file_mode)
            actual_gid = os.fstat(fd).st_gid
            expected_gid = self._managed_gid()
            if expected_gid is not None and actual_gid != expected_gid:
                raise PermissionError(
                    errno.EPERM,
                    f"refusing temporary partition with GID {actual_gid}; "
                    f"expected {expected_gid}: {tmp_path}",
                )
            raw = os.fdopen(fd, "wb")
            fd = -1
            with raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
                for record in records:
                    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
                    if len(line) > MAX_USAGE_LINE_BYTES:
                        raise ValueError("usage record exceeds the 1 MiB limit")
                    uncompressed_bytes += len(line)
                    if uncompressed_bytes > MAX_PARTITION_UNCOMPRESSED_BYTES:
                        raise UsageFormatError("usage partition exceeds the 512 MiB safety limit")
                    digest.update(line)
                    compressed.write(line)
                    count += 1
            _verify_gzip(tmp_path, digest.hexdigest(), count)
            os.replace(tmp_path, target)
            fsync_directory(target.parent)
            _atomic_write_json(
                _meta_path_for(target),
                {
                    "format": STORE_FORMAT,
                    "record_type": tier,
                    "schema_major": 1,
                    "schema_minor": 0,
                    "codec": "jsonl+gzip",
                    "day": day.isoformat(),
                    "record_count": count,
                    "uncompressed_sha256": digest.hexdigest(),
                },
                self.file_mode,
                target.parent,
                ".partition-meta.",
                expected_gid=self._managed_gid(),
            )
            return target
        finally:
            if fd >= 0:
                os.close(fd)
            tmp_path.unlink(missing_ok=True)

    def _seal_partition(self, path: Path) -> None:
        if not path.name.endswith(".jsonl"):
            return
        self._ensure_usage_directory(path.parent)
        source_fd = open_existing_regular(
            path,
            expected_mode=self.file_mode,
            expected_gid=self._managed_gid(),
        )
        os.close(source_fd)
        target = path.with_suffix(path.suffix + ".gz")
        if target.exists() and _meta_path_for(target).exists():
            metadata = self._read_json(_meta_path_for(target))
            source_digest, source_count = _plain_partition_digest(path)
            if (
                hmac.compare_digest(str(metadata.get("uncompressed_sha256", "")), source_digest)
                and int(metadata.get("record_count", -1)) == source_count
            ):
                path.unlink(missing_ok=True)
                fsync_directory(path.parent)
                return
            self.last_warnings.append(f"both open and closed partitions contain different data: {path}")
            return
        raw_records = list(self._read_jsonl(path))
        self._write_closed_partition(_tier_from_path(self.usage_dir, path), _partition_day(path), raw_records)
        path.unlink(missing_ok=True)
        fsync_directory(path.parent)

    def _safe_to_remove_tier(self, tier: str, day: date) -> bool:
        if tier == "minute":
            return all(
                self._valid_partition_exists(target, day)
                for target in ("five-minute", "ten-minute", "hourly", "daily")
            )
        if tier == "five-minute":
            return all(
                self._valid_partition_exists(target, day)
                for target in ("ten-minute", "hourly", "daily")
            )
        if tier == "ten-minute":
            return all(self._valid_partition_exists(target, day) for target in ("hourly", "daily"))
        if tier == "hourly":
            return self._valid_partition_exists("daily", day)
        return True

    def _valid_partition_exists(self, tier: str, day: date) -> bool:
        plain = self._partition_path(tier, day)
        if plain.exists():
            return _is_regular_path(plain)
        closed = plain.with_suffix(plain.suffix + ".gz")
        if not closed.exists():
            return False
        try:
            _verify_closed_partition(closed)
            return True
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.last_warnings.append(f"invalid derived partition {closed}: {exc}")
            return False

    def _read_jsonl(self, path: Path) -> Iterator[dict]:
        if not path.exists():
            return
        fd = open_existing_regular(path)
        raw = os.fdopen(fd, "rb")
        compressed = path.name.endswith(".gz")
        try:
            if compressed:
                metadata = self._partition_metadata(path)
                digest, line_count = self._scan_closed_stream(raw, path)
                self._verify_partition_metadata(path, metadata, digest, line_count)
                raw.seek(0)
            elif os.fstat(fd).st_size > MAX_PARTITION_UNCOMPRESSED_BYTES:
                raise UsageFormatError(f"usage partition exceeds the 512 MiB safety limit: {path}")
            yield from self._iter_jsonl_records(raw, path, compressed=compressed)
        finally:
            if not raw.closed:
                raw.close()

    def _ordered_candidates(
        self,
        path: Path,
        *,
        newest_first: bool,
        max_records: Optional[int],
        seen: set,
        decoder: Callable[[dict], Optional[Tuple[tuple, dict]]],
    ) -> Iterator[Tuple[tuple, dict]]:
        raw_records = self._read_jsonl(path)
        try:
            if not newest_first:
                for raw in raw_records:
                    candidate = decoder(raw)
                    if candidate is None or candidate[0] in seen:
                        continue
                    yield candidate
                return

            latest = {}
            for raw in raw_records:
                candidate = decoder(raw)
                if candidate is None or candidate[0] in seen:
                    continue
                key, record = candidate
                latest.pop(key, None)
                latest[key] = record
                if max_records is not None and len(latest) > max_records:
                    del latest[next(iter(latest))]
            for key in reversed(latest):
                yield key, latest[key]
        finally:
            close = getattr(raw_records, "close", None)
            if close is not None:
                close()

    def _event_candidate(
        self,
        raw: dict,
        path: Path,
        *,
        start: Optional[datetime],
        end: Optional[datetime],
        uid: Optional[int],
        gpu: Optional[int],
        versioned: bool,
    ) -> Optional[Tuple[tuple, dict]]:
        try:
            if versioned and int(raw.get("v", EVENT_SCHEMA_VERSION)) > EVENT_SCHEMA_VERSION:
                self.last_warnings.append(f"skipped newer event schema in {path}")
                return None
            record = decode_event(raw, self.username_for_uid)
            timestamp = parse_iso(str(record["timestamp"]))
        except (KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
            if versioned:
                self.last_warnings.append(f"skipped invalid event in {path}: {exc}")
            return None
        if not _in_range(timestamp, start, end) or (uid is not None and record.get("uid") != uid):
            return None
        if gpu is not None and record.get("gpu") != gpu:
            return None
        return _public_event_key(record), record

    def _rollup_candidate(
        self,
        raw: dict,
        path: Path,
        *,
        start: Optional[datetime],
        end: Optional[datetime],
        uid: Optional[int],
        gpu: Optional[int],
        versioned: bool,
    ) -> Optional[Tuple[tuple, dict]]:
        try:
            if versioned and int(raw.get("v", ROLLUP_SCHEMA_VERSION)) > ROLLUP_SCHEMA_VERSION:
                self.last_warnings.append(f"skipped newer rollup schema in {path}")
                return None
            record = decode_rollup(raw, self.username_for_uid)
            timestamp = parse_iso(str(record["window_start"]))
        except (KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
            if versioned:
                self.last_warnings.append(f"skipped invalid rollup in {path}: {exc}")
            return None
        if not _in_range(timestamp, start, end) or (uid is not None and record.get("uid") != uid):
            return None
        if gpu is not None and record.get("gpu") != gpu:
            return None
        return _public_rollup_key(record), record

    def _scan_closed_stream(self, raw, path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        line_count = 0
        uncompressed_bytes = 0
        with gzip.GzipFile(fileobj=raw, mode="rb") as stream:
            for raw_line in stream:
                uncompressed_bytes += len(raw_line)
                if uncompressed_bytes > MAX_PARTITION_UNCOMPRESSED_BYTES:
                    raise UsageFormatError(f"usage partition exceeds the 512 MiB safety limit: {path}")
                if len(raw_line) > MAX_USAGE_LINE_BYTES:
                    continue
                digest.update(raw_line)
                line_count += 1
        return digest.hexdigest(), line_count

    def _iter_jsonl_records(self, raw, path: Path, *, compressed: bool) -> Iterator[dict]:
        uncompressed_bytes = 0
        stream = gzip.GzipFile(fileobj=raw, mode="rb") if compressed else raw
        with stream:
            for raw_line in stream:
                uncompressed_bytes += len(raw_line)
                if uncompressed_bytes > MAX_PARTITION_UNCOMPRESSED_BYTES:
                    raise UsageFormatError(f"usage partition exceeds the 512 MiB safety limit: {path}")
                if len(raw_line) > MAX_USAGE_LINE_BYTES:
                    self.last_warnings.append(f"skipped oversized usage record in {path}")
                    continue
                try:
                    value = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.last_warnings.append(f"skipped malformed usage record in {path}")
                    continue
                if isinstance(value, dict):
                    yield value

    def _partition_metadata(self, path: Path) -> dict:
        meta_path = _meta_path_for(path)
        if not meta_path.exists():
            raise UsageFormatError(f"closed usage partition has no metadata: {path}")
        return self._read_json(meta_path)

    def _verify_partition_metadata(
        self,
        path: Path,
        metadata: dict,
        digest: str,
        line_count: int,
    ) -> None:
        meta_path = _meta_path_for(path)
        if metadata.get("format") != STORE_FORMAT or int(metadata.get("schema_major", -1)) != 1:
            raise UsageFormatError(f"unsupported partition metadata: {meta_path}")
        if int(metadata.get("record_count", -1)) != line_count:
            raise UsageFormatError(f"usage partition record count mismatch: {path}")
        if not hmac.compare_digest(str(metadata.get("uncompressed_sha256", "")), digest):
            raise UsageFormatError(f"usage partition checksum mismatch: {path}")

    def _read_json(self, path: Path) -> dict:
        fd = open_existing_regular(path)
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            value = json.load(fh)
        if not isinstance(value, dict):
            raise ValueError(f"{path} must contain an object")
        return value

    def _load_users(self) -> dict:
        if self._users is not None:
            return self._users
        if not self.users_path.exists():
            self._users = {"version": 1, "users": {}}
            return self._users
        try:
            value = self._read_json(self.users_path)
            if value.get("version") != 1 or not isinstance(value.get("users"), dict):
                raise ValueError("invalid users dictionary")
            self._users = value
        except (OSError, ValueError, json.JSONDecodeError):
            self._users = {"version": 1, "users": {}}
        return self._users

    def _recover_state_transition(self) -> None:
        try:
            journal = self._read_json(self.transition_journal_path)
            if journal.get("version") != 1:
                raise ValueError("unsupported state transition journal")
            events = journal.get("events")
            processes = journal.get("processes")
            if not isinstance(events, list) or not isinstance(processes, dict):
                raise ValueError("invalid state transition journal")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise UsageFormatError(f"cannot recover usage state transition: {exc}") from exc
        self.append_events(item for item in events if isinstance(item, dict))
        self.save_state(processes)
        self.transition_journal_path.unlink(missing_ok=True)
        fsync_directory(self.usage_dir)

    def _remember_users(self, records: Sequence[dict]) -> None:
        users = self._load_users()
        changed = False
        for record in records:
            uid = record.get("uid")
            username = str(record.get("username", "")).strip()
            if uid is None or not username:
                continue
            key = str(int(uid))
            current = users["users"].get(key)
            if current is None or current.get("username") != username:
                users["users"][key] = {"username": username[:128], "updated_at": to_iso(utc_now())}
                changed = True
        if changed:
            _atomic_write_json(
                self.users_path,
                users,
                self.file_mode,
                self.usage_dir,
                ".users.",
                expected_gid=self._managed_gid(),
            )

    def _load_workload_registry(self) -> None:
        if self._workloads_by_id is not None and self._workloads_by_fingerprint is not None:
            return
        by_id: Dict[int, dict] = {}
        by_fingerprint: Dict[str, int] = {}
        if self.workloads_path.exists():
            metadata = self.workloads_path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise UsageFormatError("workload dictionary must be a regular file")
            if metadata.st_size > MAX_DICTIONARY_BYTES:
                raise UsageFormatError("workload dictionary exceeds the 64 MiB safety limit")
            for record in self._read_jsonl(self.workloads_path):
                try:
                    if int(record.get("v", 1)) > 1:
                        self.last_warnings.append("skipped a newer workload dictionary record")
                        continue
                    workload_id = int(record["id"])
                    fingerprint = str(record["fp"])
                except (KeyError, TypeError, ValueError):
                    continue
                by_id[workload_id] = record
                by_fingerprint[fingerprint] = workload_id
        self._workloads_by_id = by_id
        self._workloads_by_fingerprint = by_fingerprint

    def _workload_registry(self) -> Tuple[Dict[str, int], Dict[int, dict]]:
        self._load_workload_registry()
        by_fingerprint = self._workloads_by_fingerprint
        by_id = self._workloads_by_id
        if by_fingerprint is None or by_id is None:
            raise UsageFormatError("workload registry initialization failed")
        return by_fingerprint, by_id

    def _load_or_create_workload_key(self) -> bytes:
        if self._workload_key is not None:
            return self._workload_key
        if self.key_path.exists():
            fd = open_existing_regular(
                self.key_path,
                expected_mode=0o600,
                expected_gid=self._managed_gid(),
            )
            with os.fdopen(fd, "rb") as fh:
                key = fh.read(64)
            if len(key) != 32:
                raise UsageFormatError("invalid workload HMAC key")
            self._workload_key = key
            return key
        if self.workloads_path.exists() and self.workloads_path.lstat().st_size:
            raise UsageFormatError("workload HMAC key is missing; refusing to fork workload identities")
        key = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(self.key_path), flags, 0o600)
        created = True
        try:
            os.fchmod(fd, 0o600)
            actual_gid = os.fstat(fd).st_gid
            expected_gid = self._managed_gid()
            if expected_gid is not None and actual_gid != expected_gid:
                raise PermissionError(
                    errno.EPERM,
                    f"refusing workload key with GID {actual_gid}; expected {expected_gid}: "
                    f"{self.key_path}",
                )
            if os.write(fd, key) != len(key):
                raise OSError(errno.EIO, "short write while creating workload HMAC key")
            os.fsync(fd)
            created = False
        finally:
            os.close(fd)
            if created:
                self.key_path.unlink(missing_ok=True)
        fsync_directory(self.usage_dir)
        self._workload_key = key
        return key

    def _validate_core_files_for_write(self, expected_gid: Optional[int]) -> None:
        expected_modes = {
            self.meta_path: self.file_mode,
            self.state_path: self.file_mode,
            self.transition_journal_path: self.file_mode,
            self.load_path: self.file_mode,
            self.collector_path: self.file_mode,
            self.users_path: self.file_mode,
            self.workloads_path: self.file_mode,
            self.key_path: 0o600,
        }
        for path, expected_mode in expected_modes.items():
            if not os.path.lexists(path):
                continue
            fd = open_existing_regular(
                path,
                expected_mode=expected_mode,
                expected_gid=expected_gid,
            )
            os.close(fd)
        for path in (
            self.legacy_state_path,
            self.events_path,
            self.rollups_path,
            self.legacy_load_path,
        ):
            if not os.path.lexists(path):
                continue
            fd = open_existing_regular(path, expected_gid=expected_gid)
            os.close(fd)

    def _validate_usage_tree_for_write(self) -> None:
        unsafe_types = {
            "usage-directory-type",
            "usage-directory-mode",
            "usage-directory-gid",
            "usage-file-type",
            "usage-file-mode",
            "usage-file-gid",
            "usage-file-links",
            "usage-path-stat",
            "usage-path-scan",
        }
        legacy_paths = {
            str(self.legacy_state_path),
            str(self.events_path),
            str(self.rollups_path),
            str(self.legacy_load_path),
        }
        issue = next(
            (
                item
                for item in self.health_issues()
                if item.get("type") in unsafe_types
                and str(item.get("path", "")) not in legacy_paths
            ),
            None,
        )
        if issue is not None:
            raise UsageFormatError(
                f"refusing usage maintenance with {issue['type']} at "
                f"{issue.get('path', self.usage_dir)}"
            )

    def _ensure_usage_directory(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.usage_dir)
        except ValueError as exc:
            raise UsageFormatError(f"usage path escapes the store: {path}") from exc
        expected_gid = self._managed_gid()
        ensure_directory(
            self.usage_dir,
            self.dir_mode,
            require_mode=True,
            expected_gid=expected_gid,
        )
        current = self.usage_dir
        for part in relative.parts:
            current /= part
            ensure_directory(
                current,
                self.dir_mode,
                require_mode=True,
                expected_gid=expected_gid,
            )

    def _legacy_visible(self) -> bool:
        marker = self.migrations_dir / "legacy-v1.json"
        if not marker.exists():
            return True
        try:
            payload = self._read_json(marker)
        except (OSError, ValueError, json.JSONDecodeError):
            return True
        sources = [path for path in (self.events_path, self.rollups_path) if path.exists()]
        return _legacy_sources_changed(payload, sources)


# Public name for new integrations; the old name remains import-compatible.
VersionedUsageStore = UsageAuditStore


def _resolution_for_tier(tier: str) -> int:
    for seconds, value in TIER_FOR_RESOLUTION.items():
        if value == tier:
            return seconds
    raise ValueError(f"unknown usage tier: {tier}")


def _partition_day(path: Path) -> Optional[date]:
    try:
        return date.fromisoformat(path.name[:10])
    except ValueError:
        return None


def _tier_from_path(root: Path, path: Path) -> str:
    return path.relative_to(root).parts[0]


def _day_start(day: date) -> datetime:
    return datetime.combine(day, datetime_time.min, timezone.utc)


def _day_end(day: date) -> datetime:
    return _day_start(day) + timedelta(days=1)


def _in_range(value: datetime, start: Optional[datetime], end: Optional[datetime]) -> bool:
    normalized = value.astimezone(timezone.utc)
    if start is not None and normalized < start.astimezone(timezone.utc):
        return False
    return end is None or normalized <= end.astimezone(timezone.utc)


def _atomic_write_json(
    path: Path,
    payload: dict,
    mode: int,
    directory: Path,
    prefix: str,
    *,
    expected_gid: Optional[int] = None,
) -> None:
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"refusing non-directory path: {directory}")
    if expected_gid is not None and metadata.st_gid != expected_gid:
        raise PermissionError(
            errno.EPERM,
            f"refusing directory with GID {metadata.st_gid}; expected {expected_gid}: {directory}",
        )
    if os.path.lexists(path):
        existing_fd = open_existing_regular(
            path,
            expected_mode=mode,
            expected_gid=expected_gid,
        )
        os.close(existing_fd)
    fd, tmp_name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        actual_gid = os.fstat(fd).st_gid
        if expected_gid is not None and actual_gid != expected_gid:
            raise PermissionError(
                errno.EPERM,
                f"refusing temporary file with GID {actual_gid}; expected {expected_gid}: "
                f"{tmp_path}",
            )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        fsync_directory(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)


def _atomic_write_jsonl(
    path: Path,
    records: Sequence[dict],
    mode: int,
    directory: Path,
    *,
    expected_gid: Optional[int] = None,
) -> None:
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"refusing non-directory path: {directory}")
    if expected_gid is not None and metadata.st_gid != expected_gid:
        raise PermissionError(
            errno.EPERM,
            f"refusing directory with GID {metadata.st_gid}; expected {expected_gid}: {directory}",
        )
    if os.path.lexists(path):
        existing_fd = open_existing_regular(
            path,
            expected_mode=mode,
            expected_gid=expected_gid,
        )
        os.close(existing_fd)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        actual_gid = os.fstat(fd).st_gid
        if expected_gid is not None and actual_gid != expected_gid:
            raise PermissionError(
                errno.EPERM,
                f"refusing temporary file with GID {actual_gid}; expected {expected_gid}: "
                f"{tmp_path}",
            )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            total_bytes = 0
            for record in records:
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
                line_bytes = len(line.encode("utf-8"))
                if line_bytes > MAX_USAGE_LINE_BYTES:
                    raise ValueError("usage record exceeds the 1 MiB limit")
                total_bytes += line_bytes
                if total_bytes > MAX_PARTITION_UNCOMPRESSED_BYTES:
                    raise UsageFormatError("usage partition exceeds the 512 MiB safety limit")
                fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        fsync_directory(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)


def _is_regular_path(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _event_storage_key(record: dict) -> tuple:
    event_id = str(record.get("id", ""))
    if event_id:
        return ("id", event_id)
    return (
        "event",
        record.get("e"),
        record.get("t"),
        record.get("g"),
        record.get("p"),
        record.get("u"),
    )


def _rollup_storage_key(record: dict) -> tuple:
    record_id = str(record.get("id", ""))
    if record_id:
        return ("id", record_id)
    return (
        record.get("t"),
        record.get("d"),
        record.get("g"),
        record.get("u"),
        record.get("s"),
        tuple(record.get("r", [])),
    )


def _public_event_key(record: dict) -> tuple:
    event_id = str(record.get("event_id", ""))
    if event_id:
        return ("id", event_id)
    return (
        "event",
        record.get("event"),
        record.get("timestamp"),
        record.get("gpu"),
        record.get("pid"),
        record.get("uid"),
    )


def _public_rollup_key(record: dict) -> tuple:
    record_id = str(record.get("record_id", ""))
    if record_id:
        return ("id", record_id)
    return (
        record.get("window_start"),
        record.get("window_end"),
        record.get("gpu"),
        record.get("uid"),
        record.get("status"),
        tuple(record.get("reservation_ids", [])),
    )


def _verify_gzip(path: Path, expected_digest: str, expected_count: int) -> None:
    digest = hashlib.sha256()
    count = 0
    with gzip.open(path, "rb") as fh:
        for line in fh:
            digest.update(line)
            count += 1
    if digest.hexdigest() != expected_digest or count != expected_count:
        raise OSError(f"closed usage partition failed verification: {path}")


def _verify_closed_partition(path: Path) -> None:
    meta_path = _meta_path_for(path)
    fd = open_existing_regular(meta_path)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        metadata = json.load(fh)
    if not isinstance(metadata, dict) or metadata.get("format") != STORE_FORMAT:
        raise UsageFormatError(f"invalid partition metadata: {meta_path}")
    _verify_gzip(
        path,
        str(metadata.get("uncompressed_sha256", "")),
        int(metadata.get("record_count", -1)),
    )


def _plain_partition_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    fd = open_existing_regular(path)
    with os.fdopen(fd, "rb") as fh:
        for line in fh:
            digest.update(line)
            count += 1
    return digest.hexdigest(), count


def _meta_path_for(path: Path) -> Path:
    return path.with_name(path.name + ".meta.json")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    fd = open_existing_regular(path)
    with os.fdopen(fd, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _legacy_source_metadata(paths: Sequence[Path]) -> dict:
    result = {}
    for path in paths:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise UsageFormatError(f"legacy usage source must be a regular file: {path}")
        result[path.name] = {"size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns}
    return result


def _legacy_sources_changed(marker: dict, paths: Sequence[Path]) -> bool:
    if not paths:
        return False
    expected = marker.get("source_files")
    if not isinstance(expected, dict):
        return bool(paths)
    try:
        return expected != _legacy_source_metadata(paths)
    except OSError:
        return True


def _partition_record_count(path: Path) -> int:
    meta = _meta_path_for(path)
    if meta.exists():
        try:
            fd = open_existing_regular(meta)
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                return max(0, int(json.load(fh).get("record_count", 0)))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if path.name.endswith(".gz"):
        fd = open_existing_regular(path)
        raw = os.fdopen(fd, "rb")
        try:
            with gzip.GzipFile(fileobj=raw, mode="rb") as fh:
                return sum(1 for _line in fh)
        finally:
            if not raw.closed:
                raw.close()
    return _line_count(path)


def _usage_path_health(
    path: Path,
    expected_type: str,
    expected_mode: int,
    *,
    expected_gid: Optional[int] = None,
) -> Tuple[Optional[dict], bool]:
    if not os.path.lexists(path):
        return None, False
    try:
        metadata = path.lstat()
    except OSError as exc:
        return (
            {
                "type": "usage-path-stat",
                "path": str(path),
                "message": str(exc),
            },
            False,
        )
    actual_type = file_type_name(metadata.st_mode)
    if actual_type != expected_type:
        return (
            {
                "type": "usage-directory-type"
                if expected_type == "directory"
                else "usage-file-type",
                "path": str(path),
                "expected": expected_type,
                "actual": actual_type,
            },
            False,
        )
    if expected_type == "regular-file" and metadata.st_nlink != 1:
        return (
            {
                "type": "usage-file-links",
                "path": str(path),
                "expected": 1,
                "actual": metadata.st_nlink,
            },
            False,
        )
    mode_mask = 0o7777 if expected_type == "directory" else 0o777
    actual_mode = metadata.st_mode & mode_mask
    if actual_mode != expected_mode:
        return (
            {
                "type": "usage-directory-mode"
                if expected_type == "directory"
                else "usage-file-mode",
                "path": str(path),
                "expected": f"{expected_mode:04o}",
                "actual": f"{actual_mode:04o}",
            },
            True,
        )
    if expected_gid is not None and metadata.st_gid != expected_gid:
        return (
            {
                "type": "usage-directory-gid"
                if expected_type == "directory"
                else "usage-file-gid",
                "path": str(path),
                "expected_gid": expected_gid,
                "actual_gid": metadata.st_gid,
                "message": "path did not inherit the setgid data-directory group",
            },
            True,
        )
    return None, True


def _setgid_directory_gid(path: Path, dir_mode: int) -> Optional[int]:
    if not dir_mode & stat.S_ISGID or not os.path.lexists(path):
        return None
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(metadata.st_mode):
        return None
    return metadata.st_gid


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    fd = open_existing_regular(path)
    with os.fdopen(fd, "rb") as fh:
        return sum(1 for _line in fh)


def _recent_plain_jsonl(path: Path, limit: int) -> List[dict]:
    newest = []
    for line in _reverse_lines(path):
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(value, dict):
            newest.append(value)
            if len(newest) >= limit:
                break
    return list(reversed(newest))


def _reverse_lines(path: Path, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    fd = open_existing_regular(path)
    with os.fdopen(fd, "rb") as fh:
        position = fh.seek(0, os.SEEK_END)
        remainder = b""
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            fh.seek(position)
            parts = (fh.read(read_size) + remainder).split(b"\n")
            if position > 0:
                remainder = parts.pop(0)
            else:
                remainder = b""
            for line in reversed(parts):
                if line:
                    yield line
