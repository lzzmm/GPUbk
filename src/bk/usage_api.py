from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Config
from .node_identity import record_node_id, stable_node_identity
from .timeparse import parse_iso, to_iso, utc_now
from .usage_schema import (
    RESOLUTIONS,
    TIER_FOR_RESOLUTION,
    USAGE_API_VERSION,
    aggregate_rollups,
    parse_resolution,
    resolution_label,
)
from .usage_store import UsageAuditStore, UsageRetentionPolicy


MAX_QUERY_RECORDS = 200_000


class UsageQueryService:
    """Read-only API consumed by the CLI, TUI, MCP, or another visualizer."""

    def __init__(self, config: Config, store: Optional[UsageAuditStore] = None):
        self.config = config
        self.store = store or UsageAuditStore(
            config.data_dir,
            config.lock_timeout_seconds,
            config.file_mode,
            config.dir_mode,
            config.storage_gid,
        )

    def samples(
        self,
        *,
        start: datetime,
        end: Optional[datetime] = None,
        resolution: str = "auto",
        uid: Optional[int] = None,
        gpu: Optional[int] = None,
        limit: int = 10_000,
        include_workloads: bool = True,
        redact_labels: bool = False,
    ) -> dict:
        normalized_start, normalized_end = _normalize_range(start, end)
        generated_at = utc_now()
        self.store.last_warnings = []
        _validate_limit(limit)
        resolution_seconds = (
            auto_resolution(normalized_start, normalized_end)
            if resolution == "auto"
            else parse_resolution(resolution)
        )
        records, truncated = self._records(
            normalized_start,
            normalized_end,
            resolution_seconds,
            uid,
            gpu,
            min(limit, MAX_QUERY_RECORDS),
        )
        workload_map = self.store.workloads() if include_workloads else {}
        public_records = [
            _public_rollup(record, workload_map, include_workloads, redact_labels)
            for record in records
        ]
        return {
            "schema_version": USAGE_API_VERSION,
            "kind": "usage-samples",
            "generated_at": to_iso(generated_at),
            "node": stable_node_identity(),
            "collector": self.store.load_collector_status(
                now=generated_at,
                expected_gpu_count=self.config.gpu_count,
            ),
            "query": {
                "start_at": to_iso(normalized_start),
                "end_at": to_iso(normalized_end),
                "resolution": resolution_label(resolution_seconds),
                "resolution_seconds": resolution_seconds,
                "uid": uid,
                "gpu": gpu,
                "limit": limit,
            },
            "coverage": _sample_coverage(records, normalized_start, normalized_end),
            "records": public_records,
            "truncated": truncated,
            "warnings": list(dict.fromkeys(self.store.last_warnings)),
        }

    def events(
        self,
        *,
        start: datetime,
        end: Optional[datetime] = None,
        uid: Optional[int] = None,
        gpu: Optional[int] = None,
        limit: int = 1000,
        include_workloads: bool = True,
        redact_labels: bool = False,
    ) -> dict:
        normalized_start, normalized_end = _normalize_range(start, end)
        generated_at = utc_now()
        self.store.last_warnings = []
        _validate_limit(limit)
        records = list(
            self.store.iter_events(
                start=normalized_start,
                end=normalized_end,
                uid=uid,
                gpu=gpu,
                limit=min(limit + 1, MAX_QUERY_RECORDS + 1),
            )
        )
        truncated = len(records) > limit
        records = records[:limit]
        workload_map = self.store.workloads() if include_workloads else {}
        public_records = [
            _public_event(record, workload_map, include_workloads, redact_labels)
            for record in records
        ]
        return {
            "schema_version": USAGE_API_VERSION,
            "kind": "usage-events",
            "generated_at": to_iso(generated_at),
            "node": stable_node_identity(),
            "collector": self.store.load_collector_status(
                now=generated_at,
                expected_gpu_count=self.config.gpu_count,
            ),
            "query": {
                "start_at": to_iso(normalized_start),
                "end_at": to_iso(normalized_end),
                "uid": uid,
                "gpu": gpu,
                "limit": limit,
            },
            "records": public_records,
            "truncated": truncated,
            "warnings": list(dict.fromkeys(self.store.last_warnings)),
        }

    def users(
        self,
        *,
        start: datetime,
        end: Optional[datetime] = None,
        resolution: str = "auto",
        uid: Optional[int] = None,
        limit: int = 1000,
        redact_labels: bool = False,
    ) -> dict:
        normalized_start, normalized_end = _normalize_range(start, end)
        generated_at = utc_now()
        self.store.last_warnings = []
        _validate_limit(limit)
        resolution_seconds = (
            auto_resolution(normalized_start, normalized_end)
            if resolution == "auto"
            else parse_resolution(resolution)
        )
        records, truncated = self._records(
            normalized_start,
            normalized_end,
            resolution_seconds,
            uid,
            None,
            MAX_QUERY_RECORDS,
        )
        workloads = self.store.workloads()
        summaries = _summarize_users(records, workloads, redact_labels)
        if len(summaries) > limit:
            summaries = summaries[:limit]
            truncated = True
        coverage = _sample_coverage(records, normalized_start, normalized_end)
        if uid is not None and not records:
            coverage.update(
                {
                    "store_has_samples": self._has_any_rollup(normalized_start, normalized_end),
                    "matching_process_event": next(
                        self.store.iter_events(
                            start=normalized_start,
                            end=normalized_end,
                            uid=uid,
                            limit=1,
                            newest_first=True,
                        ),
                        None,
                    )
                    is not None,
                }
            )
        return {
            "schema_version": USAGE_API_VERSION,
            "kind": "usage-users",
            "generated_at": to_iso(generated_at),
            "node": stable_node_identity(),
            "collector": self.store.load_collector_status(
                now=generated_at,
                expected_gpu_count=self.config.gpu_count,
            ),
            "query": {
                "start_at": to_iso(normalized_start),
                "end_at": to_iso(normalized_end),
                "resolution": resolution_label(resolution_seconds),
                "resolution_seconds": resolution_seconds,
                "uid": uid,
                "limit": limit,
            },
            "coverage": coverage,
            "users": summaries,
            "truncated": truncated,
            "warnings": list(dict.fromkeys(self.store.last_warnings)),
            "notes": [
                "SM and process memory are attributed from per-process NVML samples.",
                "Whole-device utilization is intentionally not divided between shared users.",
                "Missing history means not sampled; it is never interpreted as zero utilization.",
            ],
        }

    def _has_any_rollup(self, start: datetime, end: datetime) -> bool:
        for tier in dict.fromkeys(TIER_FOR_RESOLUTION.values()):
            if next(
                self.store.iter_rollups(
                    tier,
                    start=start,
                    end=end,
                    limit=1,
                    newest_first=True,
                ),
                None,
            ) is not None:
                return True
        return False

    def capabilities(self) -> dict:
        policy = UsageRetentionPolicy.from_config(self.config)
        generated_at = utc_now()
        storage = self.store.storage_info(include_collector=False)
        return {
            "schema_version": USAGE_API_VERSION,
            "kind": "usage-capabilities",
            "generated_at": to_iso(generated_at),
            "node": stable_node_identity(),
            "collector": self.store.load_collector_status(
                now=generated_at,
                expected_gpu_count=self.config.gpu_count,
            ),
            "ingest_schema": "gpubk.telemetry.v1",
            "storage_format": "gpubk.usage/1",
            "resolutions": dict(RESOLUTIONS),
            "retention": {
                "load_minutes": policy.load_minutes,
                "minute_days": policy.minute_days,
                "five_minute_days": policy.five_minute_days,
                "ten_minute_days": policy.ten_minute_days,
                "hourly_days": policy.hourly_days,
                "daily_days": policy.daily_days,
                "event_days": policy.event_days,
                "zero_days_means": "unlimited",
            },
            "interfaces": {
                "python": "bk.usage_api.UsageQueryService",
                "writer_protocol": "bk.telemetry.TelemetrySink",
                "collector_status_protocol": "bk.telemetry.CollectorStatusSink",
                "json_cli": "bk usage ... --json",
                "mcp": "get_my_gpu_usage",
                "cluster_history_schema": "gpubk.cluster-history.v1",
            },
            "topology": {
                "current_node": stable_node_identity(),
                "record_extension": "gpubk.node",
                "legacy_records_node": "legacy",
                "multi_node_scheduling": False,
                "cross_node_identity_mapping": False,
                "federated_cluster_client": True,
                "immutable_history_archive": True,
            },
            "writer_policy": {
                "configured_uid": self.config.monitor_uid,
                "role_required": bool(self.config.dir_mode & 0o022),
                "root_owned_config_required": bool(self.config.dir_mode & 0o022),
            },
            "durability": {
                "single_writer_lock": True,
                "append_batch_rollback": True,
                "interrupted_tail_repair": True,
                "closed_partition_checksums": True,
            },
            "storage": storage,
        }

    def _records(
        self,
        start: datetime,
        end: datetime,
        resolution_seconds: int,
        uid: Optional[int],
        gpu: Optional[int],
        limit: int,
    ) -> tuple[List[dict], bool]:
        target_tier = TIER_FOR_RESOLUTION[resolution_seconds]
        selected = list(
            self.store.iter_rollups(
                target_tier,
                start=start,
                end=end,
                uid=uid,
                gpu=gpu,
                limit=MAX_QUERY_RECORDS + 1,
            )
        )
        by_key = {_rollup_key(record): record for record in selected}

        finer = [seconds for seconds in sorted(TIER_FOR_RESOLUTION, reverse=True) if seconds < resolution_seconds]
        fallback_start = start
        if selected:
            current_day_start = datetime.fromtimestamp(
                int(end.timestamp()) - (int(end.timestamp()) % RESOLUTIONS["1d"]),
                timezone.utc,
            )
            fallback_start = max(start, current_day_start)
            if fallback_start >= end:
                finer = []
        for finer_seconds in finer:
            tier = TIER_FOR_RESOLUTION[finer_seconds]
            source = list(
                self.store.iter_rollups(
                    tier,
                    start=fallback_start,
                    end=end,
                    uid=uid,
                    gpu=gpu,
                    limit=MAX_QUERY_RECORDS + 1,
                )
            )
            if not source:
                continue
            for record in aggregate_rollups(source, resolution_seconds):
                by_key.setdefault(_rollup_key(record), record)
            if len(source) > MAX_QUERY_RECORDS:
                break

        records = sorted(by_key.values(), key=_rollup_sort_key)
        truncated = len(records) > limit
        return records[:limit], truncated


def auto_resolution(start: datetime, end: datetime) -> int:
    seconds = max(1, int((end - start).total_seconds()))
    if seconds <= 2 * 24 * 60 * 60:
        return RESOLUTIONS["1m"]
    if seconds <= 60 * 24 * 60 * 60:
        return RESOLUTIONS["5m"]
    if seconds <= 400 * 24 * 60 * 60:
        return RESOLUTIONS["10m"]
    if seconds <= 1500 * 24 * 60 * 60:
        return RESOLUTIONS["1h"]
    return RESOLUTIONS["1d"]


def _sample_coverage(records: Sequence[dict], start: datetime, end: datetime) -> dict:
    """Describe stored sample coverage without implying that sparse spans are continuous."""
    starts: List[datetime] = []
    ends: List[datetime] = []
    for record in records:
        try:
            record_start = parse_iso(str(record["window_start"]))
            record_end = parse_iso(str(record["window_end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if record_end <= start or record_start >= end:
            continue
        starts.append(max(start, record_start))
        ends.append(min(end, record_end))
    return {
        "record_count": len(starts),
        "first_sample_at": to_iso(min(starts)) if starts else None,
        "last_sample_at": to_iso(max(ends)) if ends else None,
        "continuous": False,
    }


def summarize_public_rollups(
    records: Sequence[dict],
    *,
    redact_labels: bool = False,
) -> List[dict]:
    """Build user summaries from public sample records without reading storage."""
    workloads: Dict[int, dict] = {}
    normalized = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("public usage sample record must be an object")
        normalized.append(record)
        for value in record.get("workloads", []):
            if not isinstance(value, dict):
                continue
            workload_id = value.get("workload_id")
            if isinstance(workload_id, bool) or not isinstance(workload_id, int):
                continue
            descriptor = dict(value)
            descriptor.pop("workload_id", None)
            workloads.setdefault(workload_id, descriptor)
    return _summarize_users(normalized, workloads, redact_labels)


def _summarize_users(records: Sequence[dict], workloads: Dict[int, dict], redact_labels: bool) -> List[dict]:
    groups: Dict[int, dict] = {}
    for record in records:
        uid = record.get("uid")
        if uid is None:
            continue
        uid = int(uid)
        group = groups.setdefault(
            uid,
            {
                "uid": uid,
                "username": str(record.get("username", uid)),
                "reserved_gpu_seconds": 0.0,
                "active_gpu_seconds": 0.0,
                "idle_reserved_gpu_seconds": 0.0,
                "violation_gpu_seconds": 0.0,
                "sampled_gpu_seconds": 0.0,
                "verified_gpu_seconds": 0.0,
                "inferred_gpu_seconds": 0.0,
                "ambiguous_gpu_seconds": 0.0,
                "_sm_total": 0.0,
                "_sm_samples": 0,
                "max_sm_percent": None,
                "max_gpu_memory_mb": 0,
                "gpus": set(),
                "nodes": set(),
                "devices": set(),
                "statuses": {},
                "workload_observed_seconds": {},
            },
        )
        observed = max(0.0, float(record.get("observed_seconds", 0)))
        active = max(0.0, float(record.get("active_observed_seconds", 0)))
        status = str(record.get("status", "unknown"))
        group["sampled_gpu_seconds"] += observed
        group["active_gpu_seconds"] += active
        identity_sources = set(
            record.get("extensions", {})
            .get("gpubk.container-summary", {})
            .get("identity_sources", [])
        )
        if "container-ambiguous" in identity_sources:
            group["ambiguous_gpu_seconds"] += active
        elif "container-reservation" in identity_sources:
            group["inferred_gpu_seconds"] += active
        else:
            group["verified_gpu_seconds"] += active
        if status == "ok":
            group["reserved_gpu_seconds"] += observed
            group["idle_reserved_gpu_seconds"] += max(0.0, observed - active)
        elif status not in {"idle", "system"}:
            group["violation_gpu_seconds"] += active or observed
        group["gpus"].add(int(record.get("gpu", -1)))
        node_id = record_node_id(record)
        group["nodes"].add(node_id)
        group["devices"].add((node_id, int(record.get("gpu", -1))))
        group["statuses"][status] = group["statuses"].get(status, 0.0) + observed
        sm_samples = max(0, int(record.get("sm_sample_count", 0)))
        if record.get("avg_sm_percent") is not None:
            group["_sm_total"] += float(record["avg_sm_percent"]) * sm_samples
            group["_sm_samples"] += sm_samples
        if record.get("max_sm_percent") is not None:
            group["max_sm_percent"] = _max_optional(group["max_sm_percent"], record["max_sm_percent"])
        group["max_gpu_memory_mb"] = max(group["max_gpu_memory_mb"], int(record.get("max_gpu_memory_mb") or 0))
        for workload_id, seconds in record.get("workload_observed_seconds", {}).items():
            numeric_id = int(workload_id)
            group["workload_observed_seconds"][numeric_id] = (
                group["workload_observed_seconds"].get(numeric_id, 0.0) + float(seconds)
            )

    result = []
    for uid in sorted(groups):
        group = groups[uid]
        sm_samples = group.pop("_sm_samples")
        sm_total = group.pop("_sm_total")
        workload_seconds = group.pop("workload_observed_seconds")
        group["avg_sm_percent"] = round(sm_total / sm_samples, 3) if sm_samples else None
        group["gpus"] = sorted(value for value in group["gpus"] if value >= 0)
        group["nodes"] = sorted(group["nodes"])
        group["devices"] = [
            {"node_id": node_id, "gpu": gpu}
            for node_id, gpu in sorted(group["devices"])
            if gpu >= 0
        ]
        for key in (
            "reserved_gpu_seconds",
            "active_gpu_seconds",
            "idle_reserved_gpu_seconds",
            "violation_gpu_seconds",
            "sampled_gpu_seconds",
            "verified_gpu_seconds",
            "inferred_gpu_seconds",
            "ambiguous_gpu_seconds",
        ):
            group[key] = round(group[key], 3)
        group["statuses"] = {key: round(value, 3) for key, value in sorted(group["statuses"].items())}
        group["workloads"] = []
        for workload_id, seconds in sorted(workload_seconds.items(), key=lambda item: (-item[1], item[0])):
            descriptor = dict(workloads.get(workload_id, _unknown_workload(workload_id)))
            if redact_labels:
                descriptor["label"] = descriptor.get("kind", "unknown")
            group["workloads"].append(
                {"workload_id": workload_id, "observed_gpu_seconds": round(seconds, 3), **descriptor}
            )
        result.append(group)
    return result


def _public_rollup(record: dict, workloads: Dict[int, dict], include: bool, redact: bool) -> dict:
    result = dict(record)
    if include:
        result["workloads"] = []
        for workload_id in record.get("workload_ids", []):
            descriptor = dict(workloads.get(int(workload_id), _unknown_workload(int(workload_id))))
            if redact:
                descriptor["label"] = descriptor.get("kind", "unknown")
            result["workloads"].append({"workload_id": int(workload_id), **descriptor})
    return result


def _public_event(record: dict, workloads: Dict[int, dict], include: bool, redact: bool) -> dict:
    result = dict(record)
    workload_id = record.get("workload_id")
    if include and workload_id is not None:
        descriptor = dict(workloads.get(int(workload_id), _unknown_workload(int(workload_id))))
        if redact:
            descriptor["label"] = descriptor.get("kind", "unknown")
        result["workload"] = {"workload_id": int(workload_id), **descriptor}
    return result


def _unknown_workload(workload_id: int) -> dict:
    return {
        "schema_version": "gpubk.usage.workload.unknown",
        "id": workload_id,
        "launcher": "unknown",
        "entrypoint_kind": "unknown",
        "kind": "unknown",
        "framework": "unknown",
        "execution": "unknown",
        "source": "unknown",
        "confidence": 0,
        "label": "unknown",
        "created_at": None,
    }


def _normalize_range(start: datetime, end: Optional[datetime]) -> Tuple[datetime, datetime]:
    normalized_start = start.astimezone(timezone.utc)
    normalized_end = (end or utc_now()).astimezone(timezone.utc)
    if normalized_end <= normalized_start:
        raise ValueError("usage end must be after start")
    return normalized_start, normalized_end


def _validate_limit(limit: int) -> None:
    if limit < 1 or limit > MAX_QUERY_RECORDS:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_RECORDS}")


def _rollup_key(record: dict) -> tuple:
    return (
        str(record.get("window_start", "")),
        int(record.get("gpu", -1)),
        record.get("uid"),
        str(record.get("status", "unknown")),
    )


def _rollup_sort_key(record: dict) -> tuple:
    return (
        parse_iso(str(record["window_start"])),
        int(record.get("gpu", -1)),
        -1 if record.get("uid") is None else int(record["uid"]),
        str(record.get("status", "unknown")),
    )


def _max_optional(left, right):
    parsed = float(right)
    return parsed if left is None else max(float(left), parsed)
